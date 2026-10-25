"""trader-timemachine/backtest.py: realistic fills, costs and your day limits.

Hand-built sessions check each rule exactly; the saved TSLA bars check the
whole run end to end."""

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

from _load import FIX, ROOT, load

sys.path.insert(0, os.path.join(ROOT, "trader-timemachine"))
bt = load("trader-timemachine", "backtest")
TMDIR = os.path.join(FIX, "trader-timemachine")
CFG = dict(bt.DEFAULTS)
RULE = {"setup": "orb", "or_minutes": 15, "stop_rule": "or_mid", "target_r": 1.0, "slippage_bps": 0.0,
        "commission": 0.0}


def bar(o, h, l, c):
    return {"o": o, "h": h, "l": l, "c": c, "v": 1000}


def session(**at):
    """78 quiet bars around 100.5 after an opening range of 99 to 101; at={slot: bar}."""
    D = [bar(100, 101, 99, 100) for _ in range(3)] + [bar(100.5, 100.6, 100.4, 100.5) for _ in range(75)]
    for slot, b in at.items():
        D[int(slot[1:])] = b
    return D


def day(**at):
    return bt.trade_day("XYZ", "2026-10-19", session(**at), RULE, CFG)


class OneDay(unittest.TestCase):
    def test_fill_is_the_next_open_not_the_signal_close(self):
        t = day(s3=bar(100.5, 101.6, 100.4, 101.5), s4=bar(101.8, 101.9, 101.7, 101.8))
        self.assertEqual(t["signal"], "09:50")
        self.assertEqual(t["entry"], 101.8)                  # the 9:50 bar's open, not 101.5
        self.assertEqual(t["stop"], 100.0)                   # midpoint of 99 to 101

    def test_target(self):
        t = day(s3=bar(100.5, 101.6, 100.4, 101.5), s4=bar(101, 101.2, 100.9, 101.1),
                s5=bar(101.5, 102.5, 101.4, 102.4))
        self.assertEqual((t["outcome"], t["target"], t["exit"]), ("target", 102.0, 102.0))
        self.assertAlmostEqual(t["R"], 1.0, places=2)
        self.assertEqual(t["qty"], 39)                       # $50 risk / $1.00 = 50, capped by $4,000 / $101

    def test_a_bar_touching_both_is_the_stop(self):
        t = day(s3=bar(100.5, 101.6, 100.4, 101.5), s4=bar(101, 101.2, 100.9, 101.1),
                s5=bar(101, 103, 99.5, 101))
        self.assertEqual(t["outcome"], "stop")
        self.assertAlmostEqual(t["R"], -1.0, places=2)

    def test_a_gap_through_the_stop_fills_worse(self):
        t = day(s3=bar(100.5, 101.6, 100.4, 101.5), s4=bar(101, 101.2, 100.9, 101.1),
                s5=bar(99.0, 99.2, 98.5, 99.0))
        self.assertEqual((t["outcome"], t["exit"]), ("stop", 99.0))
        self.assertAlmostEqual(t["R"], -2.0, places=2)

    def test_slippage_and_commission_cost_money(self):
        rule = dict(RULE, slippage_bps=10, commission=0.01)
        t = bt.trade_day("XYZ", "d", session(s3=bar(100.5, 101.6, 100.4, 101.5), s4=bar(101, 101.2, 100.9, 101.1),
                                             s5=bar(101.5, 102.5, 101.4, 102.4)), rule, CFG)
        self.assertLess(t["R"], 1.0)

    def test_flat_by_the_close(self):
        t = day(s3=bar(100.5, 101.6, 100.4, 101.5), s4=bar(101, 101.2, 100.9, 101.1))
        self.assertEqual((t["outcome"], t["exit_slot"]), ("flat", 76))   # the 15:50 bar, out at 15:55

    def test_opening_through_the_stop_is_no_trade(self):
        self.assertIsNone(day(s3=bar(100.5, 101.6, 100.4, 101.5), s4=bar(99.5, 99.6, 99.4, 99.5)))

    def test_entry_window(self):
        late = {"s%d" % 72: bar(100.5, 101.6, 100.4, 101.5)}             # closes 15:35, after 15:30
        self.assertIsNone(day(**late))
        self.assertIsNone(day())                                          # never closes above the range

    def test_tiny_risk_is_skipped(self):
        D = [bar(100, 100.02, 99.98, 100) for _ in range(3)] + [bar(100.01, 100.02, 100.0, 100.01)] * 75
        D[3] = bar(100.01, 100.05, 100.0, 100.04)
        self.assertIsNone(bt.trade_day("XYZ", "d", D, RULE, CFG))


class Limits(unittest.TestCase):
    def t(self, sym, entry, exit_, pnl, d="2026-10-19"):
        return {"symbol": sym, "date": d, "entry_slot": entry, "exit_slot": exit_, "pnl": pnl, "R": pnl / 50.0,
                "outcome": "stop" if pnl < 0 else "target"}

    def test_max_trades_a_day(self):
        trades = [self.t("S%d" % i, 4 + i, 70, 10) for i in range(5)]
        self.assertEqual([x["symbol"] for x in bt.apply_day_limits(trades, CFG)], ["S0", "S1", "S2"])

    def test_no_new_trade_after_the_daily_loss(self):
        cfg = dict(CFG, max_daily_loss_usd=100, max_trades_per_day=10)
        trades = [self.t("A", 4, 5, -50), self.t("B", 6, 7, -50), self.t("C", 8, 9, 50),
                  self.t("D", 4, 70, -50, d="2026-10-20")]
        self.assertEqual([x["symbol"] for x in bt.apply_day_limits(trades, cfg)], ["A", "B", "D"])


class Verdict(unittest.TestCase):
    def trades(self, rs):
        return [{"symbol": "X", "date": "2026-10-%02d" % (i % 28 + 1), "entry_slot": 4, "exit_slot": 5,
                 "pnl": r * 50, "R": r, "outcome": "target" if r > 0 else "stop"} for i, r in enumerate(rs)]

    def test_three_answers(self):
        self.assertIn("an edge", bt.verdict(bt.stats(self.trades([1, 1, -1, 1] * 25))))
        self.assertIn("loses money", bt.verdict(bt.stats(self.trades([-1, -1, 1, -1] * 25))))
        self.assertIn("no evidence", bt.verdict(bt.stats(self.trades([1, -1] * 25))))
        self.assertIn("too few", bt.verdict(bt.stats(self.trades([1, 1, 1]))))


class TslaEndToEnd(unittest.TestCase):
    def run_cli(self, *extra):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = bt.main(["--symbols", "TSLA", "--offline", TMDIR, "--config", "/nonexistent", "--quiet"]
                           + list(extra))
        return code, out.getvalue(), err.getvalue()

    def test_the_saved_60_days(self):
        code, out, err = self.run_cli("--json")
        self.assertEqual(code, 0, err)
        doc = json.loads(out)
        s = doc["with_limits"]
        self.assertEqual((doc["sessions"], s["trades"], s["target"], s["stop"], s["flat"]), (59, 33, 10, 16, 7))
        self.assertAlmostEqual(s["mean_R"], -0.232, places=3)
        self.assertIn("no evidence", doc["verdict"])

    def test_report_csv_and_grid(self):
        d = tempfile.mkdtemp()
        path, csv_path = os.path.join(d, "bt.json"), os.path.join(d, "t.csv")
        code, out, err = self.run_cli("--write", path, "--csv", csv_path)
        self.assertEqual(code, 0, err)
        self.assertIn("VERDICT with your limits:", out)
        self.assertIn("VERDICT for the rule (every signal, 33 trades)", out)
        with open(csv_path) as fh:
            self.assertEqual(len(fh.read().strip().splitlines()), 34)       # header + 33 trades
        code, out, err = self.run_cli("--grid")
        self.assertEqual(code, 0, err)
        self.assertEqual(sum(1 for l in out.splitlines() if l.startswith(("or_mid", "or_low"))), 8)
        self.assertIn("by luck", out)

    def test_bad_input(self):
        self.assertEqual(self.run_cli("--target", "0")[0], 1)
        self.assertEqual(self.run_cli("--or-minutes", "7")[0], 1)


if __name__ == "__main__":
    unittest.main()
