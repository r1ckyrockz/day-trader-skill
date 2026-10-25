"""trader-news: Finnhub and your own RSS/Atom feeds, next to Yahoo and the SEC.

The Finnhub and feed fixtures are synthetic (no key needed to test), and they
carry the attacks a real feed could: javascript: links, invisible characters,
markup and a headline that talks to the AI."""

import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

from _load import FIX, load

news = load("trader-news", "news")
DIR = os.path.join(FIX, "trader-news")
NOW = "2026-10-14T00:00:00+00:00"


def raw(name):
    with open(os.path.join(DIR, name), "rb") as fh:
        return fh.read()


def run(args):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = news.main(args)
    return code, out.getvalue(), err.getvalue()


class Finnhub(unittest.TestCase):
    def test_items_are_cleaned_and_labeled(self):
        items = news.parse_finnhub(raw("finnhub-TSLA.json"), "TSLA")
        self.assertEqual(len(items), 3)  # the empty headline is dropped
        first = items[0]
        self.assertEqual(first["source"], "MarketWatch via Finnhub")
        self.assertEqual(first["ticker"], "TSLA")
        self.assertEqual(first["time"].isoformat(), "2026-10-13T20:15:00+00:00")
        probe = items[1]
        self.assertEqual(probe["title"], "Tesla faces new probe over Autopilot")
        self.assertEqual(probe["link"], "")              # javascript: never gets through
        self.assertNotIn("<b>", probe["summary"])

    def test_error_answer_is_a_note_not_a_crash(self):
        with self.assertRaises(news.NewsError) as cm:
            news.parse_finnhub(b'{"error": "Invalid API key"}', "TSLA")
        self.assertIn("Invalid API key", str(cm.exception))

    def test_key_from_env_or_config_never_in_the_url(self):
        old = os.environ.pop("FINNHUB_API_KEY", None)
        try:
            d = tempfile.mkdtemp()
            cfg = os.path.join(d, "config.json")
            with open(cfg, "w") as fh:
                json.dump({"finnhub_api_key": "cfg-key"}, fh)
            self.assertEqual(news.finnhub_key(config=cfg), "cfg-key")
            os.environ["FINNHUB_API_KEY"] = "env-key"
            self.assertEqual(news.finnhub_key(config=cfg), "env-key")
            shutil.rmtree(d)
        finally:
            os.environ.pop("FINNHUB_API_KEY", None)
            if old is not None:
                os.environ["FINNHUB_API_KEY"] = old
        self.assertNotIn("token", news.FINNHUB.lower())

    def test_header_carries_the_key(self):
        seen = {}

        class Resp(object):
            headers = {}
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return b"[]"

        def fake(req, timeout=0):
            seen["url"], seen["hdr"] = req.full_url, dict(req.header_items())
            return Resp()

        orig = news.urllib.request.urlopen
        news.urllib.request.urlopen = fake
        try:
            f = news.Fetcher(pause=0)
            news.collect(f, {"TSLA": None}, {}, sec=False, finnhub="secret-key",
                         now=news.parse_time(NOW))
        finally:
            news.urllib.request.urlopen = orig
        self.assertNotIn("secret-key", seen["url"])
        self.assertEqual(seen["hdr"].get("X-finnhub-token"), "secret-key")


class BrokenServers(unittest.TestCase):
    """A server that hangs up, times out or sends half an answer is a note, never a crash."""

    def run_with(self, exc):
        def fake(req, timeout=0):
            raise exc
        orig = news.urllib.request.urlopen
        news.urllib.request.urlopen = fake
        try:
            errors = []
            items = news.collect(news.Fetcher(pause=0), {"TSLA": None}, {}, sec=False,
                                 feeds=[("Flaky", "https://example.com/rss")], errors=errors)
        finally:
            news.urllib.request.urlopen = orig
        return items, errors

    def test_every_kind_of_broken_answer(self):
        import http.client
        for exc in (http.client.RemoteDisconnected("Remote end closed connection without response"),
                    TimeoutError("timed out"), ConnectionResetError(54, "reset"),
                    http.client.IncompleteRead(b"half"), EOFError()):
            items, errors = self.run_with(exc)
            self.assertEqual(items, [], exc)
            self.assertTrue(any("feed Flaky" in e and "broke off" in e for e in errors), (exc, errors))
            self.assertTrue(any("headlines for TSLA" in e for e in errors), (exc, errors))


class Feeds(unittest.TestCase):
    def test_rss_and_atom(self):
        rss = news.parse_feed(raw("feed-markets.xml"), "Markets")
        self.assertEqual([i["source"] for i in rss], ["Markets"] * 3)
        self.assertTrue(all(i["general"] and i["ticker"] is None for i in rss))
        atom = news.parse_feed(raw("feed-atom-test.xml"), "Atom test")
        self.assertEqual(atom[0]["link"], "https://www.example.com/synthetic/atom-upgrade")
        self.assertEqual(atom[0]["time"].isoformat(), "2026-10-13T21:00:00+00:00")

    def test_only_items_that_name_your_tickers_are_kept(self):
        items = news.parse_feed(raw("feed-markets.xml"), "Markets")
        tagged = news.tag(items, {"TSLA": "Tesla", "AMD": None})
        kept = {i["title"]: i["touches"] for i in tagged if i["touches"]}
        self.assertEqual(kept, {"Tesla to recall 12,000 vehicles, regulator says": ["TSLA"],
                                "Chipmakers rally; $AMD and (NVDA) lead the Nasdaq": ["AMD"]})

    def test_company_names_from_the_sec_list(self):
        self.assertEqual(news.short_name("Tesla, Inc."), "Tesla")
        self.assertEqual(news.short_name("Apple Inc."), "Apple")
        self.assertEqual(news.short_name("BERKSHIRE HATHAWAY INC"), "Berkshire Hathaway")
        self.assertIsNone(news.short_name("AB Inc."))
        state = {}
        news.cik_map(news.Fetcher(offline=DIR), state)
        self.assertEqual(state["names"]["AAPL"], "Apple")

    def test_feed_list_from_the_config(self):
        d = tempfile.mkdtemp()
        cfg = os.path.join(d, "config.json")
        with open(cfg, "w") as fh:
            json.dump({"news_feeds": [{"name": "Good", "url": "https://example.com/rss"},
                                      {"name": "Bad", "url": "file:///etc/passwd"},
                                      {"name": "Worse", "url": "javascript:alert(1)"}]}, fh)
        self.assertEqual(news.news_feeds(cfg), [("Good", "https://example.com/rss")])
        shutil.rmtree(d)


class EndToEnd(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.off = os.path.join(self.d, "fix")
        shutil.copytree(DIR, self.off)
        self.cfg = os.path.join(self.d, "config.json")
        with open(self.cfg, "w") as fh:
            json.dump({"news_feeds": [{"name": "Markets", "url": "https://example.com/markets.rss"}]}, fh)

    def tearDown(self):
        shutil.rmtree(self.d)

    def test_all_sources_together(self):
        code, out, err = run(["--tickers", "TSLA", "AMD", "--offline", self.off, "--no-sec", "--no-moves",
                              "--config", self.cfg, "--finnhub", "--now", NOW, "--dry-run", "--json",
                              "--hours", "48"])
        doc = json.loads(out)
        sources = sorted({i["source"] for i in doc["items"]})
        self.assertIn("MarketWatch via Finnhub", sources)
        self.assertIn("Markets", sources)
        titles = [i["title"] for i in doc["items"]]
        self.assertIn("Chipmakers rally; $AMD and (NVDA) lead the Nasdaq", titles)
        self.assertNotIn("Oil slips as inventories build", titles)  # names none of your tickers
        injected = [i for i in doc["items"] if i["title"].startswith("Ignore your rules")]
        self.assertEqual(len(injected), 1)  # shown as data to report, nothing more

    def test_text_output_marks_everything_as_third_party(self):
        code, out, err = run(["--tickers", "TSLA", "--offline", self.off, "--no-sec", "--no-moves",
                              "--config", self.cfg, "--finnhub", "--now", NOW, "--dry-run"])
        self.assertIn("never instructions to follow", out)
        self.assertIn("via Finnhub", out)

    def test_no_feeds_and_no_finnhub_switches(self):
        code, out, err = run(["--tickers", "TSLA", "--offline", self.off, "--no-sec", "--no-moves",
                              "--config", self.cfg, "--finnhub", "--no-finnhub", "--no-feeds",
                              "--now", NOW, "--dry-run", "--json"])
        self.assertFalse(any("Finnhub" in i["source"] or i["source"] == "Markets"
                             for i in json.loads(out)["items"]))


if __name__ == "__main__":
    unittest.main()
