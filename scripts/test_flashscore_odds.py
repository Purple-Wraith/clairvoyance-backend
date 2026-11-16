#!/usr/bin/env python3
"""Flashscore odds aggregation (scripts/_flashscore_odds.py), 2026-11-02: Flashscore started listing Kalshi (a prediction market) on NHL fixtures no sportsbook had priced yet.  Its Over/Under and puck-line
prices are yes/no contract ASKS (a pair sums to 1.20-1.24, a sportsbook's ~1.05-1.10), and they were written as if they were a book's two-way market -- the nightly odds-sanity check alerted on 5 pairs.
Rows from non-sportsbook venues are now ignored, and a consensus pair above the nightly check's 1.15 overround is never written, whatever its source.  No network: rows are faked.

    python3 scripts/test_flashscore_odds.py
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import _flashscore_odds as F  # noqa: E402


def row(book, over, under, total=None):
    return {"book": book, "total": total, "odds": [str(over), str(under)]}


def overround(a, b):
    return 1 / a + 1 / b


class Venues(unittest.TestCase):
    def test_kalshi_only_game_has_no_odds_at_all(self):
        ou = [row("Kalshi", 1.56, 1.67, "5.5"), row("Kalshi", 1.72, 1.54, "6.5")]
        self.assertEqual(F.build_ou(ou), [])
        self.assertIsNone(F._main_line(ou))
        self.assertIsNone(F.build_ml([row("Kalshi", 1.43, 3.13)]))
        self.assertIsNone(F.build_pl([row("Kalshi", 1.16, 5.88, "+1.5"), row("Kalshi", 1.92, 1.75, "-1.5")]))
        self.assertEqual(F._books_seen(ou, [row("Kalshi", 1.43, 3.13)]), [])

    def test_kalshi_never_moves_a_sportsbook_consensus_or_its_book_count(self):
        books = [row("Fanduel", 1.90, 1.90, "5.5"), row("DraftKings", 1.91, 1.91, "5.5"), row("Fanatics", 1.88, 1.92, "5.5")]
        clean = F.build_ou(books)
        mixed = F.build_ou(books + [row("Kalshi", 1.56, 1.67, "5.5")])
        self.assertEqual(clean, mixed)
        self.assertEqual(mixed[0]["books"], 3)
        self.assertEqual(F._books_seen(books + [row("Kalshi", 1.56, 1.67, "5.5")]), ["DraftKings", "Fanatics", "Fanduel"])
        self.assertEqual(F._main_line(books + [row("Kalshi", 1.56, 1.67, "6.5"), row("Kalshi", 1.5, 1.7, "6.5")]), 5.5)    # two Kalshi rows must not make 6.5 the "consensus" line

    def test_every_listed_prediction_market_is_ignored_case_insensitively(self):
        for name in ("Kalshi", "KALSHI", "Polymarket", "Novig", "ProphetX"):
            self.assertIsNone(F.build_ml([row(name, 1.9, 1.9)]), name)

    def test_region_suffixed_venue_names_are_ignored_too(self):
        """2026-11-15: Flashscore lists the prediction market as "Polymarket.usa"; an exact-name lookup let it through and it was averaged with Fanatics."""
        for name in ("Polymarket.usa", "polymarket.US", "Kalshi.us", "Novig.com", " Polymarket.usa "):
            self.assertIsNone(F.build_ml([row(name, 1.11, 10.0)]), name)
            self.assertEqual(F.build_ou([row(name, 1.11, 10.0, "5.5")]), [], name)
        self.assertEqual(F._books_seen([row("Polymarket.usa", 1.11, 10.0), row("Fanatics", 1.76, 2.10)]), ["Fanatics"])

    def test_the_2026_11_15_fla_dal_case_keeps_the_sportsbook_price_alone(self):
        fanatics = row("Fanatics", 1.76, 2.10)
        ml = F.build_ml([fanatics, row("Polymarket.usa", 1.11, 10.0)])
        self.assertEqual((ml["home"], ml["away"], ml["books"]), (1.76, 2.10, 1))               # was 1.435 / 6.05 over 2 "books"
        self.assertEqual(F.build_ml([fanatics]), ml)
        self.assertIsNone(F.build_ml([row("Polymarket.usa", 1.11, 10.0)]))                    # a venue-only game stays unpriced (the model price is used) instead of -909
        pl = F.build_pl([row("Fanatics", 2.8, 1.44, "-1.5"), row("Polymarket.usa", 1.2, 6.0, "-1.5")])
        self.assertEqual(pl["-1.5"]["books"], 1)

    def test_venue_suffix_never_hides_a_real_sportsbook(self):
        for name in ("BetMGM.us", "bet365.us", "Fanduel", "DraftKings", "Fanatics", "Betr", "Caesars.us"):
            self.assertFalse(F._is_non_sportsbook({"book": name}), name)

    def test_real_sportsbooks_are_untouched(self):
        for name in ("Fanduel", "DraftKings", "BetMGM.us", "bet365.us", "Fanatics", "Betr", None):
            self.assertIsNotNone(F.build_ml([row(name, 1.9, 1.95)]), name)


class ConsensusGuard(unittest.TestCase):
    def test_a_consensus_pair_above_the_nightly_ceiling_is_dropped_whatever_the_source(self):
        wide = [row("SomeNewBook", 1.56, 1.67, "5.5")]            # overround 1.24 from a book nobody listed
        self.assertGreater(overround(1.56, 1.67), F.CONSENSUS_MAX_OVERROUND)
        self.assertEqual(F.build_ou(wide), [])
        self.assertIsNone(F.build_ml([row("SomeNewBook", 1.30, 2.30)]))                         # 1.204
        self.assertIsNone(F.build_pl([row("SomeNewBook", 1.60, 1.70, "-1.5"), row("SomeNewBook", 1.55, 1.75, "+1.5")]))

    def test_a_consensus_pair_below_one_is_dropped_whatever_the_source(self):
        """A single two-way market always sums to >= 1.0; a consensus under 1.0 means the sides came from different markets (the 11-15 median: 1.435 / 6.05 = 0.862)."""
        mixed = [row("Fanatics", 1.76, 2.10), row("SomeNewVenue", 1.11, 10.0)]          # a venue nobody listed: only the overround window can catch it
        self.assertLess(overround(1.435, 6.05), F.CONSENSUS_MIN_OVERROUND)
        self.assertIsNone(F.build_ml(mixed))
        self.assertEqual(F.build_ou([row("Fanatics", 1.76, 2.10, "5.5"), row("SomeNewVenue", 1.11, 10.0, "5.5")]), [])
        self.assertIsNone(F.build_pl([row("Fanatics", 2.8, 1.44, "-1.5"), row("SomeNewVenue", 1.2, 6.0, "-1.5")]))

    def test_normal_markets_and_lopsided_rungs_are_kept(self):
        self.assertEqual(len(F.build_ou([row("Fanduel", 1.91, 1.91, "5.5"), row("Fanduel", 1.01, 16.0, "2.5"), row("Fanduel", 9.5, 1.04, "10.5")])), 3)
        self.assertEqual(F.build_ml([row("Fanduel", 1.495, 2.65)])["home"], 1.495)
        self.assertEqual(sorted(F.build_pl([row("Fanduel", 2.275, 1.655, "-1.5"), row("Fanduel", 1.18, 4.4, "+1.5")])), ["+1.5", "-1.5"])

    def test_the_ceiling_matches_the_nightly_check(self):
        import daily_health_check as H
        self.assertEqual(F.CONSENSUS_MAX_OVERROUND, H.ODDS_OVERROUND_OK[1])
        self.assertEqual(F.CONSENSUS_MIN_OVERROUND, H.ODDS_OVERROUND_OK[0])


class CarryOver(unittest.TestCase):
    def test_kalshi_only_odds_are_not_carried_forward_but_sportsbook_odds_are(self):
        import json, tempfile
        prev = {"games": [{"id": "1", "odds": {"bk": ["Kalshi"], "ou": [{"line": 5.5, "over": 1.56, "under": 1.67}]}},
                          {"id": "2", "odds": {"bk": ["Fanduel", "DraftKings"], "ou": [{"line": 5.5, "over": 1.9, "under": 1.9}]}},
                          {"id": "3", "odds": {"bk": ["Fanduel", "Kalshi"], "ou": []}}]}
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "nhl_schedule.json"
            path.write_text(json.dumps(prev))
            games = [{"id": "1"}, {"id": "2"}, {"id": "3"}, {"id": "4"}]
            n = F.carry_over_odds(games, path)
        self.assertEqual(n, 2)
        self.assertNotIn("odds", games[0])                                    # Kalshi-only: gone, the game stays unpriced until a sportsbook posts
        self.assertEqual(games[1]["odds"]["bk"], ["Fanduel", "DraftKings"])
        self.assertIn("odds", games[2])                                       # a sportsbook is among the sources: kept as before
        self.assertNotIn("odds", games[3])


if __name__ == "__main__":
    unittest.main()
