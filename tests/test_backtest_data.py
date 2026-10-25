"""backtest.py: Alpaca data, filters, the split and explore."""

import gzip
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

from _load import FIX, ROOT, load

sys.path.insert(0, os.path.join(ROOT, "trader-timemachine"))
bt = load("trader-timemachine", "backtest")
TMDIR = os.path.join(FIX, "trader-timemachine")


class Resp(object):
    def __init__(self, doc):
        self.doc = doc

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return json.dumps(self.doc).encode("utf-8")


class Alpaca(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.calls = []
        self.orig = bt.urllib.request.urlopen

    def tearDown(self):
        bt.urllib.request.urlopen = self.orig
        shutil.rmtree(self.d)

    def fake(self, pages):
        def urlopen(req, timeout=0):
            self.calls.append((req.full_url, dict(req.header_items())))
            ans = pages[len(self.calls) - 1]
            if isinstance(ans, Exception):
                raise ans
            return Resp(ans)
        bt.urllib.request.urlopen = urlopen

    def test_times_in_summer_and_winter_and_only_the_regular_session(self):
        rows = [{"t": "2026-10-19T13:30:00Z", "o": 1, "h": 2, "l": 0.5, "c": 1.5, "v": 10},   # 9:30 EDT
                {"t": "2026-10-19T13:25:00Z", "o": 1, "h": 1, "l": 1, "c": 1, "v": 1},        # 9:25: pre-market
                {"t": "2026-10-19T20:00:00Z", "o": 1, "h": 1, "l": 1, "c": 1, "v": 1},        # 16:00: after hours
                {"t": "2026-12-01T14:30:00Z", "o": 1, "h": 1, "l": 1, "c": 1, "v": 1},        # 9:30 EST
                {"t": "2026-12-01T14:32:00Z", "o": 1, "h": 1, "l": 1, "c": 1, "v": 1},        # off the 5-minute grid
                {"t": "garbage"}]
        bars = bt.alpaca_to_bars(rows)
        self.assertEqual([(b["date"], b["start"], b["slot"]) for b in bars],
                         [("2026-10-19", "09:30", 0), ("2026-12-01", "09:30", 0)])

    def test_pages_headers_and_cache(self):
        page1 = {"bars": {"TSLA": [{"t": "2026-10-19T13:30:00Z", "o": 1, "h": 2, "l": 1, "c": 2, "v": 5}]},
                 "next_page_token": "abc"}
        page2 = {"bars": {"TSLA": [{"t": "2026-10-19T13:35:00Z", "o": 2, "h": 3, "l": 2, "c": 3, "v": 5}]},
                 "next_page_token": None}
        self.fake([page1, page2])
        bars = bt.alpaca_bars("TSLA", ("KEYID", "SECRET"), "2026-10-01", "2026-10-24", cache_dir=self.d)
        self.assertEqual(len(bars), 2)
        self.assertEqual(len(self.calls), 2)
        url1, hdr1 = self.calls[0]
        self.assertIn("timeframe=5Min", url1)
        self.assertIn("feed=sip", url1)
        self.assertIn("adjustment=split", url1)
        self.assertNotIn("KEYID", url1)
        self.assertNotIn("SECRET", url1)
        self.assertEqual(hdr1.get("Apca-api-key-id"), "KEYID")
        self.assertIn("page_token=abc", self.calls[1][0])
        again = bt.alpaca_bars("TSLA", ("KEYID", "SECRET"), "2026-10-01", "2026-10-24", cache_dir=self.d)
        self.assertEqual(again, bars)
        self.assertEqual(len(self.calls), 2)                     # read from the cache

    def test_month_chunks_and_which_are_final(self):
        import datetime
        today = datetime.date(2026, 10, 25)
        ch = bt.month_chunks("2026-08-15", "2026-10-24", today)
        self.assertEqual([(a, b, n, f) for a, b, n, f in ch], [
            ("2026-08-15", "2026-08-31", "2026-08-15_2026-08-31", False),   # starts mid-month
            ("2026-09-01", "2026-09-30", "2026-09", True),                    # whole and past: kept for good
            ("2026-10-01", "2026-10-24", "2026-10-01_2026-10-24", False)])    # the running month
        self.assertEqual(len(bt.month_chunks("2024-10-25", "2026-10-24", today)), 25)

    def test_finished_months_are_never_downloaded_again(self):
        page = {"bars": {"TSLA": [{"t": "2026-09-01T13:30:00Z", "o": 1, "h": 2, "l": 1, "c": 2, "v": 5}]},
                "next_page_token": None}
        self.fake([page, page])
        import datetime
        bt.alpaca_bars("TSLA", ("k", "s"), "2026-09-01", "2026-09-30", cache_dir=self.d,
                       today=datetime.date(2026, 10, 25))
        bt.alpaca_bars("TSLA", ("k", "s"), "2026-09-01", "2026-09-30", cache_dir=self.d,
                       today=datetime.date(2026, 11, 3))
        self.assertEqual(len(self.calls), 1)
        self.assertTrue(os.path.exists(os.path.join(self.d, "alpaca-TSLA-2026-09-sip.json.gz")))

    def test_rate_limiter_spaces_requests(self):
        import time
        lim = bt.RateLimiter(600)                    # one every 0.1 s
        t0 = time.monotonic()
        for _ in range(4):
            lim.wait()
        self.assertGreaterEqual(time.monotonic() - t0, 0.29)

    def test_refused_keys_are_a_plain_message(self):
        import urllib.error
        self.fake([urllib.error.HTTPError("u", 403, "Forbidden", {}, None)])
        with self.assertRaises(bt.BTError) as cm:
            bt.alpaca_bars("TSLA", ("k", "s"), "2024-01-01", "2024-01-31")
        self.assertIn("HTTP 403", str(cm.exception))

    def test_no_bars_is_a_plain_message(self):
        self.fake([{"bars": {}, "next_page_token": None}])
        with self.assertRaises(bt.BTError):
            bt.alpaca_bars("ZZZZ", ("k", "s"), "2024-01-01", "2024-01-31")

    def test_keys_from_env_then_config(self):
        saved = {k: os.environ.pop(k, None) for k in ("ALPACA_API_KEY", "ALPACA_SECRET_KEY",
                                                      "APCA_API_KEY_ID", "APCA_API_SECRET_KEY")}
        try:
            self.assertIsNone(bt.alpaca_keys({}))
            self.assertEqual(bt.alpaca_keys({"alpaca_api_key": "a", "alpaca_secret_key": "b"}), ("a", "b"))
            os.environ["ALPACA_API_KEY"], os.environ["ALPACA_SECRET_KEY"] = "x", "y"
            self.assertEqual(bt.alpaca_keys({"alpaca_api_key": "a", "alpaca_secret_key": "b"}), ("x", "y"))
        finally:
            for k, v in saved.items():
                os.environ.pop(k, None)
                if v is not None:
                    os.environ[k] = v

    def test_alpaca_without_keys_refuses(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = bt.main(["--source", "alpaca", "--symbols", "TSLA", "--config", "/nonexistent", "--quiet"])
        self.assertEqual(code, 1)
        self.assertIn("ALPACA_API_KEY", err.getvalue())


def cli(*extra):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = bt.main(["--symbols", "TSLA", "--offline", TMDIR, "--config", "/nonexistent", "--quiet"] + list(extra))
    return code, out.getvalue(), err.getvalue()


class Features(unittest.TestCase):
    def test_rvol_needs_ten_sessions_and_gap_needs_one(self):
        days = [d for d, _ in bt.load_sessions("TSLA", {"source": "yahoo", "offline": TMDIR})]
        code, out, err = cli("--json")
        for t in json.loads(out)["trades"]:
            i = days.index(t["date"])
            self.assertEqual(t["gap_pct"] is None, i == 0, t["date"])          # needs yesterday's close
            self.assertEqual(t["rvol"] is None, i < bt.RVOL_MIN_DAYS, t["date"])  # needs 10 sessions

    def test_filters_keep_only_what_passes(self):
        code, out, err = cli("--json", "--min-rvol", "1.2")
        doc = json.loads(out)
        self.assertEqual(doc["filter"], "rvol >= 1.2")
        self.assertTrue(all(t["rvol"] >= 1.2 for t in doc["trades"]))
        code, out, err = cli("--json", "--max-gap", "0")
        self.assertTrue(all(t["gap_pct"] <= 0 for t in json.loads(out)["trades"]))

    def test_market_filter_needs_spy(self):
        code, out, err = cli("--market")
        self.assertEqual(code, 1)
        self.assertIn("needs SPY", err)

    def test_market_direction_from_spy(self):
        D = [{"o": 100, "h": 101, "l": 99, "c": 100, "v": 1} for _ in range(3)] + \
            [{"o": 100.5, "h": 100.6, "l": 100.4, "c": 100.5, "v": 1} for _ in range(75)]
        D[3] = {"o": 100.5, "h": 101.6, "l": 100.4, "c": 101.5, "v": 1}
        spy_up = [{"o": 500, "h": 501, "l": 499, "c": 500.5, "v": 1} for _ in range(78)]
        spy_dn = [{"o": 500, "h": 501, "l": 499, "c": 499.5, "v": 1} for _ in range(78)]
        rule = {"or_minutes": 15, "stop_rule": "or_mid", "target_r": 1.0, "slippage_bps": 0, "commission": 0}
        self.assertTrue(bt.trade_day("X", "d", D, rule, dict(bt.DEFAULTS), {"market": spy_up})["market_up"])
        self.assertFalse(bt.trade_day("X", "d", D, rule, dict(bt.DEFAULTS), {"market": spy_dn})["market_up"])


class SplitAndExplore(unittest.TestCase):
    def test_split_halves_add_up_and_the_verdict_is_out_of_sample(self):
        code, out, err = cli("--json", "--split")
        doc = json.loads(out)
        sp = doc["split"]
        ins, oos = sp["in_sample"]["with_limits"]["trades"], sp["out_of_sample"]["with_limits"]["trades"]
        self.assertEqual(ins + oos, doc["with_limits"]["trades"])
        self.assertEqual(doc["verdict"], sp["out_of_sample"]["verdict"])
        self.assertTrue(all(t["date"] >= sp["cut"] for t in sp["out_of_sample"]["trades"]))
        code, out, err = cli("--split")
        self.assertIn("VERDICT (out-of-sample only)", out)

    def test_explore_never_shows_the_second_half(self):
        code, out, err = cli("--json", "--explore")
        ex = json.loads(out)
        code2, out2, _ = cli("--json", "--split")
        cut = json.loads(out2)["split"]["cut"]
        self.assertEqual(ex["cut"], cut)
        signals_first = len([t for t in json.loads(cli("--json")[1])["trades"] if t["date"] < cut])
        self.assertGreaterEqual(ex["signals"], signals_first)    # every signal, no day limits
        code, out, err = cli("--explore")
        self.assertIn("not shown here, on purpose", out)
        self.assertNotIn("inf", out)

    def test_grid_with_split_reports_the_second_half(self):
        code, out, err = cli("--grid", "--split")
        self.assertEqual(code, 0, err)
        self.assertIn("(out-of-sample half)", out)


if __name__ == "__main__":
    unittest.main()
