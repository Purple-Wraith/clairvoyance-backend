#!/usr/bin/env python3
"""MoneyPuck team rows (clairvoyance_update._mp_load_teams): xGF/60 and xGA/60 are per-60 RATES and hdcfPct is a 0-1 SHARE, not the raw counts they used to carry (found 2026-10-17).

    /usr/bin/python3 scripts/test_moneypuck_units.py     (needs bs4, like clairvoyance_update itself)
"""
import sys, unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    import clairvoyance_update as C  # noqa: E402
except Exception as exc:  # default python here has no bs4
    C = None
    _WHY = str(exc)[:80]


@unittest.skipIf(C is None, "bs4/lxml not installed in this interpreter")
class Units(unittest.TestCase):
    def rows(self):
        return [{"team": "COL", "situation": "5on5", "iceTime": "23477.0", "xGoalsFor": "19.8", "xGoalsAgainst": "15.52", "xGoalsPercentage": "0.56", "corsiPercentage": "0.52",
                 "highDangerShotsFor": "12", "highDangerShotsAgainst": "10", "goalsFor": "30", "goalsAgainst": "15", "shotsOnGoalFor": "191"}]

    def test_expected_goals_are_per_60_and_high_danger_is_a_share(self):
        with mock.patch.object(C, "fetch_csv_rows", return_value=self.rows()):
            t = C._mp_load_teams(2026)["COL"]["5on5"]
        self.assertAlmostEqual(t["xgf60"], 19.8 / (23477 / 3600), places=4)            # ~3.04 per 60, not 19.8
        self.assertAlmostEqual(t["xga60"], 15.52 / (23477 / 3600), places=4)
        self.assertAlmostEqual(t["hdcfPct"], 12 / 22, places=6)                         # 0.545, not 12
        self.assertLess(t["xgf60"], 6); self.assertLessEqual(t["hdcfPct"], 1)

    def test_a_team_with_twice_the_games_gets_the_same_rate(self):
        r2 = self.rows(); r2[0].update({"iceTime": "46954.0", "xGoalsFor": "39.6", "xGoalsAgainst": "31.04"})
        with mock.patch.object(C, "fetch_csv_rows", return_value=self.rows()):
            a = C._mp_load_teams(2026)["COL"]["5on5"]["xgf60"]
        with mock.patch.object(C, "fetch_csv_rows", return_value=r2):
            b = C._mp_load_teams(2026)["COL"]["5on5"]["xgf60"]
        self.assertAlmostEqual(a, b, places=6)                                           # the old counts doubled with games played

    def test_per60_helper_degrades(self):
        self.assertEqual(C._per60(None, 100), 0.0); self.assertEqual(C._per60(5, 0), 0.0); self.assertEqual(C._per60("x", 3600), 0.0); self.assertEqual(C._per60(6, 3600), 6.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
