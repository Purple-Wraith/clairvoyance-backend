#!/usr/bin/env python3
"""Offline tests for docs/nba_schedule.json (scripts/fetch_nba.py) and its wiring (lock_prep job, workflows, lock-pass NBA merge).

No network: the HTTP layer is faked with recorded ESPN payloads in scripts/fixtures/nba_schedule/:
  scoreboard_20261003_preseason.json / scoreboard_20261004_preseason.json   REAL NBA scoreboard responses recorded 2026-10-03 (preseason: no odds)
  odds_object_recorded_nfl.json     REAL current ESPN odds object (DraftKings; nested moneyline/pointSpread/total, no moneyLine key), recorded from
                                    the NFL scoreboard the same day -- NBA regular-season games share ESPN's odds schema
  scoreboard_regular_season_with_odds.json   the real NBA preseason event reshaped into regular-season / live / final / postponed games,
                                    carrying NBA-valued copies of that real odds object plus the OLD flat shape (homeTeamOdds.moneyLine)
  core_odds_item_recorded_nhl.json  REAL ESPN core-API odds item (flat shape: homeTeamOdds.moneyLine, overOdds/underOdds), recorded 2026-10-03

    python3 scripts/test_nba_schedule.py
"""
from __future__ import annotations

import copy
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
FIX = HERE / "fixtures" / "nba_schedule"
sys.path.insert(0, str(HERE))
import fetch_nba as F  # noqa: E402
import lock_prep as LP  # noqa: E402

NOW = datetime(2026, 10, 3, 20, 0, tzinfo=timezone.utc)   # 4 PM ET on 2026-10-03


def fx(name):
    return json.loads((FIX / name).read_text())


class FakeESPN:
    """get(url, params=None, **kw) -> payload; raises for days in `fail`; records calls."""

    def __init__(self, boards=None, fail=(), core=None):
        self.boards, self.fail, self.core, self.calls = boards or {}, set(fail), core or {}, []

    def __call__(self, url, params=None, **kw):
        self.calls.append((url, dict(params or {})))
        if "/scoreboard" in url:
            day = params["dates"]
            if day in self.fail:
                raise RuntimeError("HTTP 500")
            return self.boards.get(day, {"leagues": [{"season": {"year": 2027, "displayName": "2026-27"}}], "events": []})
        if url in self.core:
            v = self.core[url]
            if isinstance(v, Exception):
                raise v
            return v
        raise RuntimeError("HTTP 404")


NOSLEEP = lambda s: None  # noqa: E731


class Parsing(unittest.TestCase):
    def test_preseason_event_from_real_response(self):
        games, season = F.fetch_day("20261004", get=FakeESPN({"20261004": fx("scoreboard_20261004_preseason.json")}), now=NOW)
        self.assertEqual(len(games), 2)
        g = games[0]
        self.assertEqual((g["id"], g["home"], g["away"]), ("401914127", "DEN", "UTAH"))   # ESPN keys, same as data.json nba.standings
        self.assertEqual((g["homeName"], g["awayName"]), ("Denver Nuggets", "Utah Jazz"))
        self.assertEqual(g["date"], "2026-10-04T23:00Z")
        self.assertEqual(g["day"], "2026-10-04")
        self.assertEqual(g["state"], "pre")
        self.assertIsNone(g["homeScore"])
        self.assertIsNone(g["awayScore"])
        self.assertEqual(g["seasonType"], 1)
        self.assertTrue(g["preseason"])
        self.assertEqual(g["venue"], "CU Events Center")
        self.assertEqual(g["network"], "NBA TV")
        for k in F.ODDS_FIELDS:
            self.assertIsNone(g[k], k)            # preseason games carry no odds object -- present in the file, all null
        self.assertIsNone(g["oddsAt"])
        self.assertEqual(season["year"], 2027)
        self.assertTrue(games[1]["neutralSite"])  # GS VS LAC at a neutral site

    def test_series_note_kept(self):
        games, _ = F.fetch_day("20261003", get=FakeESPN({"20261003": fx("scoreboard_20261003_preseason.json")}), now=NOW)
        self.assertEqual(games[0]["note"], "NBA Canada Games 2026")

    def test_regular_season_games_and_flags(self):
        sb = fx("scoreboard_regular_season_with_odds.json")
        games, _ = F.fetch_day("20261022", get=FakeESPN({"20261022": sb}), now=NOW)
        by = {g["id"]: g for g in games}
        self.assertEqual(len(by), 6)
        self.assertFalse(by["401800001"]["preseason"])
        self.assertEqual(by["401800001"]["seasonType"], 2)
        self.assertTrue(by["401800006"]["preseason"])
        live = by["401800003"]
        self.assertEqual((live["state"], live["homeScore"], live["awayScore"], live["period"], live["displayClock"]), ("in", 54, 51, 2, "3:12"))
        final = by["401800004"]
        self.assertEqual((final["state"], final["homeScore"], final["awayScore"]), ("post", 108, 112))
        pp = by["401800005"]
        self.assertTrue(pp["postponed"])
        self.assertEqual(pp["state"], "pre")       # a postponed game must never look final
        self.assertIsNone(pp["homeScore"])

    def test_event_without_ids_or_teams_is_dropped(self):
        self.assertIsNone(F.parse_event({"id": "1", "competitions": [{"competitors": []}]}, NOW))
        self.assertIsNone(F.parse_event({"competitions": [{}]}, NOW))


class OddsShapes(unittest.TestCase):
    def test_real_recorded_nested_shape(self):
        o = F.parse_odds(fx("odds_object_recorded_nfl.json"), "WSH", "IND")      # home WSH is the underdog (+4.5), away IND the favourite
        self.assertEqual((o["homeML"], o["awayML"]), (170, -205))
        self.assertEqual(o["spread"], 4.5)                                       # always the HOME line
        self.assertEqual(o["spreadFav"], "IND")
        self.assertEqual((o["spreadHomeOdds"], o["spreadAwayOdds"]), (-115, -105))
        self.assertEqual((o["overUnder"], o["ou"], o["ouOver"], o["ouUnder"]), (46.5, 46.5, -115, -105))
        self.assertEqual(o["details"], "IND -4.5")
        self.assertEqual(o["provider"], "DraftKings")

    def test_nba_home_favourite(self):
        o = F.parse_odds(fx("odds_object_nba_shape.json"), "BOS", "NY")
        self.assertEqual((o["homeML"], o["awayML"], o["spread"], o["spreadFav"], o["overUnder"]), (-270, 220, -6.5, "BOS", 224.5))
        self.assertIsInstance(o["homeML"], int)

    def test_away_favourite_sign_convention(self):
        games, _ = F.fetch_day("20261022", get=FakeESPN({"20261022": fx("scoreboard_regular_season_with_odds.json")}), now=NOW)
        g = {x["id"]: x for x in games}["401800002"]
        self.assertEqual((g["spread"], g["spreadFav"], g["homeML"], g["awayML"]), (3.5, "LAL", 140, -165))
        self.assertEqual(g["details"], "LAL -3.5")
        self.assertEqual(g["oddsAt"], "2026-10-03T20:00:00Z")

    def test_old_flat_shape(self):
        games, _ = F.fetch_day("20261022", get=FakeESPN({"20261022": fx("scoreboard_regular_season_with_odds.json")}), now=NOW)
        g = {x["id"]: x for x in games}["401800003"]
        self.assertEqual((g["homeML"], g["awayML"], g["spread"], g["overUnder"]), (-135, 115, -2.5, 219.0))
        self.assertEqual(g["spreadFav"], "MIA")

    def test_core_api_item_flat_shape_recorded(self):
        o = F.parse_odds(fx("core_odds_item_recorded_nhl.json"), "BUF", "CHI")
        self.assertEqual((o["homeML"], o["awayML"]), (-230, 190))
        self.assertEqual((o["ouOver"], o["ouUnder"]), (-102, -118))
        self.assertEqual(o["overUnder"], 6.5)

    def test_helpers(self):
        self.assertEqual(F._american("-230"), -230)
        self.assertEqual(F._american("+190"), 190)
        self.assertEqual(F._american("EVEN"), 100)
        self.assertIsNone(F._american("n/a"))
        self.assertEqual(F._num("PK"), 0.0)
        self.assertEqual(F._num("+4.5"), 4.5)
        self.assertEqual(F._line("o224.5"), 224.5)
        self.assertEqual(F.parse_odds({}), {})
        self.assertEqual(F.parse_odds({"details": "x"}), {})      # no usable number -> no line
        pk = F.parse_odds({"spread": 0, "overUnder": 200, "homeTeamOdds": {"moneyLine": -110}}, "A", "B")
        self.assertIsNone(pk["spreadFav"])                         # pick'em has no favourite


class Windows(unittest.TestCase):
    def test_et_day_index(self):
        # 2026-10-06T02:00Z is 10 PM ET on the 5th: ESPN lists it under dates=20261005
        self.assertEqual(F.et_day(datetime(2026, 10, 6, 2, 0, tzinfo=timezone.utc)), "20261005")
        g = F.parse_event(fx("scoreboard_20261004_preseason.json")["events"][0], NOW)
        self.assertEqual(g["day"], "2026-10-04")

    def test_window_is_yesterday_through_n_days(self):
        espn = FakeESPN()
        F.build_schedule({}, days=10, now=NOW, get=espn, sleep=NOSLEEP)
        days = [p["dates"] for u, p in espn.calls if "/scoreboard" in u]
        self.assertEqual(days[0], "20261002")
        self.assertEqual(days[-1], "20261013")
        self.assertEqual(len(days), 12)


class CarryForward(unittest.TestCase):
    def _prev(self):
        sb4, sb3 = fx("scoreboard_20261004_preseason.json"), fx("scoreboard_20261003_preseason.json")
        espn = FakeESPN({"20261004": sb4, "20261003": sb3})
        pay = F.build_schedule({}, days=3, now=NOW, get=espn, sleep=NOSLEEP)
        self.assertIsNotNone(pay)
        return {"generated_at": "2026-10-03 18:00 UTC", **pay}

    def test_failed_day_keeps_previous_games(self):
        prev = self._prev()
        sb3 = fx("scoreboard_20261003_preseason.json")
        espn = FakeESPN({"20261003": sb3}, fail={"20261004"})        # 10-04 fetch fails
        pay = F.build_schedule(prev, days=3, now=NOW, get=espn, sleep=NOSLEEP)
        ids = {g["id"]: g for g in pay["games"]}
        self.assertIn("401914127", ids)                              # DEN/UTAH survived the outage
        self.assertTrue(ids["401914127"]["carried"])
        self.assertEqual(ids["401914127"]["lastSeen"], "2026-10-03T18:00Z")
        self.assertNotIn("carried", ids["401902644"])                # the day that did load is fresh, not carried
        self.assertEqual(pay["meta"]["days_failed"], ["20261004"])

    def test_every_day_failing_writes_nothing(self):
        prev = self._prev()
        wanted = {"20261002", "20261003", "20261004", "20261005", "20261006", "20261007"}   # yesterday + 4 days + today
        self.assertIsNone(F.build_schedule(prev, days=4, now=NOW, get=FakeESPN(fail=wanted), sleep=NOSLEEP))

    def test_run_leaves_file_untouched_on_total_failure(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "nba_schedule.json"
            out.write_text('{"generated_at": "2026-10-03 18:00 UTC", "games": [{"id": "1"}]}')
            before = out.read_text()
            wanted = {(datetime(2026, 10, 2 + i).strftime("%Y%m%d")) for i in range(0, 12)}
            rc = F.run(out, 10, now=NOW, get=FakeESPN(fail=wanted), sleep=NOSLEEP, health=False)
            self.assertEqual(rc, 1)
            self.assertEqual(out.read_text(), before)
            self.assertEqual([p.name for p in Path(td).iterdir()], ["nba_schedule.json"])   # no stray temp file

    def test_blank_espn_response_is_an_outage_not_an_empty_schedule(self):
        prev = self._prev()                                          # has upcoming preseason games in the window
        self.assertIsNone(F.build_schedule(prev, days=3, now=NOW, get=FakeESPN(), sleep=NOSLEEP))

    def test_offseason_empty_is_fine_without_upcoming_prev(self):
        pay = F.build_schedule({"games": []}, days=3, now=NOW, get=FakeESPN(), sleep=NOSLEEP)
        self.assertEqual(pay["games"], [])

    def test_vanished_line_carried_then_expires(self):
        sb = fx("scoreboard_regular_season_with_odds.json")
        now = datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc)
        first = F.build_schedule({}, days=1, now=now, get=FakeESPN({"20261022": sb}), sleep=NOSLEEP, core_odds=False)
        g1 = {g["id"]: g for g in first["games"]}["401800001"]
        self.assertEqual(g1["homeML"], -270)
        prev = {"generated_at": "2026-10-22 12:00 UTC", **first}
        stripped = copy.deepcopy(sb)
        for ev in stripped["events"]:
            ev["competitions"][0].pop("odds", None)
        later = datetime(2026, 10, 22, 16, 0, tzinfo=timezone.utc)
        second = F.build_schedule(prev, days=1, now=later, get=FakeESPN({"20261022": stripped}), sleep=NOSLEEP, core_odds=False)
        g2 = {g["id"]: g for g in second["games"]}["401800001"]
        self.assertTrue(g2["oddsCarried"])
        self.assertEqual((g2["homeML"], g2["spread"], g2["overUnder"]), (-270, -6.5, 224.5))
        self.assertEqual(g2["oddsAt"], "2026-10-22T12:00:00Z")       # real age stays visible, not refreshed
        too_late = datetime(2026, 10, 24, 1, 0, tzinfo=timezone.utc)  # > 36 h after oddsAt
        third = F.build_schedule(prev, days=1, now=too_late, get=FakeESPN({"20261023": stripped, "20261022": stripped}), sleep=NOSLEEP, core_odds=False)
        for g in third["games"]:
            self.assertNotIn("oddsCarried", g)
        # a live/final game never inherits a stale pre-game line
        self.assertEqual(second["meta"]["odds_carried"].count("401800004"), 0)

    def test_core_odds_fallback_fills_near_games_only(self):
        sb = fx("scoreboard_regular_season_with_odds.json")
        stripped = copy.deepcopy(sb)
        for ev in stripped["events"]:
            ev["competitions"][0].pop("odds", None)
        now = datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc)
        item = fx("core_odds_item_recorded_nhl.json")
        core = {F.ESPN_CORE_ODDS.format(eid="401800001"): {"items": [item]}}
        espn = FakeESPN({"20261022": stripped}, core=core)
        pay = F.build_schedule({}, days=1, now=now, get=espn, sleep=NOSLEEP)
        by = {g["id"]: g for g in pay["games"]}
        self.assertEqual(by["401800001"]["homeML"], -230)
        self.assertEqual(pay["meta"]["core_odds_fills"], 1)
        core_calls = [u for u, _ in espn.calls if "core.api" in u]
        self.assertNotIn(F.ESPN_CORE_ODDS.format(eid="401800006"), core_calls)   # preseason: never asked
        self.assertNotIn(F.ESPN_CORE_ODDS.format(eid="401800003"), core_calls)   # live game: never asked
        self.assertLessEqual(len(core_calls), F.CORE_ODDS_MAX)

    def test_http_retries(self):
        calls = []

        class R:
            def __init__(self, code, body=None):
                self.status_code, self._b = code, body

            def json(self):
                return self._b

        seq = [R(503), R(429), R(200, {"events": []})]

        def getter(url, params=None, headers=None, timeout=None):
            calls.append(1)
            return seq[len(calls) - 1]

        sess = mock.Mock(get=getter)
        slept = []
        self.assertEqual(F.get_json("http://x", {}, session=sess, sleep=slept.append), {"events": []})
        self.assertEqual(len(calls), 3)
        self.assertEqual(slept, [1.0, 2.0])
        calls.clear()
        seq[:] = [R(403)]
        with self.assertRaises(RuntimeError):
            F.get_json("http://x", {}, session=sess, sleep=slept.append)
        self.assertEqual(len(calls), 1)                              # 403 is not retried


class Writing(unittest.TestCase):
    def test_generated_at_first_key_and_format(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "sub" / "nba_schedule.json"
            pay = F.build_schedule({}, days=1, now=NOW, get=FakeESPN({"20261003": fx("scoreboard_20261003_preseason.json")}), sleep=NOSLEEP)
            F.write_schedule(out, pay, now=NOW)
            raw = out.read_text()
            doc = json.loads(raw)
            self.assertEqual(list(doc)[0], "generated_at")
            self.assertRegex(doc["generated_at"], r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2} UTC$")
            self.assertEqual(doc["generated_at"], "2026-10-03 20:00 UTC")
            # the app's freshness line reads the first ~1 KB with a regex: the stamp must be inside it
            self.assertRegex(raw[:1024], r'"generated_at"\s*:\s*"2026-10-03 20:00 UTC"')
            self.assertEqual(list(doc)[1:4], ["season", "seasonYear", "source"])
            self.assertEqual(list(doc)[-1], "games")
            self.assertEqual(doc["season"], "2026-27")
            self.assertEqual(doc["seasonYear"], 2027)

    def test_atomic_write_never_leaves_partial_file(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "nba_schedule.json"
            out.write_text('{"generated_at": "OLD"}')
            with mock.patch.object(F.os, "replace", side_effect=OSError("disk")):
                with self.assertRaises(OSError):
                    F.write_schedule(out, {"games": []}, now=NOW)
            self.assertEqual(out.read_text(), '{"generated_at": "OLD"}')              # old file intact
            self.assertEqual([p.name for p in Path(td).iterdir()], ["nba_schedule.json"])   # temp cleaned up

    def test_dry_run_writes_nothing(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "n.json"
            espn = FakeESPN({"20261003": fx("scoreboard_20261003_preseason.json")})
            self.assertEqual(F.run(out, 1, dry_run=True, now=NOW, get=espn, sleep=NOSLEEP, health=False), 0)
            self.assertFalse(out.exists())

    def test_main_skips_health_log_for_a_non_default_out(self):
        # --out elsewhere (e.g. /tmp) must not touch docs/scraper_health.json, and --push must be ignored
        with tempfile.TemporaryDirectory() as td, mock.patch.object(F, "run", return_value=0) as r, mock.patch.object(F, "git_push") as gp:
            self.assertEqual(F.main(["--out", str(Path(td) / "x.json"), "--days", "1", "--push"]), 0)
            self.assertFalse(r.call_args.kwargs["health"])
            gp.assert_not_called()
        with mock.patch.object(F, "run", return_value=0) as r, mock.patch.object(F, "git_push") as gp, \
                mock.patch("_scraper_health.commit_and_push"):
            self.assertEqual(F.main(["--push"]), 0)
            self.assertTrue(r.call_args.kwargs["health"])
            gp.assert_called_once_with(["docs/nba_schedule.json"], "chore: refresh NBA schedule/lines")

    def test_health_logged_for_real_out_path_arguments(self):
        with tempfile.TemporaryDirectory() as td, mock.patch("_scraper_health.log_scrape") as ls:
            espn = FakeESPN({"20261003": fx("scoreboard_20261003_preseason.json")})
            self.assertEqual(F.run(Path(td) / "n.json", 1, now=NOW, get=espn, sleep=NOSLEEP, health=True), 0)
            ls.assert_called_once_with("NBA", 1, 0)

    def test_default_output_path_is_docs(self):
        self.assertEqual(F.SCHEDULE_OUT, ROOT / "docs" / "nba_schedule.json")
        self.assertEqual(F.HEADERS, {})                              # ESPN 403s custom User-Agents


class Wiring(unittest.TestCase):
    def test_lock_prep_job_registered(self):
        argv, files = LP.JOBS["nba"]
        self.assertEqual(argv, ["scripts/fetch_nba.py"])
        self.assertEqual(files, ["docs/nba_schedule.json"])
        self.assertEqual(LP.ODDS_FILES["nba"], "docs/nba_schedule.json")
        self.assertEqual(LP.expand("nhl,nba,data-nhl"), ["nhl", "nba", "data-nhl"])
        self.assertTrue((ROOT / argv[0]).exists())

    def test_lock_prep_commits_the_file(self):
        calls = []

        def fake_run(cmd, *a, **k):
            calls.append(list(cmd))
            r = mock.Mock()
            r.returncode = 0 if "--quiet" not in cmd else 1
            return r

        with mock.patch.object(LP.subprocess, "run", side_effect=fake_run), mock.patch.object(LP.time, "sleep"):
            LP.commit_and_push(["nba"])
        self.assertIn("docs/nba_schedule.json", next(c for c in calls if "add" in c))

    def test_lock_prep_dry_run_lists_it(self):
        r = subprocess.run([sys.executable, str(ROOT / "scripts" / "lock_prep.py"), "--jobs", "nba", "--dry-run"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0)
        self.assertIn("scripts/fetch_nba.py", r.stdout)

    def test_freshness_report_understands_flat_nba_odds(self):
        import contextlib
        import io
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "docs").mkdir()
            stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            (Path(td) / "docs" / "nba_schedule.json").write_text(json.dumps({"games": [
                {"state": "pre", "homeML": -150, "awayML": 130, "oddsAt": stamp}, {"state": "pre", "homeML": None, "awayML": None}]}))
            out = io.StringIO()
            with mock.patch.object(LP, "ROOT", Path(td)), contextlib.redirect_stdout(out):
                LP.freshness_report(["nba"])
            self.assertIn("2 upcoming, 1 with a real price", out.getvalue())

    def test_workflows(self):
        wf = ROOT / ".github" / "workflows"
        a, w, d = ((wf / n).read_text() for n in ("auto-lock-settle.yml", "lock-watchdog.yml", "daily-schedules-refresh.yml"))
        self.assertIn("lock_prep.py --jobs nhl,nfl,nba,data-nhl", a)
        self.assertIn("lock_prep.py --jobs hockey,nhl,nba,data-nhl", w)
        self.assertIn("python3 scripts/fetch_nba.py --push", d)
        step = d.split("Refresh NBA schedule/lines")[1].split("- name:")[0]
        self.assertIn("continue-on-error: true", step)
        # NBA is not wired into the CFB-only / hockey-only / soccer lock workflows
        for n in ("cfb-lock-early.yml", "cfb-lock-evening.yml", "hockey-lock-evening.yml", "soccer-lock-evening.yml", "european-lock-early.yml"):
            self.assertNotIn("nba", (wf / n).read_text().split("lock_prep.py --jobs")[1].split("\n")[0])

    def test_health_check_watches_the_file(self):
        import daily_health_check as H
        self.assertIn("nba_schedule.json", [f[0] for f in H.FRESHNESS_FILES])


NODE = shutil.which("node")


@unittest.skipUnless(NODE, "node not installed")
class LockPassMerge(unittest.TestCase):
    """The NBA block of auto_lock_settle.gather_legs: extracted by its markers and executed with node."""

    @classmethod
    def setUpClass(cls):
        src = (HERE / "auto_lock_settle.py").read_text()
        m = re.search(r"// NBA-MERGE-BEGIN\n(.*?)// NBA-MERGE-END", src, re.S)
        assert m, "NBA-MERGE markers missing in auto_lock_settle.py"
        cls.js = m.group(1)
        assert "\\" not in cls.js, "backslash inside the non-raw Python string"

    def merge(self, cv, sched):
        prog = f"{self.js}\nconst [cv, sched] = JSON.parse(process.argv[1]);\nconsole.log(JSON.stringify(_nbaMerge(cv, sched)));"
        r = subprocess.run([NODE, "-e", prog, json.dumps([cv, sched])], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        return {g["id"]: g for g in json.loads(r.stdout)}

    @staticmethod
    def g(i, **kw):
        base = {"id": i, "home": "BOS", "away": "NY", "state": "pre", "date": "2026-10-22T23:30Z", "homeML": None, "awayML": None, "ou": None}
        base.update(kw)
        return base

    def test_no_schedule_file_is_exactly_the_old_behaviour(self):
        cv = {"generated": "2026-10-22T10:00:00+00:00", "nba": {"today": [self.g("1", homeML=-150, awayML=130, ou=221.5)]}}
        self.assertEqual(self.merge(cv, None), {"1": cv["nba"]["today"][0]})

    def test_fresher_schedule_wins_and_old_only_games_survive(self):
        cv = {"generated": "2026-10-22T10:00:00+00:00", "nba": {"today": [self.g("1", homeML=-150, awayML=130, ou=221.5), self.g("2", homeML=-110, awayML=-110, ou=210.5)]}}
        sched = {"generated_at": "2026-10-22 18:00 UTC", "games": [self.g("1", homeML=-170, awayML=145, ou=222.5), self.g("3", homeML=-300, awayML=250, ou=230.5)]}
        out = self.merge(cv, sched)
        self.assertEqual((out["1"]["homeML"], out["1"]["ou"]), (-170, 222.5))        # schedule newer -> its line
        self.assertEqual(out["2"]["homeML"], -110)                                    # only in data.json -> kept
        self.assertEqual(out["3"]["homeML"], -300)                                    # only in the schedule -> added

    def test_older_schedule_loses_to_newer_data_json(self):
        cv = {"generated": "2026-10-22T19:00:00+00:00", "nba": {"today": [self.g("1", homeML=-150, awayML=130, ou=221.5)]}}
        sched = {"generated_at": "2026-10-22 08:00 UTC", "games": [self.g("1", homeML=-999, awayML=999, ou=1.5)]}
        self.assertEqual(self.merge(cv, sched)["1"]["homeML"], -150)

    def test_missing_line_filled_from_the_other_file(self):
        cv = {"generated": "2026-10-22T10:00:00+00:00", "nba": {"today": [self.g("1", homeML=-150, awayML=130, ou=221.5)]}}
        sched = {"generated_at": "2026-10-22 18:00 UTC", "games": [self.g("1")]}
        self.assertEqual((self.merge(cv, sched)["1"]["homeML"], self.merge(cv, sched)["1"]["ou"]), (-150, 221.5))

    def test_preseason_flag_from_either_file_blocks(self):
        cv = {"generated": "2026-10-22T10:00:00+00:00", "nba": {"today": [self.g("1", homeML=-150, awayML=130, ou=221.5)]}}
        sched = {"generated_at": "2026-10-22 18:00 UTC", "games": [self.g("1", homeML=-170, awayML=140, ou=220.5, preseason=True, seasonType=1)]}
        self.assertEqual(self.merge(cv, sched)["1"]["seasonType"], 1)                 # the lock loop skips seasonType === 1
        cv2 = {"generated": "2026-10-22T10:00:00+00:00", "nba": {"today": [self.g("1", seasonType=1)]}}
        sched2 = {"generated_at": "2026-10-22 18:00 UTC", "games": [self.g("1", homeML=-170, awayML=140, ou=220.5, seasonType=2)]}
        self.assertEqual(self.merge(cv2, sched2)["1"]["seasonType"], 1)               # data.json says preseason, schedule says regular -> still blocked
        sched3 = {"generated_at": "2026-10-22 18:00 UTC", "games": [self.g("9", preseason=True, seasonType=1)]}
        self.assertEqual(self.merge({"nba": {"today": []}}, sched3)["9"]["seasonType"], 1)

    def test_carried_and_postponed_schedule_rows_ignored(self):
        sched = {"generated_at": "2026-10-22 18:00 UTC", "games": [self.g("1", carried=True), self.g("2", postponed=True), self.g("3")]}
        self.assertEqual(sorted(self.merge({"nba": {"today": []}}, sched)), ["3"])

    def test_garbage_inputs_do_not_throw(self):
        self.assertEqual(self.merge({}, {"games": "nope"}), {})
        self.assertEqual(self.merge({"nba": {"today": None}}, {"generated_at": "bad", "games": [self.g("1")]}).keys(), {"1"})

    def test_python_block_still_filters_preseason_and_today(self):
        src = (HERE / "auto_lock_settle.py").read_text()
        blk = src.split("const nbaToday = _nbaMerge(cvData, nbaSched);")[1].split("const warmups")[0]
        self.assertIn("g.seasonType === 1", blk)                                      # the original skip, untouched
        self.assertIn("g.state === 'post'", blk)
        self.assertIn("_nbaGameCard(espnEv)", blk)


class RegularSeasonDays(unittest.TestCase):
    """The NBA tab's date dropdown: regular-season game days only (no preseason / play-in / playoffs), carried when ESPN hiccups."""
    CAL = ["2026-10-03", "2026-10-19", "2026-10-20", "2026-10-21", "2027-04-11", "2027-04-14", "2027-05-01"]
    TYPE2 = {"startDate": "2026-10-20T07:00Z", "endDate": "2027-04-12T06:59Z"}

    def test_filters_to_the_regular_season_range(self):
        core = {F.ESPN_CORE_SEASON_TYPE.format(year=2027): self.TYPE2}
        rng, days = F.regular_season_days({"_calendar": self.CAL}, 2027, get=FakeESPN(core=core))
        self.assertEqual(rng, {"start": "2026-10-20", "end": "2027-04-12"})
        self.assertEqual(days, ["2026-10-20", "2026-10-21", "2027-04-11"])             # preseason (10-03, 10-19) and playoffs (04-14, 05-01) are out

    def test_failure_keeps_the_previous_values(self):
        prev = {"regularSeason": {"start": "a", "end": "b"}, "regularSeasonDays": ["2026-10-20"]}
        rng, days = F.regular_season_days({"_calendar": self.CAL}, 2027, get=FakeESPN(), prev_doc=prev)       # core call 404s
        self.assertEqual((rng, days), (prev["regularSeason"], prev["regularSeasonDays"]))
        rng, days = F.regular_season_days({}, 2027, get=FakeESPN(), prev_doc=prev)                            # no calendar in the response
        self.assertEqual(days, ["2026-10-20"])
        self.assertEqual(F.regular_season_days({}, 2027, get=FakeESPN()), (None, None))

    def test_build_schedule_writes_the_days(self):
        core = {F.ESPN_CORE_SEASON_TYPE.format(year=2027): self.TYPE2}
        board = {"leagues": [{"season": {"year": 2027, "displayName": "2026-27"}, "calendar": [c + "T07:00Z" for c in self.CAL]}], "events": []}
        espn = FakeESPN({d: board for d in ("20261002", "20261003", "20261004", "20261005")}, core=core)
        pay = F.build_schedule({}, days=2, now=NOW, get=espn, sleep=NOSLEEP)
        self.assertEqual(pay["regularSeasonDays"], ["2026-10-20", "2026-10-21", "2027-04-11"])
        self.assertEqual(pay["regularSeason"]["start"], "2026-10-20")
        self.assertNotIn("_calendar", json.dumps(pay))


if __name__ == "__main__":
    unittest.main(verbosity=1)
