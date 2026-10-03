#!/usr/bin/env python3
"""Unit tests for the alternate-line shift (CFB/NFL spread + O/U) in scripts/auto_lock_settle.py.

    python3 scripts/test_alt_lines.py
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import auto_lock_settle as A  # noqa: E402


def leg(sport, side, label, prob=0.66, tier=2):
    return {"kind": "GAME", "sport": sport, "side": side, "label": label, "prob": prob, "tierN": tier, "evVal": 0.05,
            "hA": "MSST", "awA": "ALA", "dec": 1.909, "ml": "-110"}


class AltLine(unittest.TestCase):
    def test_cfb_under_moves_up_to_the_target_probability(self):
        out = A._alt_shift_leg(leg("CFB", "under", "UNDER 60.5"))
        self.assertEqual(out["label"], "UNDER 65.5")                      # +5 cushion
        self.assertGreaterEqual(out["prob"], 0.67)
        self.assertEqual(out["priceSource"], "estimated")
        self.assertEqual(out["altLine"]["posted"], 60.5)
        self.assertEqual(out["altLine"]["shift"], 5.0)
        self.assertEqual(out["altLine"]["postedLabel"], "UNDER 60.5")

    def test_cfb_over_moves_down(self):
        out = A._alt_shift_leg(leg("CFB", "over", "OVER 60.5"))
        self.assertEqual(out["label"], "OVER 55.5")

    def test_spread_favourite_lays_fewer_points_and_dog_gets_more(self):
        fav = A._alt_shift_leg(leg("CFB", "sprdFav", "ALA -5.5"))
        dog = A._alt_shift_leg(leg("CFB", "sprdDog", "MSST +5.5"))
        self.assertEqual(fav["label"], "ALA +1.5")                         # -5.5 + 7 crosses zero, still a valid alternate line
        self.assertEqual(dog["label"], "MSST +12.5")
        self.assertTrue(all(o["prob"] >= 0.67 for o in (fav, dog)))

    def test_fav_label_keeps_sign_format(self):
        out = A._alt_shift_leg(leg("CFB", "sprdFav", "ALA -21.5"))
        self.assertEqual(out["label"], "ALA -14.5")

    def test_shift_stays_inside_the_band(self):
        for sport, mkt, side, label in (("CFB", "OU", "under", "UNDER 50.5"), ("NFL", "OU", "under", "UNDER 44.5"),
                                        ("NFL", "SPREAD", "sprdDog", "NYG +3.5"), ("NBA", "OU", "over", "OVER 221.5")):
            out = A._alt_shift_leg(leg(sport, side, label))
            _curve, kmin, kmax = A.ALT_LINE_CFG[sport][mkt]
            if out:
                self.assertTrue(kmin <= out["altLine"]["shift"] <= kmax, (sport, mkt, out["altLine"]))

    def test_nfl_cap_that_cannot_reach_the_floor_keeps_the_posted_line(self):
        saved = A.ALT_FLOOR_P
        try:
            A.ALT_FLOOR_P = 0.99
            self.assertIsNone(A._alt_shift_leg(leg("NFL", "under", "UNDER 44.5")))
        finally:
            A.ALT_FLOOR_P = saved

    def test_moneyline_and_other_sports_are_untouched(self):
        self.assertIsNone(A._alt_shift_leg(leg("CFB", "mlFav", "ALA ML")))
        self.assertIsNone(A._alt_shift_leg(leg("MLB", "under", "UNDER 8.5")))

    def test_whole_number_lines_move_onto_a_half_point(self):
        out = A._alt_shift_leg(leg("CFB", "under", "UNDER 61.0"))
        self.assertTrue(out["label"].endswith(".5"), out["label"])

    def test_every_alt_line_keeps_a_half_point(self):
        for posted in ("UNDER 47.5", "UNDER 58.5", "UNDER 71.5"):
            self.assertTrue(A._alt_shift_leg(leg("CFB", "under", posted))["label"].endswith(".5"))

    def test_nba_total_uses_the_empirical_table(self):
        under = A._alt_shift_leg(leg("NBA", "under", "UNDER 225.5"))
        over = A._alt_shift_leg(leg("NBA", "over", "OVER 221.5"))
        self.assertEqual(under["label"], "UNDER 233.5")
        self.assertEqual(over["label"], "OVER 213.5")
        self.assertAlmostEqual(under["prob"], 0.67, places=3)
        self.assertEqual(under["altLine"]["shift"], 8.0)

    def test_nba_spread_without_a_line_number_is_left_alone(self):
        self.assertIsNone(A._alt_shift_leg(leg("NBA", "sprdFav", "BOS -ATS")))

    def test_table_interpolates_between_points(self):
        curve = A.ALT_LINE_CFG["NBA"]["OU"][0]
        self.assertAlmostEqual(A._alt_prob(curve, 9.0), (0.670 + 0.711) / 2, places=3)
        self.assertEqual(A._alt_prob(curve, 20), 0.711)

    def test_hockey_total_picks_the_side_and_line_nearest_the_60_65_band(self):
        for sport in ("NHL", "SHL", "LIIGA", "NLA", "EXTRALIGA"):
            self.assertEqual(A._alt_shift_leg(leg(sport, "over", "OVER 5.5"))["label"], "UNDER 6.5")      # flips side
            self.assertEqual(A._alt_shift_leg(leg(sport, "under", "UNDER 5.5"))["label"], "UNDER 6.5")
            self.assertEqual(A._alt_shift_leg(leg(sport, "under", "UNDER 6.5"))["label"], "OVER 5.5")      # flips side
            self.assertEqual(A._alt_shift_leg(leg(sport, "over", "OVER 6.5"))["label"], "OVER 5.5")
            self.assertEqual(A._alt_shift_leg(leg(sport, "over", "OVER 4.5"))["label"], "UNDER 5.5")

    def test_hockey_flip_updates_side_and_is_recorded(self):
        out = A._alt_shift_leg(leg("NHL", "over", "OVER 5.5"))
        self.assertEqual(out["side"], "under")
        self.assertTrue(out["altLine"]["flip"])
        self.assertFalse(A._alt_shift_leg(leg("NHL", "under", "UNDER 5.5"))["altLine"]["flip"])

    def test_hockey_uses_the_nhl_closing_line_hit_rates(self):
        self.assertEqual(A._alt_shift_leg(leg("NHL", "over", "OVER 5.5"))["prob"], 0.585)
        self.assertEqual(A._alt_shift_leg(leg("NHL", "under", "UNDER 6.5"))["prob"], 0.589)

    def test_hockey_never_picks_below_the_floor(self):
        saved = A.HOCKEY_ALT_FLOOR
        try:
            A.HOCKEY_ALT_FLOOR = 0.99
            with self.assertRaises(TypeError):
                A._hockey_pick_alt(5.5)       # nothing qualifies -> no candidate (callers only reach this with a real total)
        finally:
            A.HOCKEY_ALT_FLOOR = saved

    def test_hockey_whole_line_is_anchored_on_a_half_point(self):
        out = A._alt_shift_leg(leg("NHL", "under", "UNDER 6.0"))
        self.assertTrue(out["label"].endswith(".5"), out["label"])

    def test_hockey_puck_line_and_moneyline_are_untouched(self):
        self.assertIsNone(A._alt_shift_leg(leg("NHL", "plFav", "BOS -1.5")))
        self.assertIsNone(A._alt_shift_leg(leg("NHL", "mlFav", "BOS ML")))

    def test_hockey_leg_is_priced_as_an_estimate(self):
        out = A._alt_shift_leg(leg("NHL", "over", "OVER 5.5"))
        self.assertEqual(out["priceSource"], "estimated")
        self.assertEqual(out["altLine"]["postedLabel"], "OVER 5.5")

    def test_over_never_goes_to_a_non_positive_line(self):
        self.assertIsNone(A._alt_shift_leg(leg("CFB", "over", "OVER 3.5")))

    def test_already_shifted_leg_is_not_shifted_twice(self):
        once = A._alt_shift_leg(leg("CFB", "under", "UNDER 60.5"))
        self.assertIsNone(A._alt_shift_leg(once))

    def test_estimated_price_is_a_short_price_consistent_with_the_probability(self):
        out = A._alt_shift_leg(leg("CFB", "under", "UNDER 60.5"))
        self.assertLess(out["dec"], 1.5)
        self.assertLess(1 / out["dec"], out["prob"] + 0.05)
        self.assertTrue(out["ml"].startswith("-"))

    def test_apply_passes_moneylines_through_and_shifts_the_rest(self):
        ml, ou = leg("CFB", "mlFav", "ALA ML", 0.8), leg("CFB", "under", "UNDER 60.5")
        res = A._apply_alt_lines([ml, ou])
        self.assertIs(res[0], ml)
        self.assertEqual(res[1]["label"], "UNDER 65.5")

    def test_build_qualifying_locks_the_shifted_line(self):
        result = {"gameLegs": [{"sport": "CFB", "hA": "MSST", "awA": "ALA", "startMs": None, "markets": [
            {"side": "under", "label": "UNDER 60.5", "prob": 0.66, "tierN": 2, "evVal": 0.08, "dec": 1.909, "ml": "-110"}]}]}
        q = A.build_qualifying(result, only_sports=frozenset({"CFB"}))
        self.assertEqual([x["label"] for x in q], ["UNDER 65.5"])
        self.assertEqual(q[0]["altLine"]["posted"], 60.5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
