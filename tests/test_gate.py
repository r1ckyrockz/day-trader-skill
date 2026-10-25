import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load, ROOT, FIX

ticket = load("trader-broker", "ticket")
check = load("trader-risk", "check")
sys.path.insert(0, os.path.join(ROOT, "trader-broker"))
gate = load("trader-broker", "gate")

CFG_PATH = os.path.join(ROOT, "trader-budget", "config.example.json")
ORDERS_PATH = os.path.join(FIX, "trader-risk", "orders-tsla.json")
NOW = "2026-10-19 10:15"
TSLA_CALL = {"symbol": "TSLA", "side": "buy", "qty": "8", "type": "market", "time_in_force": "day",
             "order_class": "bracket", "take_profit_limit_price": "370.04", "stop_loss_stop_price": "358.32"}
TOOL = "mcp__alpaca__place_stock_order"


def quiet(fn, *a):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        return fn(*a), out.getvalue(), err.getvalue()


class Gate(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        t = os.path.join(self.d, "trader")
        os.makedirs(os.path.join(t, "state"))
        self.orders = os.path.join(t, "orders.json")
        shutil.copy(ORDERS_PATH, self.orders)
        self.cfg = os.path.join(t, "config.json")
        shutil.copy(CFG_PATH, self.cfg)
        self.risk = os.path.join(t, "state", "risk.json")
        self.state = os.path.join(t, "state", "broker.json")
        self.pf = os.path.join(t, "portfolio.json")

    def tearDown(self):
        shutil.rmtree(self.d)

    def args(self, *extra, now=NOW):
        return list(extra) + ["--orders", self.orders, "--config", self.cfg, "--risk", self.risk,
                              "--state", self.state, "--now", now]

    def risk_check(self, now=NOW):
        quiet(check.main, ["--orders", self.orders, "--config", self.cfg, "--portfolio", self.pf,
                           "--prices", os.path.join(self.d, "none"), "--state", self.state,
                           "--now", now, "--out", self.risk])

    def approve(self, reply="yes", now=NOW):
        return quiet(ticket.main, self.args("approve", "--index", "1", "--reply", reply, "--quote", "364.20",
                                            now=now))[0]

    def call(self, args=None, now=NOW, tool=TOOL):
        ev = {"tool_name": tool, "tool_input": TSLA_CALL if args is None else args, "cwd": self.d}
        return gate.decide(ev, ticket.parse_now(now))

    def statuses(self):
        with open(self.state) as fh:
            return [r["status"] for r in json.load(fh)["orders"]]

    # -- the happy path, once

    def test_exact_approved_order_goes_through_once(self):
        self.risk_check()
        self.assertEqual(self.approve(), 0)
        self.assertEqual(self.call(), (0, ""))
        self.assertEqual(self.statuses()[-1], "sent")
        code, msg = self.call()
        self.assertEqual(code, 2)
        self.assertIn("no unused yes", msg)

    def test_result_can_follow_a_sent_record(self):
        self.risk_check()
        self.approve()
        self.call()
        code, _, err = quiet(ticket.main, self.args("result", "--index", "1", "--status", "placed",
                                                    "--broker-id", "abc"))
        self.assertEqual(code, 0, err)
        self.assertEqual(self.statuses()[-1], "placed")

    # -- what it blocks

    def test_no_yes_no_order(self):
        self.risk_check()
        code, msg = self.call()
        self.assertEqual(code, 2)
        self.assertIn("no unused yes", msg)

    def test_changed_arguments_are_blocked(self):
        self.risk_check()
        self.approve()
        bigger = dict(TSLA_CALL, qty="80")
        code, msg = self.call(bigger)
        self.assertEqual(code, 2)
        self.assertIn("not the ones the user said yes to", msg)
        no_stop = {k: v for k, v in TSLA_CALL.items() if k != "stop_loss_stop_price"}
        self.assertEqual(self.call(no_stop)[0], 2)

    def test_numbers_as_numbers_still_match(self):
        self.risk_check()
        self.approve()
        as_numbers = dict(TSLA_CALL, qty=8, take_profit_limit_price=370.04, stop_loss_stop_price=358.32)
        self.assertEqual(self.call(as_numbers)[0], 0)

    def test_stale_yes_is_blocked(self):
        self.risk_check()
        self.approve()
        code, msg = self.call(now="2026-10-19 10:45")
        self.assertEqual(code, 2)
        self.assertIn("minutes old", msg)

    def test_yes_from_yesterday_does_not_count(self):
        self.risk_check(now="2026-10-16 10:15")
        self.approve(now="2026-10-16 10:15")
        self.assertEqual(self.call()[0], 2)

    def test_risk_block_after_the_yes_blocks(self):
        self.risk_check()
        self.approve()
        with open(self.risk) as fh:
            risk = json.load(fh)
        for r in risk["results"]:
            r["verdict"] = "BLOCK"
            r["reasons"] = ["daily loss limit"]
        with open(self.risk, "w") as fh:
            json.dump(risk, fh)
        code, msg = self.call()
        self.assertEqual(code, 2)
        self.assertIn("daily loss limit", msg)

    def test_missing_files_fail_closed(self):
        shutil.rmtree(os.path.join(self.d, "trader"))
        self.assertEqual(self.call()[0], 2)

    def test_account_wide_tools_are_always_blocked(self):
        for t in ("close_all_positions", "cancel_all_orders", "place_option_order", "place_crypto_order"):
            code, msg = self.call({}, tool="mcp__alpaca__" + t)
            self.assertEqual(code, 2, t)
            self.assertIn(t, msg)

    def test_gate_allow_unblocks_by_name(self):
        with open(self.cfg) as fh:
            cfg = json.load(fh)
        cfg["gate_allow"] = ["cancel_all_orders"]
        with open(self.cfg, "w") as fh:
            json.dump(cfg, fh)
        self.assertEqual(self.call({}, tool="mcp__alpaca__cancel_all_orders")[0], 0)
        self.assertEqual(self.call({}, tool="mcp__alpaca__close_all_positions")[0], 2)

    def test_read_only_tools_pass(self):
        self.assertEqual(self.call({}, tool="mcp__alpaca__get_account_info"), (0, ""))

    # -- as a real hook process

    def test_runs_as_a_hook_and_fails_closed_on_garbage(self):
        script = os.path.join(ROOT, "trader-broker", "gate.py")
        p = subprocess.run([sys.executable, script], input="not json", capture_output=True, text=True)
        self.assertEqual(p.returncode, 2)
        ev = json.dumps({"tool_name": TOOL, "tool_input": TSLA_CALL, "cwd": self.d})
        p = subprocess.run([sys.executable, script, "--now", NOW], input=ev, capture_output=True, text=True)
        self.assertEqual(p.returncode, 2)
        self.assertIn("order blocked", p.stderr)


class ReplyOnStdin(unittest.TestCase):
    """The reply travels on stdin, so nothing the user typed is ever shell syntax."""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.orders = os.path.join(self.d, "orders.json")
        shutil.copy(ORDERS_PATH, self.orders)
        self.cfg = os.path.join(self.d, "config.json")
        shutil.copy(CFG_PATH, self.cfg)
        self.risk = os.path.join(self.d, "risk.json")
        self.state = os.path.join(self.d, "broker.json")
        quiet(check.main, ["--orders", self.orders, "--config", self.cfg,
                           "--portfolio", os.path.join(self.d, "pf.json"),
                           "--prices", os.path.join(self.d, "none"), "--state", self.state,
                           "--now", NOW, "--out", self.risk])

    def tearDown(self):
        shutil.rmtree(self.d)

    def approve_via_shell(self, reply):
        script = os.path.join(ROOT, "trader-broker", "ticket.py")
        marker = os.path.join(self.d, "pwned")
        cmd = ("%s %s approve --index 1 --quote 364.20 --orders %s --config %s --risk %s --state %s --now '%s' "
               "<<'TRADER_REPLY'\n%s\nTRADER_REPLY\n" % (sys.executable, script, self.orders, self.cfg,
                                                         self.risk, self.state, NOW,
                                                         reply.replace("MARKER", marker)))
        p = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True)
        return p, os.path.exists(marker)

    def test_plain_yes_is_recorded(self):
        p, _ = self.approve_via_shell("yes")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("Recorded your yes", p.stdout)

    def test_shell_syntax_in_the_reply_is_never_run(self):
        for evil in ('yes $(touch MARKER)', 'yes `touch MARKER`', 'yes"; touch MARKER; echo "'):
            p, ran = self.approve_via_shell(evil)
            self.assertFalse(ran, evil)

    def test_overlong_reply_is_refused(self):
        p, _ = self.approve_via_shell("yes " + "a" * 3000)
        self.assertEqual(p.returncode, 1)
        self.assertIn("longer than", p.stderr)


if __name__ == "__main__":
    unittest.main()
