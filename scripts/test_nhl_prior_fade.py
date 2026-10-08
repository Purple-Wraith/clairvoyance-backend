#!/usr/bin/env python3
"""NHL Edge blend (clairvoyance_update.fetch_nhl_edge): last season's share FADES with games played -- owner's curve 90% @2, 70% @5, 40% @10, 20% @15 games, GONE from 20 (owner, 2026-10-08).

    /usr/bin/python3 scripts/test_nhl_prior_fade.py     (needs bs4, like clairvoyance_update itself)
"""
import sys, unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import clairvoyance_update as C  # noqa: E402


class Fade(unittest.TestCase):
    def test_weight_curve_and_gone_at_20(self):
        w = C._nhl_prior_weight
        self.assertEqual((w(2), w(5), w(10), w(15), w(20)), (0.9, 0.7, 0.4, 0.2, 0.0))   # the owner's five points
        self.assertAlmostEqual(w(1), 0.95, places=3)                      # straight lines between them
        self.assertAlmostEqual(w(3), 0.8333, places=3)
        self.assertAlmostEqual(w(12), 0.32, places=3)
        self.assertAlmostEqual(w(17.5), 0.1, places=3)
        self.assertEqual(w(20), 0.0)                                    # 20 games: this season's stats only
        self.assertEqual(w(21), 0.0)
        self.assertEqual(w(82), 0.0)
        self.assertEqual(w(0), 1.0)
        self.assertEqual(w(None), 1.0)
        self.assertEqual(w("x"), 1.0)
        ws = [w(g) for g in range(0, 83)]
        self.assertEqual(ws, sorted(ws, reverse=True))                  # monotone: more games never raises the prior's share

    def test_blend_uses_the_weight(self):
        self.assertEqual(C._nhl_blend(4.0, 3.0, 0.75), 3.25)            # early: mostly last season
        self.assertEqual(C._nhl_blend(4.0, 3.0, 0.25), 3.75)            # late: mostly this season (the old flat blend)
        self.assertEqual(C._nhl_blend(4.0, None, 0.9), 4.0)
        self.assertEqual(C._nhl_blend(None, 3.0, 0.1), 3.0)

    def edge(self, gp_a, gp_goalie):
        cur_t = {"AAA": {"gf60": 4.0, "ga60": 4.0, "pp": 0.3, "pk": 0.7, "gp": gp_a}}
        pri_t = {"AAA": {"gf60": 3.0, "ga60": 3.0, "pp": 0.2, "pk": 0.8, "gp": 82}}
        goalies = {"2026": ({"AAA": {"name": "G", "sv": 0.88, "gaa": 3.3, "gp": gp_goalie}}, {"G": {"sv": 0.88, "gaa": 3.3, "gp": gp_goalie}}),
                   "2025": ({"AAA": {"name": "G", "sv": 0.92, "gaa": 2.5, "gp": 60}}, {"G": {"sv": 0.92, "gaa": 2.5, "gp": 60}})}
        with mock.patch.object(C, "_nhl_current_season_id", return_value="20262027"), \
             mock.patch.object(C, "_nhl_fetch_goalie_season", side_effect=lambda s: goalies[s[:4]]), \
             mock.patch.object(C, "_nhl_fetch_team_percentages", side_effect=lambda s: {"AAA": 52.0} if s.startswith("2026") else {"AAA": 50.0}), \
             mock.patch.object(C, "_nhl_fetch_team_summary", side_effect=lambda s: cur_t if s.startswith("2026") else pri_t):
            return C.fetch_nhl_edge()

    def test_early_season_leans_on_last_season_and_20_games_uses_only_this_one(self):
        early = self.edge(2, 2)
        mid = self.edge(15, 15)
        late = self.edge(20, 20)
        e, m, l = early["teamRates"]["AAA"], mid["teamRates"]["AAA"], late["teamRates"]["AAA"]
        self.assertAlmostEqual(e["gf60"], 4.0 * 0.1 + 3.0 * 0.9, places=3)               # 3.10: nearly last season's 3.0
        self.assertAlmostEqual(m["gf60"], 4.0 * 0.8 + 3.0 * 0.2, places=3)               # 3.80 at 15 games
        self.assertEqual(l["gf60"], 4.0)                                                  # 20 games: this season only
        self.assertEqual((e["priorW"], m["priorW"], l["priorW"]), (0.9, 0.2, 0.0))
        self.assertEqual(l["pp"], 0.3)
        self.assertEqual(l["pk"], 0.7)
        self.assertAlmostEqual(early["goalies"]["AAA"]["sv"], 0.88 * 0.1 + 0.92 * 0.9, places=3)    # goalie fades on his own games
        self.assertEqual(late["goalies"]["AAA"]["sv"], 0.88)
        self.assertEqual(late["zoneStart"]["AAA"], 52.0)
        self.assertLess(early["zoneStart"]["AAA"], mid["zoneStart"]["AAA"])
        self.assertEqual(early["priorFullGp"], 20)

    def test_a_team_with_no_current_games_uses_last_season_fully(self):
        out = self.edge(0, 0)
        self.assertEqual(out["teamRates"]["AAA"]["priorW"], 1.0)
        self.assertAlmostEqual(out["teamRates"]["AAA"]["gf60"], 3.0, places=3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
