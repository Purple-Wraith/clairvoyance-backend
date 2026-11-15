#!/usr/bin/env python3
"""nba_total_shift (scripts/clairvoyance_update.py): the league-scoring correction shipped as data.json nba.teamRatings.totalShift and applied by docs/app.html nbaMC."""
import sys, unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import clairvoyance_update as cu  # noqa: E402

# 12 identical average teams: the model total for any pairing is 2 x 115 x pace/100 (drtg ratio is 1)
ADV = {f"T{i}": {"ortg": 115.0, "drtg": 115.0, "pace": 100.0} for i in range(12)}
MODEL_TOTAL = 230.0


def game(i, total, **kw):
    g = {"state": "post", "seasonType": 2, "preseason": False, "home": f"T{i % 12}", "away": f"T{(i + 1) % 12}", "homeScore": total // 2, "awayScore": total - total // 2}
    g.update(kw)
    return g


class TotalShift(unittest.TestCase):
    def test_projection_running_high_gives_a_negative_shrunk_shift(self):
        r = cu.nba_total_shift(ADV, [game(i, 227) for i in range(120)])
        self.assertEqual(r["n"], 120)
        self.assertAlmostEqual(r["rawBias"], 3.0, places=2)
        self.assertAlmostEqual(r["shift"], -3.0 * 120 / (120 + cu.NBA_TOTAL_SHIFT_K), places=2)     # -2.0: shrunk, not the full 3

    def test_unbiased_projection_gives_zero(self):
        self.assertEqual(cu.nba_total_shift(ADV, [game(i, 230) for i in range(60)])["shift"], 0.0)

    def test_projection_running_low_gives_a_positive_shift(self):
        self.assertGreater(cu.nba_total_shift(ADV, [game(i, 235) for i in range(60)])["shift"], 0)

    def test_too_few_games_returns_none(self):
        self.assertIsNone(cu.nba_total_shift(ADV, [game(i, 227) for i in range(cu.NBA_TOTAL_SHIFT_MIN_GAMES - 1)]))

    def test_clamped(self):
        r = cu.nba_total_shift(ADV, [game(i, 200) for i in range(400)])
        self.assertEqual(r["shift"], -cu.NBA_TOTAL_SHIFT_CLAMP)

    def test_only_finished_regular_season_games_with_ratings_count(self):
        games = ([game(i, 227) for i in range(30)]
                 + [game(i, 150, state="in") for i in range(30)]                    # not final
                 + [game(i, 150, preseason=True) for i in range(30)]                # preseason
                 + [game(i, 150, seasonType=3) for i in range(30)]                  # playoffs
                 + [game(i, 150, home="XXX") for i in range(30)]                    # no ratings for the team
                 + [game(i, 227, homeScore=None) for i in range(30)])               # no score
        r = cu.nba_total_shift(ADV, games)
        self.assertEqual(r["n"], 30)
        self.assertAlmostEqual(r["rawBias"], 3.0, places=2)

    def test_no_ratings_returns_none(self):
        self.assertIsNone(cu.nba_total_shift({}, [game(i, 227) for i in range(60)]))
        self.assertIsNone(cu.nba_total_shift(ADV, []))


if __name__ == "__main__":
    unittest.main(verbosity=2)
