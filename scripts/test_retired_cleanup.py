#!/usr/bin/env python3
"""Retired-league / out-of-season cleanup in scripts/clairvoyance_update.py (needs bs4: run with /usr/bin/python3)."""
from __future__ import annotations

import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    import clairvoyance_update as U
except ModuleNotFoundError as exc:      # bs4 missing under the default python3
    print(f"SKIP: {exc}")
    sys.exit(0)

RETIRED_FRAGMENTS = ("baseball/mlb", "wnba", "college-baseball", "soccer/usa.1", "pwhl")


class Recorder:
    def __init__(self):
        self.urls = []

    def __call__(self, url, *a, **k):
        self.urls.append(url)
        return {"articles": [], "injuries": [], "items": []}


class RetiredLeagues(unittest.TestCase):
    def setUp(self):
        self.saved = U.fetch_json
        self.rec = Recorder()
        U.fetch_json = self.rec

    def tearDown(self):
        U.fetch_json = self.saved

    def _no_retired_urls(self):
        bad = [u for u in self.rec.urls if any(f in u for f in RETIRED_FRAGMENTS)]
        self.assertEqual(bad, [])

    def test_news_does_not_call_retired_leagues_but_keeps_the_keys(self):
        out = U.fetch_sports_news()
        self._no_retired_urls()
        for k in ("mlb", "wnba", "ncaab", "mls"):
            self.assertEqual(out.get(k), [])

    def test_injuries_and_transactions_skip_retired_leagues(self):
        inj = U.fetch_injuries_all()
        tr = U.fetch_transactions_all()
        self._no_retired_urls()
        for k in ("mlb", "wnba", "ncaab", "mls"):
            self.assertEqual(inj.get(k), [])
            self.assertEqual(tr.get(k), [])

    def test_active_leagues_are_still_fetched(self):
        U.fetch_injuries_all()
        joined = " ".join(self.rec.urls)
        for frag in ("hockey/nhl", "basketball/nba", "football/nfl", "soccer/ger.1"):    # Bundesliga stays: it feeds the Champions League domestic-form blend
            self.assertIn(frag, joined)


class NbaBracketWindow(unittest.TestCase):
    def test_window(self):
        self.assertFalse(U.nba_in_playoff_window(date(2026, 10, 3)))
        self.assertTrue(U.nba_in_playoff_window(date(2027, 5, 10)))

    def test_no_request_outside_the_window(self):
        saved, rec = U.fetch_json, Recorder()
        U.fetch_json = rec
        try:
            import os
            os.environ.pop("NBA_PLAYOFFS_FORCE", None)
            if U.nba_in_playoff_window():          # only meaningful out of season
                self.skipTest("run inside the playoff window")
            self.assertEqual(U.fetch_nba_playoff_bracket(), {})
            self.assertEqual(rec.urls, [])
        finally:
            U.fetch_json = saved


class Quiet404(unittest.TestCase):
    def test_404_is_not_a_warning_and_is_not_retried(self):
        calls, logs = [], []

        class R:
            status_code = 404

            def raise_for_status(self):
                raise RuntimeError("404")

        class S:
            def get(self, url, **k):
                calls.append(url)
                return R()

        saved_s, saved_l = U._session, U.log
        U._session, U.log = S(), lambda m, lvl="INFO": logs.append((lvl, m))
        try:
            self.assertIsNone(U.fetch_json("https://x/y", quiet_404=True))
        finally:
            U._session, U.log = saved_s, saved_l
        self.assertEqual(len(calls), 1)
        self.assertTrue(all(l != "WARN" for l, _ in logs), logs)

    def test_default_404_still_warns(self):
        logs = []

        class R:
            status_code = 404

            def raise_for_status(self):
                raise RuntimeError("404")

        class S:
            def get(self, url, **k):
                return R()

        saved_s, saved_l, saved_sleep = U._session, U.log, U.time.sleep
        U._session, U.log, U.time.sleep = S(), lambda m, lvl="INFO": logs.append((lvl, m)), lambda s: None
        try:
            self.assertIsNone(U.fetch_json("https://x/y"))
        finally:
            U._session, U.log, U.time.sleep = saved_s, saved_l, saved_sleep
        self.assertTrue(any(l == "WARN" for l, _ in logs))


if __name__ == "__main__":
    unittest.main(verbosity=2)
