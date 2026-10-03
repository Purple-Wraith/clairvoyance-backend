#!/usr/bin/env python3
"""Unit tests for the CFB per-game lock selection (rule B, with rule A's ML + O/U pair) in scripts/auto_lock_settle.py.

    python3 scripts/test_cfb_selection.py
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import auto_lock_settle as A  # noqa: E402


def leg(kind, prob, side=None):
    side = side or {"ML": "home", "SPREAD": "sprdFav", "OU": "over"}[kind]
    return {"side": side, "prob": prob, "label": f"{kind} {prob}"}


def kinds(legs):
    return sorted(A._market_type(l["side"]) + f"@{l['prob']}" for l in legs)


class CfbSelection(unittest.TestCase):
    def test_heavy_favourite_ml_alone_is_one_pick(self):
        self.assertEqual(kinds(A._cfb_select([leg("ML", .72), leg("SPREAD", .66)])), ["ML@0.72"])

    def test_very_strong_ml_pairs_with_an_in_band_ou(self):
        self.assertEqual(kinds(A._cfb_select([leg("ML", .80), leg("OU", .66), leg("SPREAD", .68)])), ["ML@0.8", "OU@0.66"])

    def test_ml_never_pairs_with_a_spread(self):
        self.assertEqual(kinds(A._cfb_select([leg("ML", .85), leg("SPREAD", .68)])), ["ML@0.85"])

    def test_ml_below_the_pair_bar_stays_single(self):
        self.assertEqual(kinds(A._cfb_select([leg("ML", .72), leg("OU", .66)])), ["ML@0.72"])

    def test_no_heavy_ml_takes_the_best_in_band_non_ml(self):
        self.assertEqual(kinds(A._cfb_select([leg("ML", .64), leg("SPREAD", .62), leg("OU", .69)])), ["OU@0.69"])

    def test_overconfident_spread_and_ou_are_dropped(self):
        self.assertEqual(A._cfb_select([leg("SPREAD", .78), leg("OU", .92)]), [])

    def test_below_band_is_dropped(self):
        self.assertEqual(A._cfb_select([leg("OU", .58)]), [])

    def test_never_more_than_two_and_never_two_of_a_kind(self):
        out = A._cfb_select([leg("ML", .9), leg("OU", .61, "over"), leg("OU", .7, "under"), leg("SPREAD", .65)])
        self.assertEqual(len(out), 2)
        self.assertEqual(sorted(A._market_type(l["side"]) for l in out), ["ML", "OU"])

    def test_empty(self):
        self.assertEqual(A._cfb_select([]), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
