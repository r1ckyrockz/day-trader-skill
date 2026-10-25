#!/usr/bin/env python3
"""A hard gate in front of the broker's order tools (Claude Code PreToolUse hook).

The rules in trader-broker/SKILL.md (risk PASS, an explicit yes, one order per
yes) are instructions to Claude. This hook turns them into a check that runs
outside the model: Claude Code calls it before every matching tool call, and
an exit code of 2 blocks the call, whatever the conversation says.

For place_stock_order it lets the call through only if ALL of these hold:

  1. ticket.py recorded an explicit yes today (status "approved") ...
  2. ... whose tool_input is exactly the arguments of this call,
  3. that yes is younger than gate_max_age_min (default 15 minutes),
  4. trader-risk PASSed that same order today (same fingerprint),
  5. for a buy: the live price was read from the broker and recorded with
     that yes, within max_price_drift_pct of the plan's entry (default 1 %), and
  6. that yes has not been used yet (one yes, one call).

When it lets a call through it writes a "sent" record, so the same yes can
never place a second order. Letting a call through does NOT skip Claude's own
Allow once / Deny card: the normal permission check still follows.

Tools that empty or lever an account (close_all_positions, cancel_all_orders,
option and crypto orders, account config) are always blocked. A config can
allow some of them by name in "gate_allow". Every other tool passes.

If anything is unclear (unreadable input, missing files) an order tool is
blocked: the gate fails closed.

Install (Claude Code), in .claude/settings.json or via the plugin's
hooks/hooks.json:

    {"hooks": {"PreToolUse": [{"matcher": "mcp__.*alpaca.*",
       "hooks": [{"type": "command",
                  "command": "python3 /path/to/trader-broker/gate.py"}]}]}}

Claude Desktop has no hooks; there the Allow once / Deny card is the only
check outside the model. Standard library only.
"""

import datetime
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ticket  # noqa: E402  (same folder)

ORDER_TOOLS = {"place_stock_order"}
ALWAYS_BLOCK = {"close_all_positions", "cancel_all_orders", "place_option_order", "place_crypto_order",
                "exercise_options_position", "do_not_exercise_options_position", "update_account_config"}
DEFAULT_MAX_AGE_MIN = 15


class Blocked(Exception):
    pass


def _norm(d):
    """Arguments as the ticket wrote them: numbers as strings, empty values dropped."""
    out = {}
    for k, v in (d or {}).items():
        if v is None or v == "":
            continue
        out[k] = ticket._str_num(v) if not isinstance(v, (dict, list)) else v
    return out


def _parse_at(text):
    """"2026-10-19T10:15:00-04:00" -> naive UTC, or None."""
    try:
        dt = datetime.datetime.fromisoformat(str(text))
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        return None
    return (dt - dt.utcoffset()).replace(tzinfo=None)


def _paths(cwd):
    base = os.path.join(cwd or ".", os.environ.get("TRADER_DIR", "trader"))
    return {"config": os.path.join(base, "config.json"),
            "risk": os.path.join(base, "state", "risk.json"),
            "state": os.path.join(base, "state", "broker.json")}


def check_order(args, cwd, now):
    """Raises Blocked with the reason, or returns (state, record) to mark as sent."""
    p = _paths(cwd)
    cfg = ticket.read_json(p["config"], "config", required=False) or {}
    risk = ticket.read_json(p["risk"], "risk check", required=False)
    state = ticket.load_state(p["state"])
    today = ticket.to_eastern(now).date().isoformat()
    max_age = float(cfg.get("gate_max_age_min", DEFAULT_MAX_AGE_MIN))
    want = _norm(args)

    # The latest record per fingerprint today: only an unused "approved" counts.
    last = {}
    for rec in state.get("orders", []):
        fp = rec.get("fingerprint")
        if fp and ticket.rec_day(rec) == today:
            last[fp] = rec
    candidates = [r for r in last.values() if r.get("status") == "approved"]
    if not candidates:
        raise Blocked("no unused yes recorded today. Show the ticket (ticket.py show), get the user's "
                      "explicit yes and record it with ticket.py approve first")
    match = [r for r in candidates if _norm(r.get("tool_input")) == want]
    if not match:
        raise Blocked("these arguments are not the ones the user said yes to. Call place_stock_order with "
                      "exactly the JSON that ticket.py approve printed: %s"
                      % "; ".join(json.dumps(r.get("tool_input"), sort_keys=True) for r in candidates))
    rec = match[-1]
    at = _parse_at(rec.get("at"))
    if at is None:
        raise Blocked("the recorded yes has no readable time")
    age = (now - at).total_seconds() / 60.0
    if age < -1:
        raise Blocked("the recorded yes is in the future (%s); check the clock" % rec.get("at"))
    if age > max_age:
        raise Blocked("the yes is %.0f minutes old (limit %g). Prices move: run trader-risk again and ask "
                      "the user again" % (age, max_age))
    if str(rec.get("side", "")).lower() == "buy" and str(cfg.get("broker", "alpaca")).lower() != "ibkr":
        if rec.get("quote") is None:
            raise Blocked("no live price was checked for this buy. Read get_stock_latest_quote and record "
                          "it: ticket.py approve with --quote")
        limit = cfg.get("max_price_drift_pct", ticket.DEFAULT_DRIFT_PCT)
        if rec.get("quote_drift_pct") is not None and abs(float(rec["quote_drift_pct"])) > float(limit):
            raise Blocked("the live price was %+.2f%% from the plan (limit %g%%)" % (rec["quote_drift_pct"], float(limit)))
    rv, why = ticket.risk_verdict(risk, rec.get("fingerprint"), today)
    if rv is None:
        raise Blocked(why)
    if rv.get("verdict") != "PASS":
        raise Blocked("trader-risk did not pass this order: %s" % "; ".join(rv.get("reasons", [])))
    return state, rec, p["state"]


def mark_sent(state, rec, state_path, now):
    sent = {k: rec[k] for k in ("index", "fingerprint", "symbol", "side", "qty", "kind", "max_loss", "stop",
                                "plain", "tool_input") if rec.get(k) is not None}
    sent.update({"at": ticket.ny_iso(now), "date": rec.get("date"), "status": "sent", "by": "gate.py"})
    state.setdefault("orders", []).append(sent)
    ticket.write_json(state_path, state)


def decide(event, now=None):
    """(exit_code, message). 0 lets the call continue to the normal permission check, 2 blocks it."""
    now = now or datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0, tzinfo=None)
    name = str(event.get("tool_name") or "")
    action = name.rsplit("__", 1)[-1]
    cwd = event.get("cwd") or os.getcwd()
    if action in ALWAYS_BLOCK:
        cfg = ticket.read_json(_paths(cwd)["config"], "config", required=False) or {}
        if action not in set(cfg.get("gate_allow") or []):
            return 2, ("trader-broker gate: %s is blocked. It can close, cancel or lever more than one "
                       "approved order. Do it yourself in the broker's dashboard if you mean it." % action)
        return 0, ""
    if action not in ORDER_TOOLS:
        return 0, ""
    try:
        state, rec, state_path = check_order(event.get("tool_input") or {}, cwd, now)
        mark_sent(state, rec, state_path, now)
    except Blocked as e:
        return 2, "trader-broker gate: order blocked: %s." % e
    except (ticket.TicketError, OSError, ValueError, TypeError) as e:
        return 2, "trader-broker gate: order blocked, the gate could not check it (%s)." % e
    return 0, ""


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    now = ticket.parse_now(argv[argv.index("--now") + 1]) if "--now" in argv else None
    try:
        event = json.loads(sys.stdin.read() or "{}")
        if not isinstance(event, dict):
            raise ValueError("hook input is not a JSON object")
    except ValueError as e:
        # Fail closed: without readable input we cannot tell which tool this is.
        print("trader-broker gate: unreadable hook input (%s); blocked." % e, file=sys.stderr)
        return 2
    code, msg = decide(event, now)
    if msg:
        print(msg, file=sys.stderr)
    return code


if __name__ == "__main__":
    sys.exit(main())
