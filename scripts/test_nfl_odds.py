#!/usr/bin/env python3
"""NFL schedule moneylines: ESPN's 2026 nested odds shape must be read (the old flat homeTeamOdds.moneyLine key no longer exists)."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fetch_nfl as F  # noqa: E402


class NflOdds(unittest.TestCase):
    NESTED = {"spread": -7.0, "overUnder": 49.5,
              "moneyline": {"home": {"close": {"odds": "-300"}}, "away": {"close": {"odds": "+240"}}}}

    def test_nested_shape(self):
        self.assertEqual(F._ml_of(self.NESTED, "homeML", "homeTeamOdds"), -300)
        self.assertEqual(F._ml_of(self.NESTED, "awayML", "awayTeamOdds"), 240)

    def test_old_flat_shape_still_works(self):
        flat = {"homeTeamOdds": {"moneyLine": -150}, "awayTeamOdds": {"moneyLine": 130}}
        self.assertEqual(F._ml_of(flat, "homeML", "homeTeamOdds"), -150)
        self.assertEqual(F._ml_of(flat, "awayML", "awayTeamOdds"), 130)

    def test_no_odds_is_none(self):
        self.assertIsNone(F._ml_of({}, "homeML", "homeTeamOdds"))
        self.assertIsNone(F._ml_of(None, "homeML", "homeTeamOdds"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
