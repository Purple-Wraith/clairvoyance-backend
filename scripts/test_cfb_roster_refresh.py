#!/usr/bin/env python3
"""CFB conference roster refresh (scripts/fetch_cfb.py), after the 2026-11-01 incident: ESPN changed what a team's own detail record reports (American teams say group 80, Sun Belt teams 37), the
per-team lookup found nobody for those two conferences, and the monthly refresh committed an EMPTY American and Sun Belt -- CFB lock eligibility and the games tab both read docs/cfb_teams.json.
Membership now comes from ESPN's group listing, a collapsed result is refused, and an empty listing keeps the previous roster.  No network: every ESPN call is faked.

    python3 scripts/test_cfb_roster_refresh.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
ROOT = Path(os.environ.get("CV_ROOT") or HERE.parent)
sys.path.insert(0, str(HERE))
import fetch_cfb as F  # noqa: E402


def team(i, conf="X"):
    return {"id": str(i), "abbr": f"T{i}", "name": f"Team {i}", "venueCapacity": 1000 + int(i), "venueName": f"Stadium {i}", "venueIndoor": False}


class Resp:
    def __init__(self, body):
        self.body = body

    def raise_for_status(self):
        pass

    def json(self):
        return self.body


def group_page(ids, page=1, pages=1):
    return {"count": len(ids), "pageIndex": page, "pageCount": pages,
            "items": [{"$ref": f"http://sports.core.api.espn.com/v2/sports/football/leagues/college-football/seasons/2026/teams/{i}?lang=en"} for i in ids]}


class Config(unittest.TestCase):
    def test_every_tracked_conference_is_listed_and_sun_belt_uses_group_37(self):
        self.assertEqual(F.MEMBER_GROUPS["151"], "American")
        self.assertEqual(F.MEMBER_GROUPS["37"], "Sun Belt")
        self.assertNotIn("167", F.MEMBER_GROUPS)                                              # 167 / 168 are only Sun Belt's two divisions (7 teams each)
        self.assertEqual(sorted(F.MEMBER_GROUPS.values()), sorted(["American", "ACC", "Big 12", "Big Ten", "Conference USA", "FBS Independents", "MAC", "Mountain West", "Pac-12", "SEC", "Sun Belt"]))

    def test_the_committed_roster_has_every_conference_populated(self):
        """The guard on the data file itself: this is exactly what broke on 2026-11-01."""
        conf = json.loads((ROOT / "docs" / "cfb_teams.json").read_text())["conferences"]
        self.assertEqual(sorted(conf), sorted(F.MEMBER_GROUPS.values()))
        for name, teams in conf.items():
            self.assertGreaterEqual(len(teams), 2, name)
            self.assertTrue(all(t.get("abbr") and t.get("id") for t in teams), name)
        for name in ("American", "Sun Belt", "ACC", "Big 12", "Big Ten", "SEC"):
            self.assertGreaterEqual(len(conf[name]), 12, name)


class GroupListing(unittest.TestCase):
    def test_member_ids_are_read_from_the_refs_across_pages(self):
        pages = {1: group_page([5, 58], 1, 2), 2: group_page([151, 202], 2, 2)}
        with mock.patch.object(F.requests, "get", side_effect=lambda url, params=None, **k: Resp(pages[params["page"]])) as g:
            self.assertEqual(F.fetch_group_member_ids("151", 2026), ["5", "58", "151", "202"])
        self.assertEqual(g.call_count, 2)
        self.assertIn("/seasons/2026/types/2/groups/151/teams", g.call_args_list[0].args[0])

    def test_a_failed_listing_is_empty_not_an_exception(self):
        with mock.patch.object(F.requests, "get", side_effect=RuntimeError("boom")):
            self.assertEqual(F.fetch_group_member_ids("151", 2026), [])


class Build(unittest.TestCase):
    def build(self, listings, details=None, previous=None):
        details = details if details is not None else {}

        def fake_ids(gid, season=None):
            return listings.get(gid, [])

        def fake_detail(tid):
            return details[tid] if tid in details else team(tid)
        with mock.patch.object(F, "fetch_group_member_ids", fake_ids), mock.patch.object(F, "fetch_team_conference", fake_detail), mock.patch.object(F.time, "sleep", lambda *_: None):
            return F.build_roster(previous or {})

    def test_members_come_from_the_group_listing_not_the_teams_own_group_field(self):
        r = self.build({"151": ["1", "2"], "37": ["3", "4", "5"], "1": ["6"]})
        self.assertEqual([t["id"] for t in r["American"]], ["1", "2"])
        self.assertEqual([t["id"] for t in r["Sun Belt"]], ["3", "4", "5"])
        self.assertEqual(r["ACC"][0]["abbr"], "T6")
        self.assertEqual(r["ACC"][0]["venueCapacity"], 1006)                                   # name / abbr / venue still come from the team's detail record
        self.assertEqual(sorted(r), sorted(F.MEMBER_GROUPS.values()))

    def test_an_empty_listing_keeps_the_previous_roster_for_that_conference_only(self):
        prev = {"American": [team(1), team(2)], "Sun Belt": [team(3)]}
        r = self.build({"37": ["3", "9"]}, previous=prev)                                      # American's listing came back empty
        self.assertEqual([t["id"] for t in r["American"]], ["1", "2"])
        self.assertEqual([t["id"] for t in r["Sun Belt"]], ["3", "9"])

    def test_empty_listing_and_no_previous_roster_stays_empty(self):
        self.assertEqual(self.build({})["American"], [])

    def test_a_failed_detail_call_falls_back_to_the_old_record_or_drops_the_team(self):
        prev = {"American": [{"id": "1", "abbr": "OLD", "name": "Old Name"}]}
        with mock.patch.object(F, "fetch_group_member_ids", lambda gid, season=None: ["1", "2"] if gid == "151" else []), \
                mock.patch.object(F, "fetch_team_conference", lambda tid: None), mock.patch.object(F.time, "sleep", lambda *_: None):
            r = F.build_roster(prev)
        self.assertEqual([t["abbr"] for t in r["American"]], ["OLD"])                          # team 1: old record kept; team 2: nothing known, so not invented


class Guard(unittest.TestCase):
    def conf(self, **sizes):
        return {k: [team(i) for i in range(n)] for k, n in sizes.items()}

    def test_an_emptied_or_collapsed_conference_is_refused(self):
        prev = self.conf(American=14, ACC=17, **{"Sun Belt": 14})
        self.assertEqual(F.roster_problems(self.conf(American=0, ACC=17, **{"Sun Belt": 0}), prev), ["American: 14 -> 0 teams", "Sun Belt: 14 -> 0 teams"])
        self.assertEqual(F.roster_problems(self.conf(American=10, ACC=17, **{"Sun Belt": 14}), prev), ["American: 14 -> 10 teams"])      # 10 < 75% of 14
        self.assertEqual(F.roster_problems({}, prev), ["American: 14 -> 0 teams", "ACC: 17 -> 0 teams", "Sun Belt: 14 -> 0 teams"])

    def test_normal_movement_is_allowed(self):
        prev = self.conf(American=14, ACC=17)
        self.assertEqual(F.roster_problems(self.conf(American=13, ACC=18), prev), [])          # one team leaves / joins
        self.assertEqual(F.roster_problems(self.conf(American=11, ACC=17), prev), [])          # 11 >= 75% of 14 (10.5)
        self.assertEqual(F.roster_problems(self.conf(American=14), {}), [])                    # no previous roster: nothing to compare to
        self.assertEqual(F.roster_problems(self.conf(Ind=1), self.conf(Ind=2)), [])            # a 2-team conference may lose one; only emptying it is refused
        self.assertEqual(F.roster_problems(self.conf(Ind=0), self.conf(Ind=2)), ["Ind: 2 -> 0 teams"])

    def test_run_refuses_to_overwrite_a_good_roster_with_a_collapsed_one_and_writes_a_sane_one(self):
        good = self.conf(American=14, ACC=17)
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "cfb_teams.json"
            out.write_text(json.dumps({"generated_at": "old", "conferences": good}))
            before = out.read_text()
            with mock.patch.object(F, "OUT_PATH", out), mock.patch.object(F, "build_roster", lambda previous=None: self.conf(American=0, ACC=17)):
                with self.assertRaises(RuntimeError) as cm:
                    F.run()
            self.assertIn("American: 14 -> 0", str(cm.exception))
            self.assertEqual(out.read_text(), before)                                          # untouched
            with mock.patch.object(F, "OUT_PATH", out), mock.patch.object(F, "build_roster", lambda previous=None: self.conf(American=13, ACC=17)):
                r = F.run()
            self.assertEqual(len(r["American"]), 13)
            self.assertEqual(len(json.loads(out.read_text())["conferences"]["American"]), 13)


if __name__ == "__main__":
    unittest.main()
