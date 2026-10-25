#!/usr/bin/env python3
"""Backtest for trader-timemachine: does the opening-range breakout make money
after costs, across many stocks and, with Alpaca data, across years?
Standard library only.

    python3 backtest.py                                   # the default list of liquid US stocks
    python3 backtest.py --symbols TSLA NVDA AMD AAPL
    python3 backtest.py --watchlist                       # trader/watchlist.json (the scanner's hits)
    python3 backtest.py --target 2 --stop or_low          # another rule
    python3 backtest.py --grid                            # several stops and targets side by side
    python3 backtest.py --write --csv trader/trades.csv   # save the report and every trade

    python3 backtest.py --split --explore                 # look for filters on the FIRST half only
    python3 backtest.py --split --min-rvol 1.5 --market   # then judge a filter on the SECOND half

Data:
  - Alpaca (when ALPACA_API_KEY and ALPACA_SECRET_KEY are set, or
    alpaca_api_key / alpaca_secret_key in trader/config.json): consolidated
    (SIP) 5-minute bars for --years (default 2), split-adjusted. A free paper
    account is enough: data older than 15 minutes is the same on every plan.
    The keys go in request headers, never in the URL.
  - Yahoo otherwise (or with --source yahoo): the last 60 days only.
  Downloads are cached for the day in trader/state/backtest-cache/.
  --offline DIR reads <SYM>-60d5m.json(.gz) (Yahoo format) from DIR.

The rule (long only, like the rest of the pack):
  - Opening range: the first --or-minutes (15) of the session.
  - Signal: the first 5-minute bar that CLOSES above the range high, with its
    close inside the entry window (no_entries_before / no_entries_after from
    trader/config.json, default 9:45 to 15:30 New York time).
  - Fill: the OPEN of the next bar, plus slippage. Not the signal bar's close:
    nobody gets that price. If that open is already at or under the stop, no trade.
  - Stop: the range's midpoint (or_mid) or low (or_low), to the cent below.
  - Target: entry + target x risk.
  - Then bar by bar: the stop is checked before the target (a bar that touches
    both is a loss). A bar that opens through the stop fills at its open. The
    stop fill pays slippage; the target is a limit order and fills at its price.
  - Flat by flat_by (15:55): sold at the close of the 15:50 bar, minus slippage.
  - Size: shares = max_loss_per_trade_usd / risk per share, rounded down, and
    never more than max_order_usd. A risk under 0.05 % of the price is skipped.
  - Your day limits: at most max_trades_per_day trades, and no new trade once
    the day's closed trades lost max_daily_loss_usd.

Filters (off unless you ask), measured on every signal:
  --min-rvol X    the opening range's volume is at least X times its average
                  over the stock's previous 20 sessions (stocks "in play")
  --min-gap / --max-gap PCT   today's open against yesterday's close, in percent
  --market        SPY is above its own open at the signal (the market is up)

Honest testing (--split): the sessions are cut in two by date. --explore shows
how the signals do by rvol, gap and market direction on the FIRST half only.
Pick a filter there, then run it with --split: the verdict comes from the
SECOND half, which the choice never saw. A filter that only works on the first
half was luck.

Costs: --slippage-bps (default 5 = 0.05 % per side) and --commission (dollars
per share and side, default 0: Alpaca charges none on US stocks).

The verdict: the mean R per trade with a 95 % confidence interval
(mean +- 1.96 x standard deviation / square root of the number of trades).
  - the whole interval above 0: an edge in this sample
  - the whole interval below 0: loses money in this sample
  - 0 inside the interval: no evidence either way; that is not "it works"

The default list is today's big names, which flatters any long rule a little
(stocks that crashed out are not in it). Not financial advice.
"""

import argparse
import csv
import datetime
import gzip
import json
import math
import os
import statistics
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import timemachine as tm  # noqa: E402  (same folder: the data and session code)

DEFAULT_SYMBOLS = (
    "AAPL MSFT NVDA AMZN GOOGL META TSLA AVGO AMD NFLX ORCL CRM ADBE INTC QCOM MU TXN AMAT LRCX "
    "PLTR UBER SHOP COIN SNOW PANW CRWD ABNB PYPL XYZ DIS NKE SBUX MCD WMT COST HD LOW TGT "
    "JPM BAC WFC GS MS C V MA AXP XOM CVX OXY COP BA CAT DE GE LMT UNH LLY JNJ PFE MRK ABBV "
    "KO PEP PG T VZ F GM RIVN SOFI MARA SMCI ARM DELL"
).split()
MARKET = "SPY"
DEFAULTS = {"no_entries_before": "09:45", "no_entries_after": "15:30", "flat_by": "15:55",
            "max_loss_per_trade_usd": 50.0, "max_order_usd": 4000.0, "max_trades_per_day": 3,
            "max_daily_loss_usd": 150.0}
MIN_RISK_PCT = 0.05
Z95 = 1.96
RVOL_DAYS = 20
RVOL_MIN_DAYS = 10
ALPACA_BARS = "https://data.alpaca.markets/v2/stocks/bars"
ALPACA_RATE = 180       # requests a minute, all threads together: under the free plan's 200
ALPACA_WORKERS = 4      # symbols downloaded at the same time
ALPACA_PAGE = 10000


class BTError(Exception):
    pass


# ---------------------------------------------------------------- inputs

def read_config(path):
    cfg = dict(DEFAULTS)
    doc = {}
    if path and os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as fh:
                doc = json.load(fh)
        except ValueError as e:
            raise BTError("%s is not valid JSON: %s" % (path, e))
        for k in DEFAULTS:
            if doc.get(k) is not None:
                cfg[k] = doc[k]
    cfg["_alpaca"] = alpaca_keys(doc)
    return cfg


def alpaca_keys(doc=None):
    """(key, secret) from the environment (the names the Alpaca MCP server uses, or
    Alpaca's own APCA_ names), else from the config. None when not both are set."""
    doc = doc or {}
    key = os.environ.get("ALPACA_API_KEY") or os.environ.get("APCA_API_KEY_ID") or doc.get("alpaca_api_key")
    sec = os.environ.get("ALPACA_SECRET_KEY") or os.environ.get("APCA_API_SECRET_KEY") or doc.get("alpaca_secret_key")
    key, sec = str(key or "").strip(), str(sec or "").strip()
    return (key, sec) if key and sec else None


def minutes(text, what):
    try:
        return tm.parse_clock(text, what)
    except tm.TMError as e:
        raise BTError(str(e))


def watchlist_symbols(path):
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError) as e:
        raise BTError("could not read %s: %s" % (path, e))
    syms = [str(h.get("symbol")).upper() for h in doc.get("hits") or [] if isinstance(h, dict) and h.get("symbol")]
    if not syms:
        raise BTError("no hits in %s" % path)
    return syms


# ---------------------------------------------------------------- data

def _cache_read(path):
    if path and os.path.exists(path):
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            return fh.read()
    return None


def _cache_write(path, text):
    if path:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with gzip.open(path, "wt", encoding="utf-8") as fh:
            fh.write(text)


def yahoo_bars(sym, offline=None, cache=None):
    try:
        text = tm.read_offline(offline, sym) if offline else _cache_read(cache)
    except tm.TMError as e:
        raise BTError(str(e))
    if text is None:
        try:
            text = tm.fetch_chart(sym)
        except tm.TMError as e:
            raise BTError(str(e))
        _cache_write(cache, text)
    try:
        result = tm.chart_result(text, sym)
    except tm.TMError as e:
        raise BTError(str(e))
    _, bars, _ = tm.parse_bars(result, datetime.datetime.utcnow().replace(microsecond=0))
    return bars


def _alpaca_get(url, keys, retries=3):
    req = urllib.request.Request(url, headers={"APCA-API-KEY-ID": keys[0], "APCA-API-SECRET-KEY": keys[1],
                                               "Accept": "application/json", "User-Agent": tm.UA})
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < retries - 1:
                tm._sleep(5.0)
                continue
            if e.code in (401, 403):
                raise BTError("Alpaca refused the request (HTTP %d): check ALPACA_API_KEY and ALPACA_SECRET_KEY, "
                              "or try --feed iex" % e.code)
            raise BTError("Alpaca answered HTTP %d" % e.code)
        except (OSError, ValueError) as e:
            if attempt < retries - 1:
                tm._sleep(2.0)
                continue
            raise BTError("could not read Alpaca's answer (%s)" % e.__class__.__name__)
    raise BTError("Alpaca is rate limiting (HTTP 429). Wait a minute and run it again.")


def alpaca_to_bars(rows):
    """Alpaca bars -> the Time Machine's bar dicts, regular session only (9:30 to 16:00 ET)."""
    out = {}
    for b in rows:
        try:
            utc = datetime.datetime.fromisoformat(str(b["t"]).replace("Z", "+00:00"))
            o, h, l, c = float(b["o"]), float(b["h"]), float(b["l"]), float(b["c"])
        except (KeyError, TypeError, ValueError):
            continue
        utc = utc.astimezone(datetime.timezone.utc).replace(tzinfo=None)
        local = tm.to_eastern(utc)
        mins = local.hour * 60 + local.minute - tm.OPEN_MIN
        if mins < 0 or mins >= tm.SESSION_BARS * 5 or mins % 5 or local.second:
            continue
        out[utc] = {"date": local.date().isoformat(), "slot": mins // 5, "start": local.strftime("%H:%M"),
                    "o": o, "h": h, "l": l, "c": c, "v": float(b.get("v") or 0)}
    return [out[k] for k in sorted(out)]


class RateLimiter(object):
    """At most per_minute request starts a minute, shared by every download thread."""

    def __init__(self, per_minute):
        self.gap = 60.0 / per_minute if per_minute else 0.0
        self.lock = threading.Lock()
        self.next = 0.0

    def wait(self):
        if not self.gap:
            return
        with self.lock:
            slot = max(time.monotonic(), self.next)
            self.next = slot + self.gap
        delay = slot - time.monotonic()
        if delay > 0:
            time.sleep(delay)


def month_chunks(start, end, today=None):
    """[(first day, last day, cache name, final)] per calendar month. A whole month that
    ended before today is final: its bars never change, so its cache is kept for good."""
    s, e = datetime.date.fromisoformat(start), datetime.date.fromisoformat(end)
    today = today or datetime.date.today()
    out, cur = [], datetime.date(s.year, s.month, 1)
    while cur <= e:
        nxt = datetime.date(cur.year + (cur.month == 12), cur.month % 12 + 1, 1)
        last = nxt - datetime.timedelta(days=1)
        cs, ce = max(cur, s), min(last, e)
        final = cs == cur and ce == last and last < today
        name = cur.strftime("%Y-%m") if final else "%s_%s" % (cs.isoformat(), ce.isoformat())
        out.append((cs.isoformat(), ce.isoformat(), name, final))
        cur = nxt
    return out


def _alpaca_chunk(sym, keys, first, last, feed, limiter):
    start = first + "T00:00:00Z"
    end = (datetime.date.fromisoformat(last) + datetime.timedelta(days=1)).isoformat() + "T00:00:00Z"
    rows, token = [], None
    while True:
        q = {"symbols": sym, "timeframe": "5Min", "start": start, "end": end, "limit": ALPACA_PAGE,
             "adjustment": "split", "feed": feed}
        if token:
            q["page_token"] = token
        if limiter:
            limiter.wait()
        doc = _alpaca_get(ALPACA_BARS + "?" + urllib.parse.urlencode(q), keys)
        rows += (doc.get("bars") or {}).get(sym) or []
        token = doc.get("next_page_token")
        if not token:
            return alpaca_to_bars(rows)


def alpaca_bars(sym, keys, start, end, feed="sip", cache_dir=None, limiter=None, today=None, stats=None):
    """Regular-session 5-minute bars from Alpaca, month by month, each month cached."""
    found = {}
    for first, last, name, final in month_chunks(start, end, today):
        path = os.path.join(cache_dir, "alpaca-%s-%s-%s.json.gz" % (sym, name, feed)) if cache_dir else None
        text = _cache_read(path)
        if text is not None:
            bars = json.loads(text)
            if stats is not None:
                stats["cached"] = stats.get("cached", 0) + 1
        else:
            bars = _alpaca_chunk(sym, keys, first, last, feed, limiter)
            _cache_write(path, json.dumps(bars))
            if stats is not None:
                stats["fetched"] = stats.get("fetched", 0) + 1
        for b in bars:
            found[(b["date"], b["slot"])] = b
    if not found:
        raise BTError("Alpaca has no 5-minute bars for %s from %s to %s" % (sym, start, end))
    return [found[k] for k in sorted(found)]


def load_sessions(sym, src):
    """Full regular sessions, oldest first: [(date, [78 bars])]."""
    cache_dir, tag = src.get("cache_dir"), src.get("today")
    if src["source"] == "alpaca":
        bars = alpaca_bars(sym, src["keys"], src["start"], src["end"], src["feed"], cache_dir,
                           src.get("limiter"), stats=src.get("stats"))
    else:
        cache = os.path.join(cache_dir, "%s-%s.json.gz" % (sym, tag)) if cache_dir and tag else None
        bars = yahoo_bars(sym, src.get("offline"), cache)
    full, _ = tm.full_sessions(tm.split_sessions(bars), "9999-12-31")
    return full


# ---------------------------------------------------------------- one day, one stock

def trade_day(sym, day, D, rule, cfg, ctx=None):
    """The rule on one full session: a trade dict, or None when it does not fire.
    ctx (optional): prev_close, avg_or_volume, market = SPY's session that day."""
    ctx = ctx or {}
    n = rule["or_minutes"] // 5
    oh, ol = tm.opening_range(D, n)
    before = minutes(cfg["no_entries_before"], "no_entries_before") - tm.OPEN_MIN
    after = minutes(cfg["no_entries_after"], "no_entries_after") - tm.OPEN_MIN
    flat = minutes(cfg["flat_by"], "flat_by") - tm.OPEN_MIN
    last = min(tm.SESSION_BARS - 1, flat // 5 - 1)           # the bar that ends at flat_by
    slip = rule["slippage_bps"] / 10000.0
    k = None
    for i in range(n, last):
        end = 5 * (i + 1)
        if end < before:
            continue
        if end > after:
            break
        if D[i]["c"] > oh:
            k = i
            break
    if k is None:
        return None
    fill = round(D[k + 1]["o"] * (1 + slip), 4)
    stop = tm.rule_stop({"stop_rule": rule["stop_rule"]}, oh, ol, fill, True)
    if fill <= stop:
        return None                                          # opened through the stop: no trade
    risk = fill - stop
    if risk < fill * MIN_RISK_PCT / 100.0:
        return None
    target = round(fill + rule["target_r"] * risk, 2)
    qty = int(float(cfg["max_loss_per_trade_usd"]) // risk)
    qty = min(qty, int(float(cfg["max_order_usd"]) // fill))
    if qty < 1:
        return None
    outcome, exit_px, exit_slot = None, None, last
    for j in range(k + 1, last + 1):
        b = D[j]
        if b["l"] <= stop:
            outcome = "stop"
            exit_px = min(stop, b["o"]) * (1 - slip)        # a gap through the stop fills at the open
            exit_slot = j
            break
        if b["h"] >= target:
            outcome = "target"
            exit_px = max(target, b["o"]) if j > k + 1 else target
            exit_slot = j
            break
    if outcome is None:
        outcome = "flat"
        exit_px = D[last]["c"] * (1 - slip)
    fees = 2 * qty * rule["commission"]
    pnl = qty * (exit_px - fill) - fees
    R = pnl / (qty * risk)
    or_vol = sum(D[i]["v"] for i in range(n))
    prev = ctx.get("prev_close")
    avg = ctx.get("avg_or_volume")
    mkt = ctx.get("market")
    return {"symbol": sym, "date": day, "signal": tm.clock24(tm.OPEN_MIN + 5 * (k + 1)),
            "entry_slot": k + 1, "exit_slot": exit_slot, "entry": round(fill, 2), "stop": stop,
            "target": target, "qty": qty, "outcome": outcome, "exit": round(exit_px, 2),
            "pnl": round(pnl, 2), "R": round(R, 3),
            "rvol": round(or_vol / avg, 2) if avg else None,
            "gap_pct": round(100.0 * (D[0]["o"] / prev - 1), 2) if prev else None,
            "market_up": (mkt[k]["c"] > mkt[0]["o"]) if mkt else None}


# ---------------------------------------------------------------- filters

def make_filter(a):
    """A predicate over a trade from the CLI flags, and a label for the report. A trade
    whose feature is unknown (no history yet, no SPY that day) does not pass an active filter."""
    tests, label = [], []
    if a.min_rvol is not None:
        tests.append(lambda t: t["rvol"] is not None and t["rvol"] >= a.min_rvol)
        label.append("rvol >= %g" % a.min_rvol)
    if a.min_gap is not None:
        tests.append(lambda t: t["gap_pct"] is not None and t["gap_pct"] >= a.min_gap)
        label.append("gap >= %+g %%" % a.min_gap)
    if a.max_gap is not None:
        tests.append(lambda t: t["gap_pct"] is not None and t["gap_pct"] <= a.max_gap)
        label.append("gap <= %+g %%" % a.max_gap)
    if a.market:
        tests.append(lambda t: t["market_up"] is True)
        label.append("SPY above its open")
    return (lambda t: all(f(t) for f in tests)), ", ".join(label)


# ---------------------------------------------------------------- the whole sample

def apply_day_limits(trades, cfg):
    """Your day rules across all stocks: in the order the trades start, at most
    max_trades_per_day, and none once the day's closed trades lost max_daily_loss_usd."""
    by_day = {}
    for t in trades:
        by_day.setdefault(t["date"], []).append(t)
    taken = []
    for day in sorted(by_day):
        today = []
        for t in sorted(by_day[day], key=lambda x: (x["entry_slot"], x["symbol"])):
            closed = sum(x["pnl"] for x in today if x["exit_slot"] < t["entry_slot"])
            if closed <= -float(cfg["max_daily_loss_usd"]):
                continue
            if len(today) >= int(cfg["max_trades_per_day"]):
                continue
            today.append(t)
        taken += today
    return taken


def stats(trades):
    if not trades:
        return {"trades": 0}
    rs = [t["R"] for t in trades]
    pnls = [t["pnl"] for t in sorted(trades, key=lambda x: (x["date"], x["entry_slot"], x["symbol"]))]
    mean = statistics.mean(rs)
    sd = statistics.stdev(rs) if len(rs) > 1 else 0.0
    half = Z95 * sd / math.sqrt(len(rs)) if len(rs) > 1 else float("inf")
    eq, peak, dd = 0.0, 0.0, 0.0
    for p in pnls:
        eq += p
        peak = max(peak, eq)
        dd = min(dd, eq - peak)
    gains = sum(p for p in pnls if p > 0)
    losses = -sum(p for p in pnls if p < 0)
    days = len({t["date"] for t in trades})
    return {"trades": len(trades), "days": days,
            "win_rate": round(sum(1 for r in rs if r > 0) / float(len(rs)), 4),
            "target": sum(1 for t in trades if t["outcome"] == "target"),
            "stop": sum(1 for t in trades if t["outcome"] == "stop"),
            "flat": sum(1 for t in trades if t["outcome"] == "flat"),
            "mean_R": round(mean, 4), "median_R": round(statistics.median(rs), 4),
            "ci_low": round(mean - half, 4), "ci_high": round(mean + half, 4),
            "total_pnl": round(sum(pnls), 2), "avg_pnl": round(sum(pnls) / len(pnls), 2),
            "profit_factor": round(gains / losses, 3) if losses else None,
            "max_drawdown": round(dd, 2), "per_day": round(len(trades) / float(days), 2)}


def verdict(s):
    if not s.get("trades"):
        return "no trades: nothing to judge"
    if s["trades"] < 30:
        return "too few trades (%d) to judge: test more stocks" % s["trades"]
    if s["ci_low"] > 0:
        return "an edge in this sample: the whole 95 % interval is above zero"
    if s["ci_high"] < 0:
        return "loses money in this sample: the whole 95 % interval is below zero"
    return "no evidence of an edge: zero is inside the 95 % interval, it could be luck either way"


def collect(symbols, rule, cfg, src, log=None, need_market=False):
    """Every signal of every symbol, with its features. (trades, sessions, skipped, all dates)"""
    market = {}
    if need_market:
        try:
            market = dict(load_sessions(MARKET, src))
        except BTError as e:
            if need_market != "soft":
                raise BTError("the market filter needs %s: %s" % (MARKET, e))
            if log:
                log("no %s data (%s): market direction left out" % (MARKET, e))
    trades, skipped, sessions, dates = [], [], 0, set()
    for sym, full in download(symbols, src, log):
        if isinstance(full, BTError):
            skipped.append({"symbol": sym, "why": str(full)})
            continue
        sessions += len(full)
        n = rule["or_minutes"] // 5
        or_vols, prev_close = [], None
        for day, D in full:
            dates.add(day)
            hist = or_vols[-RVOL_DAYS:]
            ctx = {"prev_close": prev_close, "market": market.get(day),
                   "avg_or_volume": (sum(hist) / len(hist)) if len(hist) >= RVOL_MIN_DAYS and sum(hist) > 0 else None}
            t = trade_day(sym, day, D, rule, cfg, ctx)
            if t:
                trades.append(t)
            or_vols.append(sum(D[j]["v"] for j in range(n)))
            prev_close = D[-1]["c"]
    return trades, sessions, skipped, sorted(dates)


def download(symbols, src, log=None):
    """[(symbol, sessions or BTError)] in the given order. Alpaca: several symbols at a
    time under one shared rate limit; Yahoo: one after the other, politely."""
    total, done, began = len(symbols), [0], time.monotonic()
    lock = threading.Lock()

    def one(i, sym):
        try:
            if src["source"] != "alpaca" and i and src.get("pause"):
                tm._sleep(src["pause"])
            res = load_sessions(sym, src)
        except BTError as e:
            res = e
        if log:
            with lock:
                done[0] += 1
                spent = time.monotonic() - began
                left = spent / done[0] * (total - done[0])
                what = ("%d sessions" % len(res)) if not isinstance(res, BTError) else "skipped"
                eta = (", about %d min left" % max(1, round(left / 60.0))) if total - done[0] and left > 60 else ""
                log("%3d/%d  %-6s %s%s" % (done[0], total, sym, what, eta))
        return sym, res

    if src["source"] == "alpaca" and total > 1:
        if log:
            log("Downloading %d symbols from Alpaca, %d at a time. The first run takes a few minutes; "
                "finished months are cached for good." % (total, ALPACA_WORKERS))
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=ALPACA_WORKERS) as pool:
            return list(pool.map(lambda x: one(*x), enumerate(symbols)))
    return [one(i, s) for i, s in enumerate(symbols)]


def summarize(trades, cfg):
    taken = apply_day_limits(trades, cfg)
    s = stats(taken)
    a = stats(trades)
    return {"with_limits": s, "all_signals": a, "verdict": verdict(s), "verdict_all": verdict(a), "trades": taken}


def run(symbols, rule, cfg, src, keep=None, split=False, log=None, need_market=False):
    keep = keep or (lambda t: True)
    signals, sessions, skipped, dates = collect(symbols, rule, cfg, src, log, need_market)
    chosen = [t for t in signals if keep(t)]
    doc = {"rule": rule, "config": {k: cfg[k] for k in DEFAULTS}, "source": src["label"],
           "symbols": len(symbols) - len(skipped), "sessions": sessions, "skipped": skipped,
           "first_day": dates[0] if dates else None, "last_day": dates[-1] if dates else None,
           "_signals": signals}
    doc.update(summarize(chosen, cfg))
    if split and dates:
        cut = dates[len(dates) // 2]
        first = [t for t in chosen if t["date"] < cut]
        second = [t for t in chosen if t["date"] >= cut]
        doc["split"] = {"cut": cut,
                        "in_sample": dict(summarize(first, cfg), days="%s to %s" % (dates[0], dates[len(dates) // 2 - 1])),
                        "out_of_sample": dict(summarize(second, cfg), days="%s to %s" % (cut, dates[-1]))}
        doc["verdict"] = doc["split"]["out_of_sample"]["verdict"]
    return doc


# ---------------------------------------------------------------- explore (first half only)

BUCKETS = {
    "rvol": [("under 1", None, 1.0), ("1 to 1.5", 1.0, 1.5), ("1.5 to 2", 1.5, 2.0), ("2 and up", 2.0, None)],
    "gap_pct": [("under -1 %", None, -1.0), ("-1 to 0 %", -1.0, 0.0), ("0 to +1 %", 0.0, 1.0),
                ("+1 to +3 %", 1.0, 3.0), ("+3 % and up", 3.0, None)],
}


def explore(signals, dates):
    """Every signal of the FIRST half of the days, grouped by feature. No day limits:
    more trades per group. The second half is never shown here."""
    cut = dates[len(dates) // 2] if dates else ""
    first = [t for t in signals if t["date"] < cut]
    rows = []
    for feat, groups in BUCKETS.items():
        for label, lo, hi in groups:
            g = [t for t in first if t[feat] is not None and (lo is None or t[feat] >= lo) and (hi is None or t[feat] < hi)]
            rows.append(dict(stats(g), feature=feat, group=label))
    for label, val in (("SPY up at the signal", True), ("SPY down at the signal", False)):
        g = [t for t in first if t["market_up"] is val]
        rows.append(dict(stats(g), feature="market", group=label))
    return {"cut": cut, "days": "%s to %s" % (dates[0], dates[len(dates) // 2 - 1]) if dates else "-",
            "signals": len(first), "rows": rows}


# ---------------------------------------------------------------- output

def money(x):
    return ("-$" if x < 0 else "$") + "{:,.2f}".format(abs(x))


def block(title, s, cfg):
    if not s.get("trades"):
        return ["%s: no trades" % title]
    return ["%s: %d trades on %d days (%.1f a day)   target %d  stop %d  flat at %s %d" % (
                title, s["trades"], s["days"], s["per_day"], s["target"], s["stop"], cfg["flat_by"], s["flat"]),
            "  win rate %.1f %%   mean %+.3fR   median %+.3fR   95 %% interval %+.3fR to %+.3fR" % (
                100 * s["win_rate"], s["mean_R"], s["median_R"], s["ci_low"], s["ci_high"]),
            "  P&L %s total, %s a trade   profit factor %s   worst drawdown %s" % (
                money(s["total_pnl"]), money(s["avg_pnl"]),
                "%.2f" % s["profit_factor"] if s["profit_factor"] is not None else "-", money(s["max_drawdown"]))]


def describe(doc, filt_label=""):
    r, s, a, c = doc["rule"], doc["with_limits"], doc["all_signals"], doc["config"]
    out = ["BACKTEST  opening-range breakout, %d-minute range, stop %s, target %gR%s" % (
        r["or_minutes"], r["stop_rule"], r["target_r"], ("   filter: " + filt_label) if filt_label else ""),
        "%d stocks, %d sessions (%s to %s), %s" % (
            doc["symbols"], doc["sessions"], doc["first_day"] or "-", doc["last_day"] or "-", doc["source"]),
        "Fills: next bar's open, %g bps slippage per side, $%g commission per share. Stop before target."
        % (r["slippage_bps"], r["commission"]), ""]
    if not a.get("trades"):
        out.append("No trades. " + doc["verdict"] + ".")
        return out
    limits = "max %s trades a day, stop after %s lost, %s risk per trade" % (
        c["max_trades_per_day"], money(float(c["max_daily_loss_usd"])), money(float(c["max_loss_per_trade_usd"])))
    if "split" in doc:
        sp = doc["split"]
        out.append("Split at %s. Choose filters on the first half; judge on the second. (%s)" % (sp["cut"], limits))
        out += block("IN-SAMPLE  %s" % sp["in_sample"]["days"], sp["in_sample"]["with_limits"], c)
        out += block("OUT-OF-SAMPLE  %s" % sp["out_of_sample"]["days"], sp["out_of_sample"]["with_limits"], c)
        out.append("")
        out.append("VERDICT (out-of-sample only): " + doc["verdict"] + ".")
    else:
        out += block("With your limits (%s)" % limits, s, c)
        out.append("Every signal, no day limits: %d trades, mean %+.3fR (%+.3f to %+.3f), P&L %s" % (
            a["trades"], a["mean_R"], a["ci_low"], a["ci_high"], money(a["total_pnl"])))
        out.append("")
        out.append("VERDICT with your limits: " + doc["verdict"] + ".")
        out.append("VERDICT for the rule (every signal, %d trades): %s." % (a["trades"], doc["verdict_all"]))
        if doc["verdict"] != doc["verdict_all"]:
            out.append("The two differ: the rule's verdict rests on far more trades; your limits pick the "
                       "first %s signals of each day." % c["max_trades_per_day"])
    out.append("Today's big names give any long rule a slight tailwind. Paper trade it before any real money. "
               "Not financial advice.")
    if doc["skipped"]:
        out.append("Skipped %d: %s" % (len(doc["skipped"]), "; ".join(
            "%s (%s)" % (x["symbol"], x["why"]) for x in doc["skipped"][:5])))
    by = {}
    for t in doc["trades"]:
        by.setdefault(t["symbol"], []).append(t["pnl"])
    if len(by) > 1 and "split" not in doc:
        rank = sorted(((sum(v), k, len(v)) for k, v in by.items()))
        out.append("Worst: " + ", ".join("%s %s (%d)" % (k, money(p), n) for p, k, n in rank[:3]) +
                   "   Best: " + ", ".join("%s %s (%d)" % (k, money(p), n) for p, k, n in rank[::-1][:3]))
    return out


def describe_explore(ex):
    out = ["EXPLORE  first half only (%s), every signal, no day limits: %d signals" % (ex["days"], ex["signals"]),
           "%-8s %-22s %7s  %8s  %-21s" % ("feature", "group", "trades", "mean R", "95 % interval")]
    for r in ex["rows"]:
        if not r.get("trades"):
            out.append("%-8s %-22s %7d" % (r["feature"], r["group"], 0))
            continue
        ci = "%+.3f to %+.3f" % (r["ci_low"], r["ci_high"]) if r["trades"] > 1 else "-"
        out.append("%-8s %-22s %7d  %+8.3f  %s" % (r["feature"], r["group"], r["trades"], r["mean_R"], ci))
    out.append("")
    out.append("The second half (from %s) is not shown here, on purpose. Pick at most one or two filters that "
               "make sense, then judge them on it: backtest.py --split --min-rvol X (or --min-gap, --market)."
               % ex["cut"])
    return out


GRID_STOPS = ("or_mid", "or_low")
GRID_TARGETS = (1.0, 1.5, 2.0, 3.0)


def grid(symbols, base, cfg, src, keep=None, split=False, log=None, need_market=False):
    rows = []
    for stop in GRID_STOPS:
        for tgt in GRID_TARGETS:
            rule = dict(base, stop_rule=stop, target_r=tgt)
            doc = run(symbols, rule, cfg, src, keep, split, log, need_market)
            s = doc["split"]["out_of_sample"]["with_limits"] if split and "split" in doc else doc["with_limits"]
            rows.append({"stop_rule": stop, "target_r": tgt, "trades": s.get("trades", 0),
                         "mean_R": s.get("mean_R"), "ci_low": s.get("ci_low"), "ci_high": s.get("ci_high"),
                         "total_pnl": s.get("total_pnl"), "verdict": doc["verdict"]})
            src = dict(src, pause=None)                     # cached after the first pass
            log = None
    return rows


def describe_grid(rows, split=False):
    out = ["GRID  same stocks and days, your limits, realistic fills%s" % ("   (out-of-sample half)" if split else ""),
           "%-7s %-6s %6s  %8s  %-21s  %11s" % ("stop", "target", "trades", "mean R", "95 % interval", "P&L")]
    for r in rows:
        if not r["trades"]:
            out.append("%-7s %-6s %6d  no trades" % (r["stop_rule"], "%gR" % r["target_r"], 0))
            continue
        out.append("%-7s %-6s %6d  %+8.3f  %+.3f to %+.3f      %11s" % (
            r["stop_rule"], "%gR" % r["target_r"], r["trades"], r["mean_R"], r["ci_low"], r["ci_high"],
            money(r["total_pnl"])))
    ok = [r for r in rows if r["trades"] and r["ci_low"] is not None and r["ci_low"] > 0]
    out.append("")
    out.append("%d of %d variants show an edge (whole interval above zero). With %d tries, one can look good "
               "by luck: trust a rule only if it holds across its neighbours and again on new data."
               % (len(ok), len(rows), len(rows)))
    return out


# ---------------------------------------------------------------- CLI

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    who = ap.add_mutually_exclusive_group()
    who.add_argument("--symbols", nargs="+", help="default: %d liquid US stocks" % len(DEFAULT_SYMBOLS))
    who.add_argument("--watchlist", nargs="?", const="trader/watchlist.json", help="the scanner's hits")
    ap.add_argument("--or-minutes", type=int, default=15)
    ap.add_argument("--stop", default="or_mid", choices=["or_mid", "or_low"])
    ap.add_argument("--target", type=float, default=1.0, help="target in R (default 1)")
    ap.add_argument("--slippage-bps", type=float, default=5.0, help="per side, in basis points (default 5)")
    ap.add_argument("--commission", type=float, default=0.0, help="dollars per share and side (default 0)")
    ap.add_argument("--min-rvol", type=float, help="opening-range volume vs its 20-day average, at least")
    ap.add_argument("--min-gap", type=float, help="gap from yesterday's close, percent, at least")
    ap.add_argument("--max-gap", type=float, help="gap from yesterday's close, percent, at most")
    ap.add_argument("--market", action="store_true", help="only when SPY is above its open at the signal")
    ap.add_argument("--split", action="store_true", help="judge on the second half of the days only")
    ap.add_argument("--explore", action="store_true", help="signals by rvol, gap and market, first half only")
    ap.add_argument("--grid", action="store_true", help="both stops x targets 1, 1.5, 2, 3R")
    ap.add_argument("--source", choices=["auto", "alpaca", "yahoo"], default="auto",
                    help="auto: Alpaca when its keys are set, else Yahoo")
    ap.add_argument("--years", type=float, default=2.0, help="Alpaca: years of history (default 2)")
    ap.add_argument("--start", help="Alpaca: first day, YYYY-MM-DD (instead of --years)")
    ap.add_argument("--end", help="Alpaca: last day, YYYY-MM-DD (default yesterday)")
    ap.add_argument("--feed", default="sip", choices=["sip", "iex"], help="Alpaca feed (default sip)")
    ap.add_argument("--config", default="trader/config.json")
    ap.add_argument("--offline", metavar="DIR", help="folder with <SYM>-60d5m.json(.gz), Yahoo format")
    ap.add_argument("--cache-dir", default="trader/state/backtest-cache")
    ap.add_argument("--write", nargs="?", const="trader/backtest.json", help="save the report as JSON")
    ap.add_argument("--csv", help="save every trade (with your limits) as CSV")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--quiet", action="store_true", help="no progress lines")
    a = ap.parse_args(argv)
    try:
        if a.or_minutes <= 0 or a.or_minutes % 5:
            raise BTError("--or-minutes must be a multiple of 5")
        if a.target <= 0 or a.slippage_bps < 0 or a.commission < 0:
            raise BTError("--target must be above 0; slippage and commission at least 0")
        if a.years <= 0 or a.years > 10:
            raise BTError("--years is between 0 and 10 (Alpaca's data starts in 2016)")
        symbols = [s.upper() for s in a.symbols] if a.symbols else \
            watchlist_symbols(a.watchlist) if a.watchlist else list(DEFAULT_SYMBOLS)
        cfg = read_config(a.config)
        keys = cfg.pop("_alpaca")
        source = "yahoo" if a.offline else a.source
        if source == "auto":
            source = "alpaca" if keys else "yahoo"
        if source == "alpaca" and not keys:
            raise BTError("--source alpaca needs ALPACA_API_KEY and ALPACA_SECRET_KEY (a free paper account works)")
        today = datetime.date.today()
        end = a.end or (today - datetime.timedelta(days=1)).isoformat()
        start = a.start or (today - datetime.timedelta(days=int(365.25 * a.years))).isoformat()
        src = {"source": source, "offline": a.offline, "cache_dir": None if a.offline else a.cache_dir,
               "today": today.isoformat(), "keys": keys, "start": start, "end": end, "feed": a.feed,
               "pause": None if a.offline or source == "alpaca" else tm.PAUSE,
               "limiter": RateLimiter(ALPACA_RATE), "stats": {},
               "label": ("Alpaca %s 5-minute bars, split-adjusted" % a.feed.upper()) if source == "alpaca"
               else "Yahoo 5-minute bars (60 days)"}
        keep, label = make_filter(a)
        need_market = a.market
        log = None if (a.quiet or a.json) else (lambda line: print(line, file=sys.stderr, flush=True))
        rule = {"setup": "orb", "or_minutes": a.or_minutes, "stop_rule": a.stop, "target_r": a.target,
                "slippage_bps": a.slippage_bps, "commission": a.commission}
        if a.explore:
            signals, _, _, dates = collect(symbols, rule, cfg, src, log, need_market="soft")
            ex = explore(signals, dates)
            print(json.dumps(ex, indent=2) if a.json else "\n".join(describe_explore(ex)))
            return 0
        if a.grid:
            rows = grid(symbols, rule, cfg, src, keep, a.split, log, need_market)
            print(json.dumps(rows, indent=2) if a.json else "\n".join(describe_grid(rows, a.split)))
            return 0
        doc = run(symbols, rule, cfg, src, keep, a.split, log, need_market)
        doc["filter"] = label
    except BTError as e:
        print("backtest.py: %s" % e, file=sys.stderr)
        return 1
    public = {k: v for k, v in doc.items() if not k.startswith("_")}
    if a.write:
        d = os.path.dirname(a.write)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(a.write, "w", encoding="utf-8") as fh:
            json.dump(public, fh, indent=2)
            fh.write("\n")
    if a.csv:
        cols = ["date", "symbol", "signal", "entry", "stop", "target", "qty", "outcome", "exit", "pnl", "R",
                "rvol", "gap_pct", "market_up"]
        with open(a.csv, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
            w.writeheader()
            for t in doc["trades"]:
                w.writerow(t)
    print(json.dumps(public, indent=2) if a.json else "\n".join(describe(doc, label)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
