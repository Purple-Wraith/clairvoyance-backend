#!/usr/bin/env python3
"""Sanity tests for scripts/backtest_top_picks.py (pure functions only)."""
import sys, unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import backtest_top_picks as tp  # noqa: E402


def r(i, g, p, y=1, alt=False, market="ML"):
    return {"id": i, "g": g, "p": p, "y": y, "alt": alt, "market": market, "cell_m": (market, alt), "cell_l": (g, market, alt)}


class Pure(unittest.TestCase):
    def test_league_cap_is_three_and_held_picks_fill_the_rest(self):
        c = [r(i, "NHL", 0.9 - i * 0.01) for i in range(6)] + [r(10 + i, "NFL", 0.5 + i * 0.01) for i in range(2)]
        top = tp.pick_top(c, lambda x: x["p"], 5, True)
        self.assertEqual([x["id"] for x in top], [0, 1, 2, 11, 10])                    # three NHL, then the NFL picks
        self.assertEqual(len(tp.pick_top(c, lambda x: x["p"], 8, True)), 8)            # not enough variety: the held NHL picks fill the list
        self.assertEqual([x["id"] for x in tp.pick_top(c, lambda x: x["p"], 5, False)], [0, 1, 2, 3, 4])

    def test_shrinkage_pulls_a_thin_cell_to_its_parent(self):
        train = [r(i, "NHL", .65, y=0, market="OU") for i in range(3)] + [r(100 + i, "NFL", .65, y=1) for i in range(100)]
        st = tp.Stats(train)
        thin = st.rate_l(r(0, "NHL", .7, market="OU"))
        self.assertGreater(thin, 0.3)                                                  # 3 losses do not drag the rate to zero
        self.assertLess(thin, st.pool())
        self.assertEqual(tp.Stats([]).pool(), 0.65)                                    # no history: a neutral prior

    def test_a_strong_cell_ranks_above_a_higher_stated_probability(self):
        train = [r(i, "CFB", .68, y=1 if i % 10 < 9 else 0, alt=True, market="SPREAD") for i in range(60)] + [r(100 + i, "SOC", .8, y=1 if i % 10 < 4 else 0, market="OU") for i in range(60)]
        st = tp.Stats(train)
        strong, weak = r(1, "CFB", .68, alt=True, market="SPREAD"), r(2, "SOC", .80, market="OU")
        self.assertGreater(st.rate_l(strong), st.rate_l(weak))
        self.assertGreater(weak["p"], strong["p"])                                     # stated probability would have said the opposite


if __name__ == "__main__":
    unittest.main(verbosity=2)
