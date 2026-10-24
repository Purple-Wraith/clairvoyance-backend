#!/usr/bin/env python3
"""Regulation -> book basis for the European over/under tables (scripts/backtest_alt_lines.py to_book_basis).  Flashscore counts REGULATION goals; books settle regulation + overtime (shootout goal excluded).
A game level after 60 minutes has an EVEN regulation total R and ends R+1 when overtime decides it, so only the lines R+0.5 (2.5, 4.5, 6.5, 8.5) move.

    python3 scripts/test_euro_basis.py
"""
import sys, unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import backtest_alt_lines as B  # noqa: E402

PM = {t: v for t, v in zip(range(0, 13), [.01, .03, .06, .09, .13, .15, .16, .13, .11, .06, .04, .02, .01])}


class Basis(unittest.TestCase):
    def test_mass_is_conserved_and_only_even_totals_lose_games(self):
        pb = B.to_book_basis(PM)
        self.assertAlmostEqual(sum(pb.values()), sum(PM.values()), places=9)
        for t in (1, 3, 5, 7, 9, 11):
            self.assertGreaterEqual(pb[t], PM[t])                      # odd totals only gain
        self.assertLess(pb[4], PM[4]); self.assertLess(pb[6], PM[6])

    def test_lines_move_only_where_a_tie_total_sits_just_below_them(self):
        pb = B.to_book_basis(PM)
        ov = lambda p, x: B.over_under(p, x)[0]
        self.assertGreater(ov(pb, 4.5) - ov(PM, 4.5), 0.03)           # a 2-2 game decided in overtime is now an OVER 4.5
        self.assertGreater(ov(pb, 6.5) - ov(PM, 6.5), 0.03)
        self.assertAlmostEqual(ov(pb, 5.5), ov(PM, 5.5), places=9)     # odd R+0.5 lines are untouched ...
        self.assertAlmostEqual(ov(pb, 7.5), ov(PM, 7.5), places=9)     # ... because a level game never has an odd regulation total
        self.assertAlmostEqual(B.over_under(pb, 6.5)[1] - B.over_under(PM, 6.5)[1], -(ov(pb, 6.5) - ov(PM, 6.5)), places=9)   # UNDER 6.5 loses what OVER 6.5 gains

    def test_the_overtime_shares_are_probabilities_and_fall_with_the_total(self):
        f = B.OT_TIED
        self.assertTrue(all(0 < v < 1 for v in f.values()))
        self.assertGreater(f[2], f[4]); self.assertGreater(f[4], f[6]); self.assertGreater(f[6], f[8])
        self.assertEqual(sorted(f), [0, 2, 4, 6, 8, 10])               # even totals only


if __name__ == "__main__":
    unittest.main(verbosity=2)
