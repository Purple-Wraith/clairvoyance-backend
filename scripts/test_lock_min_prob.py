#!/usr/bin/env python3
"""Minimum stated win probability to lock (owner 2026-11-19): no leg is locked or emailed below 65%, whatever its tier or lane, checked on the FINAL probability (after any alternate-line move).
Covers game legs of every kind, the hockey high-probability lane, alternate-line moves, props, best-adjusted legs, the lineup gate's re-qualification, and the off switch.

    python3 scripts/test_lock_min_prob.py
"""
from __future__ import annotations

import sys, unittest
from datetime import datetime
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import auto_lock_settle as A  # noqa: E402

# Noon Mountain today, not the real clock: NBA locks same-day games only, so "now + 3h" crossed midnight and failed these tests from 9 PM MT on.
T0 = int(datetime.now(ZoneInfo("America/Denver")).replace(hour=12, minute=0, second=0, microsecond=0).timestamp() * 1000)


def game(sport, markets, h="H", a="A"):
    return {"sport": sport, "hA": h, "awA": a, "startMs": T0 + 3 * 3600 * 1000, "markets": markets, "mcSummary": "x", "best": None, "socFactors": None}


def mkt(side, label, prob, tier=2, lane=False, real=True):
    return {"side": side, "label": label, "prob": prob, "tierN": tier, "hkLane": lane, "evVal": 0.05, "ml": "-110", "dec": 1.91, "priceSource": "market" if real else None}


def build(sport, markets, props=()):
    return A.build_qualifying({"gameLegs": [game(sport, markets)], "propLegs": list(props)}, only_sports=None, now=T0)


def prop(player, prob, stat="PTS"):
    return {"grade": "OPTIMAL", "sportTag": "NBA", "hA": "DEN", "awA": "OKC", "player": player, "statAbbr": stat, "line": 10.5, "over": True, "prob": prob, "team": "DEN"}


class Helper(unittest.TestCase):
    def test_constant_is_65(self):
        self.assertEqual(A.LOCK_MIN_PROB, 0.65)

    def test_boundary(self):
        self.assertTrue(A.below_lock_floor(0.649))
        self.assertFalse(A.below_lock_floor(0.65))
        self.assertFalse(A.below_lock_floor(0.6499999999))       # float noise on a stated 65%
        self.assertFalse(A.below_lock_floor(0.9))

    def test_unknown_probability_is_below_the_floor(self):
        self.assertTrue(A.below_lock_floor(None))
        self.assertTrue(A.below_lock_floor("n/a"))

    def test_zero_switches_it_off(self):
        with mock.patch.object(A, "LOCK_MIN_PROB", 0):
            self.assertFalse(A.below_lock_floor(0.10))


class GameLegs(unittest.TestCase):
    def test_a_value_tier_leg_under_the_floor_is_dropped_and_logged(self):
        A.LOCK_FLOOR_DROPS.clear()
        self.assertEqual(build("NBA", [mkt("mlHome", "H ML", .63)]), [])
        self.assertEqual(len(A.LOCK_FLOOR_DROPS), 1)
        self.assertEqual(A.LOCK_FLOOR_DROPS[0]["leg"], "H ML")

    def test_at_and_above_the_floor_it_locks(self):
        self.assertEqual(len(build("NBA", [mkt("mlHome", "H ML", .65)])), 1)
        self.assertEqual(len(build("NBA", [mkt("mlHome", "H ML", .72)])), 1)

    def test_hockey_value_lane_and_high_prob_lane_follow_the_floor(self):
        self.assertEqual(build("NHL", [mkt("mlFav", "H ML", .63, tier=2)]), [])
        self.assertEqual(len(build("NHL", [mkt("mlFav", "H ML", .66, tier=2)])), 1)
        # a total in the 56%+ high-probability lane used to qualify at 56-64%
        self.assertEqual(build("NHL", [mkt("over", "OVER 5.5", .58, tier=1, lane=True)]), [])
        self.assertEqual(len(build("NHL", [mkt("over", "OVER 5.5", .66, tier=1, lane=True)])), 1)

    def test_every_sport_is_covered(self):
        for sport, side, label in (("CFB", "mlHome", "H ML"), ("NFL", "mlHome", "H ML"), ("SOC_CL", "home", "Home Win"), ("SOC_PL", "over", "OVER 2.5")):
            self.assertEqual(build(sport, [mkt(side, label, .63)]), [], sport)

    def test_a_high_hit_moneyline_is_far_above_the_floor(self):
        self.assertEqual(len(build("NFL", [mkt("mlHome", "H ML", .80, tier=1)])), 1)


class AlternateLines(unittest.TestCase):
    def test_the_floor_applies_to_the_probability_after_the_alt_line_move(self):
        def shifted(prob):
            def fake(legs):
                return [dict(l, prob=prob, label="H -4.5", altLine={"posted": -9.5, "line": -4.5}) for l in legs]
            return fake
        base = [mkt("sprdFav", "H -9.5", .62)]
        with mock.patch.object(A, "_apply_alt_lines", shifted(.67)):
            q = build("NBA", base)
            self.assertEqual([x["label"] for x in q], ["H -4.5"])               # posted 62% moved to a 67% adjusted line: locks
        with mock.patch.object(A, "_apply_alt_lines", shifted(.63)):
            self.assertEqual(build("NBA", base), [])                          # the move still ends under 65%: dropped


class Props(unittest.TestCase):
    def props_kept(self, probs):
        q = A.build_qualifying({"gameLegs": [], "propLegs": [prop(f"P{i}", p) for i, p in enumerate(probs)]}, only_sports=None)
        return sorted(x["leg"]["player"] for x in q if x["kind"] == "PROP")

    def test_props_under_the_floor_are_dropped(self):
        self.assertEqual(self.props_kept([.62, .66, .64, .65]), ["P1", "P3"])

    def test_the_floor_is_checked_before_the_per_game_cap(self):
        # 6 props: the cap keeps the best 5, but the 62% one never competes for a slot
        self.assertEqual(len(self.props_kept([.62, .66, .67, .68, .69, .70, .71])), 5)


class LineupGate(unittest.TestCase):
    def test_requalify_refuses_a_leg_repriced_under_the_floor(self):
        self.assertTrue(A.lineup_requalify({"sport": "NBA", "side": "mlHome"}, .66, .05)[0])
        self.assertFalse(A.lineup_requalify({"sport": "NBA", "side": "mlHome"}, .63, .05)[0])
        self.assertFalse(A.lineup_requalify({"sport": "NHL", "side": "over"}, .60, -.04)[0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
