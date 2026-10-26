#!/usr/bin/env python3
"""College football lock gate (owner 2026-10-25): CFB picks are only locked for games where at least one team is in the American, ACC, Big 12, Big Ten, Mountain West, Pac-12 and SEC (plus Notre Dame as the
only Independent).  MAC / Conference USA / Sun Belt / UConn / FCS are out.  The gate sits in build_qualifying (so the main pass, CFB evening pass, watchdog and the best-adjusted-line lock all obey it)
and again in lock_game_leg.  Manual locks in the app are untouched.

    python3 scripts/test_cfb_conference_gate.py
"""
from __future__ import annotations

import json, os, sys, time, unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
ROOT = Path(os.environ.get("CV_ROOT") or HERE.parent)
sys.path.insert(0, str(HERE))
import auto_lock_settle as A  # noqa: E402


def leg(sport="CFB", hA="ALA", awA="MSST"):
    return {"sport": sport, "hA": hA, "awA": awA, "startMs": time.time() * 1000 + 5 * 3600e3, "markets": [
        {"side": "ALA ML" if hA == "ALA" else f"{hA} ML", "label": f"{hA} ML", "prob": .8, "tierN": 3, "tier": "PREMIUM", "ml": "-300", "dec": 1.33, "priceSource": "market"}], "mcSummary": "x", "best": None, "socFactors": None}


class Roster(unittest.TestCase):
    def test_allowed_conferences(self):
        teams = json.loads((ROOT / "docs" / "cfb_teams.json").read_text())["conferences"]
        ok = A.cfb_lock_teams()
        for conf in ("American", "ACC", "Big 12", "Big Ten", "Mountain West", "Pac-12", "SEC"):
            self.assertTrue(teams[conf]); self.assertTrue(all(t["abbr"] in ok for t in teams[conf]), conf)
        for conf in ("MAC", "Conference USA", "Sun Belt"):
            self.assertFalse(any(t["abbr"] in ok for t in teams[conf]), conf)

    def test_notre_dame_is_the_only_independent(self):
        self.assertIn("ND", A.cfb_lock_teams()); self.assertNotIn("CONN", A.cfb_lock_teams())

    def test_one_team_in_an_allowed_conference_is_enough(self):
        self.assertTrue(A.cfb_lock_eligible("ALA", "MSST")); self.assertTrue(A.cfb_lock_eligible("ND", "USC"))
        self.assertTrue(A.cfb_lock_eligible("APP", "ALA")); self.assertTrue(A.cfb_lock_eligible("ALA", "APP")); self.assertTrue(A.cfb_lock_eligible("ND", "CONN"))
        self.assertTrue(A.cfb_lock_eligible("ALA", "NOPE")); self.assertTrue(A.cfb_lock_eligible(None, "ALA"))          # an FCS / unknown opponent does not matter
        self.assertFalse(A.cfb_lock_eligible("AKR", "BGSU")); self.assertFalse(A.cfb_lock_eligible("APP", "CONN")); self.assertFalse(A.cfb_lock_eligible("NOPE", "NOPE2"))

    def test_both_mode(self):
        with mock.patch.object(A, "CFB_LOCK_REQUIRE", "both"):
            self.assertFalse(A.cfb_lock_eligible("APP", "ALA")); self.assertTrue(A.cfb_lock_eligible("ALA", "MSST"))

    def test_unreadable_roster_fails_closed(self):
        with mock.patch.object(A, "_CFB_LOCK_TEAMS", None), mock.patch.object(A.json, "loads", side_effect=ValueError("boom")):
            self.assertEqual(A.cfb_lock_teams(), frozenset())
        A._CFB_LOCK_TEAMS = None                                   # let the next caller reload the real roster


class Gate(unittest.TestCase):
    def setUp(self):
        pass

    def test_build_qualifying_drops_cfb_games_with_no_team_in_scope(self):
        q = A.build_qualifying({"gameLegs": [leg(hA="ALA", awA="MSST"), leg(hA="APP", awA="ALA"), leg(hA="AKR", awA="BGSU")], "propLegs": []}, only_sports=None)
        got = {(x["hA"], x["awA"]) for x in q}
        self.assertEqual(got, {("ALA", "MSST"), ("APP", "ALA")}, got)

    def test_other_sports_are_not_gated(self):
        q = A.build_qualifying({"gameLegs": [leg("NFL", "APP", "AKR")], "propLegs": []}, only_sports=None)
        self.assertEqual([x["sport"] for x in q], ["NFL"])

    def test_lock_game_leg_refuses_an_out_of_scope_game_without_touching_the_page(self):
        page = mock.Mock()
        out = A.lock_game_leg(page, {"sport": "CFB", "hA": "APP", "awA": "AKR", "startMs": time.time() * 1000 + 5 * 3600e3})
        self.assertTrue(out.startswith("skip: conference"), out); page.evaluate.assert_not_called()

    def test_best_adjusted_line_is_not_staged_for_an_out_of_scope_game(self):
        g = leg(hA="APP", awA="AKR"); g["adjBest"] = {"key": "k", "sport": "CFB", "hA": "APP", "awA": "AKR", "date": "2026-12-30", "side": "under",
                                                      "alt": {"label": "UNDER 60.5", "prob": .75, "mkt": "OU", "ml": "-300", "dec": 1.33, "posted": 54.5, "line": 60.5, "shift": 6, "postedLabel": "UNDER 54.5", "postedProb": .55, "postedDec": 1.91, "postedMl": "-110"}}
        self.assertEqual([x for x in A.build_qualifying({"gameLegs": [g], "propLegs": []}, only_sports=None)], [])

    def test_lock_pass_counts_the_refusal_as_a_skip_not_a_failure(self):
        with mock.patch.object(A, "lock_game_leg", return_value="skip: conference -- CFB ALA @ APP is outside the lock conferences"):
            r = A._lock_qualifying_legs(mock.Mock(), [{"kind": "GAME", "sport": "CFB", "label": "x", "hA": "APP", "awA": "AKR"}])
        self.assertEqual((r.new, r.failed, r.skipped), (0, 0, 1))


if __name__ == "__main__":
    unittest.main(verbosity=2)
