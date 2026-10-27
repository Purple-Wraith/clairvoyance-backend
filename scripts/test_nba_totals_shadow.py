#!/usr/bin/env python3
"""NBA totals shadow log (owner 2026-10-27, scripts/nba_totals_shadow.py): live passes record NBA totals rated 55%+ at the posted line that did NOT qualify, with the adjusted line they would
have locked at. Record only: build_qualifying's output is unchanged, dry runs write nothing, a re-run adds no duplicates, qualifying / sub-55% / non-NBA / non-total markets are never recorded.

    python3 scripts/test_nba_totals_shadow.py
"""
from __future__ import annotations

import os, sys, tempfile, time, unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import auto_lock_settle as A  # noqa: E402
import nba_totals_shadow as nts  # noqa: E402


def mkt(side, label, prob, tierN, rawProb=None):
    return {"side": side, "label": label, "prob": prob, "rawProb": rawProb, "tierN": tierN, "evVal": 0.01, "ml": "-110", "dec": 1.91, "priceSource": None}


def game(sport="NBA", markets=(), start_h=5):
    return {"sport": sport, "hA": "BOS", "awA": "NY", "startMs": time.time() * 1000 + start_h * 3600e3, "markets": list(markets), "mcSummary": "x", "best": None}


class Shadow(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.patches = [mock.patch.object(A, "ROOT", self.root), mock.patch.object(A, "_commit_and_push", lambda *a, **k: None),
                        mock.patch.object(A, "_nba_preseason_pairs", lambda: set()), mock.patch.object(A, "_nba_regular_season_start", lambda: "2000-01-01")]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def rows(self):
        f = self.root / nts.STATE_REL
        return [__import__("json").loads(l) for l in f.read_text().splitlines()] if f.exists() else []

    def test_records_mild_leans_both_sides_with_adjusted_line(self):
        res = {"gameLegs": [game(markets=[mkt("over", "OVER 230.5", .57, 1), mkt("under", "UNDER 230.5", .43, 0)]),
                            {**game(markets=[mkt("under", "UNDER 228.5", .56, 1)]), "hA": "MIA", "awA": "ORL"}]}
        self.assertEqual(A.record_nba_totals_shadow(res, live=True), 2)
        r = {x["label"]: x for x in self.rows()}
        self.assertEqual(set(r), {"OVER 230.5", "UNDER 228.5"})
        self.assertEqual((r["OVER 230.5"]["altLine"], r["OVER 230.5"]["shift"]), (222.5, 8.0))   # OVER moves DOWN 8
        self.assertEqual(r["UNDER 228.5"]["altLine"], 236.5)                                       # UNDER moves UP 8

    def test_skips_qualifying_low_nonnba_nontotal_and_started(self):
        res = {"gameLegs": [game(markets=[mkt("under", "UNDER 230.5", .63, 2), mkt("over", "OVER 230.5", .54, 1), mkt("sprdFav", "BOS -5.5", .58, 1)]),
                            game(sport="NHL", markets=[mkt("over", "OVER 6.5", .58, 1)]),
                            {**game(markets=[mkt("over", "OVER 220.5", .58, 1)], start_h=-1), "hA": "LAL", "awA": "GS"}]}
        self.assertEqual(A.record_nba_totals_shadow(res, live=True), 0)
        self.assertEqual(self.rows(), [])

    def test_uses_raw_prob_when_clamped(self):
        res = {"gameLegs": [game(markets=[mkt("under", "UNDER 230.5", .54, 1, rawProb=.58)])]}
        self.assertEqual(A.record_nba_totals_shadow(res, live=True), 1)
        self.assertEqual(self.rows()[0]["p"], .58)

    def test_dry_run_and_scope_write_nothing_and_rerun_is_idempotent(self):
        res = {"gameLegs": [game(markets=[mkt("over", "OVER 230.5", .57, 1)])]}
        self.assertEqual(A.record_nba_totals_shadow(res, live=False), 0)
        self.assertEqual(A.record_nba_totals_shadow(res, live=True, only_sports=frozenset({"CFB"})), 0)
        self.assertEqual(self.rows(), [])
        self.assertEqual(A.record_nba_totals_shadow(res, live=True), 1)
        self.assertEqual(A.record_nba_totals_shadow(res, live=True), 0)
        self.assertEqual(len(self.rows()), 1)

    def test_build_qualifying_unchanged(self):
        res = {"gameLegs": [game(markets=[mkt("over", "OVER 230.5", .57, 1)])], "propLegs": []}
        self.assertEqual(A.build_qualifying(res, only_sports=frozenset({"NBA"})), [])

    def test_report_grades(self):
        f = self.root / "s.jsonl"
        f.write_text(nts.render([{"id": "a", "date": "2026-10-28", "hA": "BOS", "awA": "NY", "startMs": None, "side": "over", "label": "OVER 230.5", "posted": 230.5,
                                  "p": .57, "altLine": 222.5, "shift": 8.0}]))
        with mock.patch.object(nts, "_finals_for", lambda ds: {("BOS", "NY", "2026-10-28"): 225}), mock.patch.object(nts, "ROOT", self.root):
            self.assertEqual(nts.report(f, .55), 0)       # 225: loss at 230.5, win at 222.5
        self.assertEqual(nts._grade("over", 230.5, 225), "loss")
        self.assertEqual(nts._grade("over", 222.5, 225), "win")
        self.assertEqual(nts._grade("under", 236.5, 236.5), "push")


if __name__ == "__main__":
    unittest.main()
