"""Stale data gives no signal: live runs check how old Yahoo's last trade is."""

import datetime
import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

from _load import FIX, load

entry = load("trader-entry", "entry")
TSLA = os.path.join(FIX, "trader-entry", "TSLA-2026-10-19-5m.json")


def chart():
    return entry.get_chart("TSLA", "5m", offline=TSLA)


def later(c, minutes):
    return entry.utc_of(c["data_time"]) + datetime.timedelta(minutes=minutes)


class Freshness(unittest.TestCase):
    def test_fresh_data_is_fine(self):
        c = chart()
        r = entry.evaluate(c, later(c, 2), max_data_age_min=5)
        self.assertEqual(r["status"], "triggered")          # the 10:10 bar closed above the range
        self.assertEqual(r["data_age_min"], 2.0)

    def test_old_data_during_the_session_is_stale_and_hides_the_trigger(self):
        c = chart()
        r = entry.evaluate(c, later(c, 20), max_data_age_min=5)
        self.assertEqual(r["status"], "stale")
        self.assertIsNone(r["trigger"])
        self.assertEqual(r["stale_trigger"]["price"], 364.18)
        self.assertIn("20 minutes ago", r["reason"])
        lines = entry.describe(r)
        self.assertTrue(any(l.startswith("STALE DATA") for l in lines), lines)
        self.assertFalse(any("TRIGGERED" in l for l in lines))
        self.assertEqual(entry.entry_doc(r, later(c, 20))["status"], "stale")

    def test_replays_are_never_stale(self):
        c = chart()
        r = entry.evaluate(c, later(c, 20))                  # no limit: --now / --offline
        self.assertEqual(r["status"], "triggered")

    def test_after_the_close_age_does_not_matter(self):
        c = chart()
        evening = entry.eastern_to_utc(datetime.datetime(2026, 10, 19, 17, 0))
        r = entry.evaluate(c, evening, max_data_age_min=5)
        self.assertNotEqual(r["status"], "stale")

    def test_limit_comes_from_the_config(self):
        d = tempfile.mkdtemp()
        try:
            path = os.path.join(d, "config.json")
            self.assertEqual(entry.read_config(path)["max_data_age_min"], 5)
            with open(path, "w") as fh:
                json.dump({"max_data_age_min": 2}, fh)
            self.assertEqual(entry.read_config(path)["max_data_age_min"], 2.0)
            with open(path, "w") as fh:
                json.dump({"max_data_age_min": "soon"}, fh)
            with self.assertRaises(entry.EntryError):
                entry.read_config(path)
        finally:
            shutil.rmtree(d)

    def test_check_with_now_is_a_replay(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = entry.main(["check", "TSLA", "--offline", TSLA, "--now", "2026-10-19 10:15",
                               "--config", os.path.join(FIX, "none.json")])
        self.assertEqual(code, 0, err.getvalue())
        self.assertIn("TRIGGERED", out.getvalue())


if __name__ == "__main__":
    unittest.main()
