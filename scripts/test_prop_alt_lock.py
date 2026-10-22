#!/usr/bin/env python3
"""NBA prop adjusted lines through the lock pipeline (owner 2026-10-22): a prop leg carrying an `alt` is locked at the adjusted line, with the posted line recorded in `altLine`.

    python3 scripts/test_prop_alt_lock.py
"""
import sys, unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import auto_lock_settle as als


def leg(**kw):
    base = {"player": "Test Guy", "team": "AAA", "opp": "BBB", "stat": "ppg", "statAbbr": "PTS", "line": 24.5, "over": True, "ml": "-150", "prob": 0.60,
            "grade": "OPTIMAL", "hitRate": "60%", "sportTag": "NBA", "hA": "AAA", "awA": "BBB",
            "alt": {"line": 19.5, "shift": 5, "prob": 0.68, "ml": -246, "mlStr": "-246"}}
    base.update(kw)
    return base


class PropAlt(unittest.TestCase):
    def test_over_moves_to_adjusted_line(self):
        out = als._apply_prop_alt(leg())
        self.assertEqual((out["line"], out["prob"], out["ml"], out["hitRate"]), (19.5, 0.68, "-246", "68%"))
        self.assertEqual(out["altLine"]["posted"], 24.5)
        self.assertEqual(out["altLine"]["shift"], 5)
        self.assertEqual(out["altLine"]["postedLabel"], "Test Guy OVER PTS 24.5")

    def test_under_label_and_no_alt_passthrough(self):
        out = als._apply_prop_alt(leg(over=False, line=20.5, alt={"line": 25.5, "shift": 5, "prob": 0.67, "mlStr": "-230"}))
        self.assertEqual(out["altLine"]["postedLabel"], "Test Guy UNDER PTS 20.5")
        plain = leg(alt=None)
        self.assertIs(als._apply_prop_alt(plain), plain)

    def test_switch_off_keeps_posted_line(self):
        als.PROP_ALT_LINES = False
        try:
            p = leg()
            self.assertIs(als._apply_prop_alt(p), p)
        finally:
            als.PROP_ALT_LINES = True

    def test_only_nba_props_are_shifted(self):
        p = leg(sportTag="NHL")
        self.assertIs(als._apply_prop_alt(p), p)


if __name__ == "__main__":
    unittest.main(verbosity=2)
