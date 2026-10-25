"""The live price is checked before a buy: too far from the plan, or at the stop, is a no."""

import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

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


def quiet(fn, *a):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        return fn(*a), out.getvalue(), err.getvalue()


def order():
    with open(ORDERS_PATH) as fh:
        return json.load(fh)["orders"][0]


class CheckQuote(unittest.TestCase):
    def test_close_to_the_plan_is_fine(self):
        ok, why, drift = ticket.check_quote(order(), "364.90", {})
        self.assertTrue(ok, why)
        self.assertEqual(drift, 0.2)

    def test_too_far_above_is_chasing(self):
        ok, why, drift = ticket.check_quote(order(), "369.00", {})
        self.assertFalse(ok)
        self.assertIn("+1.32%", why)

    def test_at_the_stop_is_a_failed_setup(self):
        ok, why, _ = ticket.check_quote(order(), "358.00", {"max_price_drift_pct": 5})
        self.assertFalse(ok)
        self.assertIn("under the stop", why)

    def test_limit_from_the_config_and_junk_input(self):
        self.assertTrue(ticket.check_quote(order(), "369.00", {"max_price_drift_pct": 2})[0])
        self.assertFalse(ticket.check_quote(order(), "abc", {})[0])
        self.assertFalse(ticket.check_quote(order(), "-5", {})[0])

    def test_sells_and_ibkr_need_no_quote(self):
        self.assertTrue(ticket.needs_quote(order(), {}))
        self.assertFalse(ticket.needs_quote(dict(order(), side="sell"), {}))
        self.assertFalse(ticket.needs_quote(order(), {"broker": "ibkr"}))


class Flow(unittest.TestCase):
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
        quiet(check.main, ["--orders", self.orders, "--config", self.cfg,
                           "--portfolio", os.path.join(t, "portfolio.json"),
                           "--prices", os.path.join(self.d, "none"), "--state", self.state,
                           "--now", NOW, "--out", self.risk])

    def tearDown(self):
        shutil.rmtree(self.d)

    def approve(self, *extra):
        return quiet(ticket.main, ["approve", "--index", "1", "--reply", "yes", "--orders", self.orders,
                                   "--config", self.cfg, "--risk", self.risk, "--state", self.state,
                                   "--now", NOW] + list(extra))

    def statuses(self):
        if not os.path.exists(self.state):
            return []
        with open(self.state) as fh:
            return [r["status"] for r in json.load(fh)["orders"]]

    def test_a_buy_without_a_quote_is_not_recorded_and_the_yes_is_not_used_up(self):
        code, out, _ = self.approve()
        self.assertEqual(code, 1)
        self.assertIn("get_stock_latest_quote", out)
        self.assertNotIn("approved", self.statuses())
        code, out, _ = self.approve("--quote", "364.30")            # same yes, now with the price
        self.assertEqual(code, 0, out)
        self.assertIn("Checked the live price 364.30", out)

    def test_a_drifted_price_is_refused(self):
        code, out, _ = self.approve("--quote", "371.00")
        self.assertEqual(code, 1)
        self.assertIn("Plan again", out)
        self.assertNotIn("approved", self.statuses())

    def test_gate_blocks_an_approval_without_a_quote(self):
        self.approve("--quote", "364.30")
        with open(self.state) as fh:
            st = json.load(fh)
        st["orders"][-1]["quote"] = None                              # as an older ticket.py would write it
        with open(self.state, "w") as fh:
            json.dump(st, fh)
        ev = {"tool_name": "mcp__alpaca__place_stock_order", "tool_input": TSLA_CALL, "cwd": self.d}
        code, msg = gate.decide(ev, ticket.parse_now(NOW))
        self.assertEqual(code, 2)
        self.assertIn("no live price", msg)

    def test_gate_passes_a_checked_buy(self):
        self.approve("--quote", "364.30")
        ev = {"tool_name": "mcp__alpaca__place_stock_order", "tool_input": TSLA_CALL, "cwd": self.d}
        self.assertEqual(gate.decide(ev, ticket.parse_now(NOW)), (0, ""))


if __name__ == "__main__":
    unittest.main()
