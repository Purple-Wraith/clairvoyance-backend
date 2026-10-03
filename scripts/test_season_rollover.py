#!/usr/bin/env python3
"""Offline tests for the 2026-10-03 season-rollover / dead-API cleanup (no network):

  * scripts/_season.py                  -- football / hockey / soccer season derived from the date (+ env overrides)
  * _season.roll_prior_snapshot + fetch_cfb.write_stats -- cfb_team_stats_prior.json rolls itself, never mid-season
  * scripts/clairvoyance_update.py      -- NHL season helpers + playoff bracket (no hardcoded season=2026, one INFO line on 404),
                                           and no The Odds API request anywhere (bundle keys stay present-but-empty)
  * the committed docs/cfb_team_stats_prior.json is a byte copy of cfb_team_stats_2025.json (same schema the app reads)

    /usr/bin/python3 scripts/test_season_rollover.py     # the clairvoyance_update.py tests need bs4/lxml (else skipped)
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import re
import sys
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
import _season as S  # noqa: E402


class FootballSeason(unittest.TestCase):
    def test_boundaries(self):
        f = lambda d, **k: S.football_season_year("cfb", today=d, environ={}, **k)      # noqa: E731
        self.assertEqual(f(date(2026, 6, 30)), 2025)
        self.assertEqual(f(date(2026, 7, 1)), 2026)
        self.assertEqual(f(date(2026, 10, 3)), 2026)
        self.assertEqual(f(date(2027, 1, 20)), 2026)       # bowls / playoff still belong to the 2026 season
        self.assertEqual(f(date(2027, 2, 14)), 2026)       # Super Bowl
        self.assertEqual(f(date(2027, 7, 1)), 2027)        # nothing to hand-edit when 2027 starts
        self.assertEqual(f(datetime(2027, 9, 5, 3, 0, tzinfo=timezone.utc)), 2027)

    def test_env_overrides(self):
        d = date(2026, 10, 3)
        self.assertEqual(S.football_season_year("cfb", d, {"CFB_SEASON_YEAR": "2025"}), 2025)
        self.assertEqual(S.football_season_year("nfl", d, {"CFB_SEASON_YEAR": "2025"}), 2026)            # sport-specific only
        self.assertEqual(S.football_season_year("nfl", d, {"FOOTBALL_SEASON_YEAR": "2027"}), 2027)
        self.assertEqual(S.football_season_year("nfl", d, {"NFL_SEASON_YEAR": "2025", "FOOTBALL_SEASON_YEAR": "2027"}), 2025)
        for bad in ("abc", "1999", "20260", " "):
            self.assertEqual(S.football_season_year("nfl", d, {"NFL_SEASON_YEAR": bad}), 2026, bad)

    def test_hockey_label(self):
        h = lambda d, env=None: S.hockey_season_label(d, env or {})                    # noqa: E731
        self.assertEqual(h(date(2026, 8, 31)), "2025-26")
        self.assertEqual(h(date(2026, 9, 1)), "2026-27")
        self.assertEqual(h(date(2027, 3, 1)), "2026-27")
        self.assertEqual(h(date(2027, 9, 1)), "2027-28")
        self.assertEqual(h(date(2099, 10, 1)), "2099-00")
        self.assertEqual(h(date(2026, 10, 1), {"QUANTHOCKEY_SEASON": "2030-31"}), "2030-31")
        self.assertEqual(h(date(2026, 10, 1), {"QUANTHOCKEY_SEASON": "bogus"}), "2026-27")

    def test_soccer(self):
        self.assertEqual(S.soccer_season_start_year(date(2027, 6, 30), {}), 2026)
        self.assertEqual(S.soccer_season_start_year(date(2027, 7, 1), {}), 2027)
        self.assertEqual(S.soccer_season_start_year(date(2027, 7, 1), {"SOCCER_SEASON_START_YEAR": "2026"}), 2026)


def teams(n=3):
    return {f"T{i}": {"offense": {"ppg": 20 + i}} for i in range(n)}


def write(p, season, tms=None, **extra):
    Path(p).write_text(json.dumps({"generated_at": "x", "season": season, "teams": teams() if tms is None else tms, **extra}, indent=2))


class RollPriorSnapshot(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        self.cur = Path(self.td.name) / "cfb_team_stats.json"
        self.prior = Path(self.td.name) / "cfb_team_stats_prior.json"

    def prior_season(self):
        return json.loads(self.prior.read_text())["season"]

    def test_rolls_when_a_newer_season_replaces_it(self):
        write(self.cur, 2026)
        write(self.prior, 2025)
        self.assertEqual(S.roll_prior_snapshot(self.cur, self.prior, 2027), "rolled")
        got = json.loads(self.prior.read_text())
        self.assertEqual(got["season"], 2026)
        self.assertEqual(got["teams"], teams())                       # same schema the app reads (teams[abbr].offense...)
        self.assertIn("rolled_at", got)
        self.assertEqual([p.name for p in Path(self.td.name).iterdir() if ".tmp" in p.name], [])

    def test_creates_prior_when_missing(self):
        write(self.cur, 2026)
        self.assertEqual(S.roll_prior_snapshot(self.cur, self.prior, 2027), "rolled")
        self.assertEqual(self.prior_season(), 2026)

    def test_never_overwritten_mid_season(self):
        write(self.cur, 2026)
        write(self.prior, 2025)
        self.assertEqual(S.roll_prior_snapshot(self.cur, self.prior, 2026), "same-season")       # the 2026 season's own refreshes
        self.assertEqual(self.prior_season(), 2025)
        # 2027 starts: first refresh rolls ...
        self.assertEqual(S.roll_prior_snapshot(self.cur, self.prior, 2027), "rolled")
        write(self.cur, 2027)                                                                      # (the producer then writes 2027 stats)
        # ... and every later 2027 refresh leaves the 2026 snapshot alone
        self.assertEqual(S.roll_prior_snapshot(self.cur, self.prior, 2027), "same-season")
        self.assertEqual(self.prior_season(), 2026)

    def test_idempotent_if_called_twice_before_the_stats_file_is_replaced(self):
        write(self.cur, 2026)
        S.roll_prior_snapshot(self.cur, self.prior, 2027)
        before = self.prior.read_bytes()
        self.assertEqual(S.roll_prior_snapshot(self.cur, self.prior, 2027), "already")
        self.assertEqual(self.prior.read_bytes(), before)

    def test_skipped_season_is_not_relabelled(self):
        write(self.cur, 2025)
        self.assertEqual(S.roll_prior_snapshot(self.cur, self.prior, 2027), "gap")
        self.assertFalse(self.prior.exists())

    def test_missing_or_broken_current_file(self):
        self.assertEqual(S.roll_prior_snapshot(self.cur, self.prior, 2027), "no-current")
        self.cur.write_text("{nope")
        self.assertEqual(S.roll_prior_snapshot(self.cur, self.prior, 2027), "no-current")
        self.cur.write_text(json.dumps({"teams": teams()}))               # no season field
        self.assertEqual(S.roll_prior_snapshot(self.cur, self.prior, 2027), "no-current")

    def test_empty_teams_are_not_rolled(self):
        write(self.cur, 2026, tms={})
        self.assertEqual(S.roll_prior_snapshot(self.cur, self.prior, 2027), "gap")
        self.assertFalse(self.prior.exists())


class CfbProducer(unittest.TestCase):
    """fetch_cfb.write_stats end to end with the ESPN calls faked: the 2026 -> 2027 rollover needs no hand-editing."""

    @classmethod
    def setUpClass(cls):
        import fetch_cfb
        cls.F = fetch_cfb

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        d = Path(self.td.name)
        self.roster = d / "cfb_teams.json"
        self.roster.write_text(json.dumps({"conferences": {"SEC": [{"id": "1", "abbr": "T0", "name": "Zero"},
                                                                     {"id": "2", "abbr": "T1", "name": "One"}]}}))
        self.stats = d / "cfb_team_stats.json"
        self.prior = d / "cfb_team_stats_prior.json"
        self.calls = []

    def run_write_stats(self, today, have_games_for):
        F = self.F

        def fake_fetch(roster, season=None):
            self.calls.append(season)
            return teams(2) if season in have_games_for else {}
        with contextlib.ExitStack() as st:
            st.enter_context(mock.patch.object(F, "OUT_PATH", self.roster))
            st.enter_context(mock.patch.object(F, "STATS_OUT_PATH", self.stats))
            st.enter_context(mock.patch.object(F, "PRIOR_STATS_PATH", self.prior))
            st.enter_context(mock.patch.object(F, "fetch_all_team_stats", fake_fetch))
            st.enter_context(mock.patch.object(F, "football_season_year", lambda sport=None, today_=None, **k: S.football_season_year(sport, today, {})))
            st.enter_context(contextlib.redirect_stdout(io.StringIO()))
            F.write_stats()

    def test_full_year_cycle(self):
        write(self.stats, 2026, tms=teams(2))
        write(self.prior, 2025)
        # Oct 2026: mid-season refresh. Prior (2025) untouched.
        self.run_write_stats(date(2026, 10, 3), have_games_for={2026})
        self.assertEqual(json.loads(self.stats.read_text())["season"], 2026)
        self.assertEqual(json.loads(self.prior.read_text())["season"], 2025)
        # Feb 2027: still the 2026 season (derived from the date, not the calendar year)
        self.calls.clear()
        self.run_write_stats(date(2027, 2, 1), have_games_for={2026})
        self.assertEqual(self.calls[0], 2026)
        self.assertEqual(json.loads(self.prior.read_text())["season"], 2025)
        # Jul-Aug 2027: 2027 has no games yet -> falls back to 2026; prior still untouched
        self.calls.clear()
        self.run_write_stats(date(2027, 8, 1), have_games_for={2026})
        self.assertEqual(self.calls, [2027, 2026])
        self.assertEqual(json.loads(self.stats.read_text())["season"], 2026)
        self.assertEqual(json.loads(self.prior.read_text())["season"], 2025)
        # Sep 2027: 2027 games posted -> the 2026 file is rolled into prior, then replaced
        self.run_write_stats(date(2027, 9, 12), have_games_for={2026, 2027})
        self.assertEqual(json.loads(self.stats.read_text())["season"], 2027)
        self.assertEqual(json.loads(self.prior.read_text())["season"], 2026)
        # Oct 2027: later refreshes keep the 2026 snapshot
        self.run_write_stats(date(2027, 10, 10), have_games_for={2027})
        self.assertEqual(json.loads(self.prior.read_text())["season"], 2026)

    def test_git_push_only_adds_files_that_exist(self):
        F = self.F
        seen = []

        def fake_run(cmd, *a, **k):
            seen.append(list(cmd))
            r = mock.Mock()
            r.returncode = 0
            return r
        with mock.patch.object(F, "ROOT", Path(self.td.name)), mock.patch("subprocess.run", side_effect=fake_run), \
                contextlib.redirect_stdout(io.StringIO()):
            (Path(self.td.name) / "docs").mkdir()
            (Path(self.td.name) / "docs" / "cfb_team_stats.json").write_text("{}")
            F.git_push(["docs/cfb_team_stats.json", "docs/cfb_team_stats_prior.json"], "m")
        add = next(c for c in seen if "add" in c)
        self.assertIn("docs/cfb_team_stats.json", add)
        self.assertNotIn("docs/cfb_team_stats_prior.json", add)       # a missing path would fail the whole `git add`

    def test_defaults_are_derived_not_constants(self):
        F = self.F
        with mock.patch.dict(os.environ, {"CFB_SEASON_YEAR": "2031"}):
            seen = []
            with mock.patch.object(F, "fetch_team_stats", lambda tid, season: seen.append(season) or None), \
                    mock.patch.object(F.time, "sleep"), contextlib.redirect_stdout(io.StringIO()):
                F.fetch_all_team_stats({"SEC": [{"id": "1", "abbr": "T0", "name": "Z"}]})
            self.assertEqual(seen, [2031])


class NoHardcodedSeasons(unittest.TestCase):
    def read(self, name):
        return (HERE / name).read_text()

    def test_football_scripts(self):
        for name in ("fetch_cfb.py", "fetch_nfl.py"):
            src = self.read(name)
            self.assertNotRegex(src, r"default=202\d", name)
            self.assertNotRegex(src, r"season: int = 202\d", name)
            self.assertNotRegex(src, r"year: int = 202\d", name)
            self.assertNotIn('time.strftime("%Y"', src.replace('"generated_at": time.strftime("%Y-', ""), name)   # calendar year != season
            self.assertIn("football_season_year", src, name)

    def test_quanthockey_season_is_derived(self):
        src = self.read("fetch_quanthockey.py")
        self.assertNotRegex(src, r'SEASON = "20\d\d-\d\d"')
        self.assertIn("hockey_season_label()", src)

    def test_snapshot_tool_generalised(self):
        self.assertFalse((HERE / "snapshot_cfb_2025.py").exists())
        src = self.read("snapshot_cfb_prior.py")
        self.assertIn("cfb_team_stats_prior.json", src)
        self.assertNotRegex(src, r"season=2025")

    def test_committed_prior_snapshot_is_a_byte_copy_with_the_schema_the_app_reads(self):
        old = (ROOT / "docs" / "cfb_team_stats_2025.json").read_bytes()
        new = (ROOT / "docs" / "cfb_team_stats_prior.json").read_bytes()
        self.assertEqual(old, new)
        d = json.loads(new)
        self.assertEqual(d["season"], 2025)
        cur = json.loads((ROOT / "docs" / "cfb_team_stats.json").read_text())
        self.assertEqual(sorted(d.keys()), sorted(["generated_at", "season", "note", "teams"]))
        t0 = next(iter(d["teams"].values()))
        c0 = next(iter(cur["teams"].values()))
        self.assertEqual(sorted(t0.keys()), sorted(c0.keys()))


HAVE_BS4 = importlib.util.find_spec("bs4") is not None and importlib.util.find_spec("lxml") is not None


@unittest.skipUnless(HAVE_BS4, "bs4/lxml not installed in this interpreter (clairvoyance_update.py would pip-install them)")
class ClairvoyanceUpdate(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("cu_season_test", HERE / "clairvoyance_update.py")
        cls.cu = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.cu)
        cls.src = (HERE / "clairvoyance_update.py").read_text()

    # ── NHL season / playoffs (item C) ──
    def test_nhl_season_end_year(self):
        f = self.cu.nhl_season_end_year
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("NHL_SEASON_END_YEAR", None)
            self.assertEqual(f(date(2026, 8, 31)), 2026)
            self.assertEqual(f(date(2026, 9, 1)), 2027)
            self.assertEqual(f(date(2026, 10, 3)), 2027)
            self.assertEqual(f(date(2027, 5, 20)), 2027)
            self.assertEqual(f(date(2027, 10, 1)), 2028)
            self.assertEqual(f(date(2026, 10, 3), override="2030"), 2030)
        with mock.patch.dict(os.environ, {"NHL_SEASON_END_YEAR": "2029"}):
            self.assertEqual(f(date(2026, 10, 3)), 2029)

    def test_playoff_window(self):
        f = self.cu.nhl_in_playoff_window
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("NHL_PLAYOFFS_FORCE", None)
            self.assertFalse(f(date(2026, 10, 3)))
            self.assertFalse(f(date(2027, 3, 31)))
            self.assertTrue(f(date(2027, 4, 1)))
            self.assertTrue(f(date(2027, 6, 30)))
            self.assertFalse(f(date(2027, 7, 1)))
        with mock.patch.dict(os.environ, {"NHL_PLAYOFFS_FORCE": "1"}):
            self.assertTrue(f(date(2026, 10, 3)))

    def _resp(self, status, body=None):
        r = mock.Mock()
        r.status_code = status
        r.json.return_value = body
        r.raise_for_status.side_effect = None if status < 400 else RuntimeError(f"HTTP {status}")
        return r

    def test_bracket_skipped_outside_window_without_any_request(self):
        out = io.StringIO()
        with mock.patch.object(self.cu._session, "get", side_effect=AssertionError("must not call ESPN")), \
                mock.patch.object(self.cu, "fetch_json", side_effect=AssertionError("must not call ESPN")), \
                mock.patch.dict(os.environ, {}, clear=False), contextlib.redirect_stdout(out):
            os.environ.pop("NHL_PLAYOFFS_FORCE", None)
            self.assertEqual(self.cu.fetch_nhl_playoff_bracket(date(2026, 10, 3)), {})
        text = out.getvalue()
        self.assertEqual(text.count("NHL playoff bracket"), 1)         # one concise line
        self.assertNotIn("WARN", text)
        self.assertIn("INFO", text)

    def test_bracket_404_in_window_is_one_info_line_not_a_warn_storm(self):
        out = io.StringIO()
        with mock.patch.object(self.cu._session, "get", return_value=self._resp(404)) as g, \
                mock.patch.object(self.cu.time, "sleep"), contextlib.redirect_stdout(out):
            self.assertEqual(self.cu.fetch_nhl_playoff_bracket(date(2027, 4, 20)), {})
        self.assertEqual(g.call_count, 1)                              # no retries
        self.assertIn("season=2027", g.call_args[0][0])
        self.assertNotIn("WARN", out.getvalue())
        self.assertNotIn("FAILED", out.getvalue())

    def test_bracket_in_window_with_data(self):
        with mock.patch.object(self.cu._session, "get", return_value=self._resp(200, {"series": [1]})) as g, \
                contextlib.redirect_stdout(io.StringIO()):
            got = self.cu.fetch_nhl_playoff_bracket(date(2027, 5, 2))
        self.assertEqual(got["raw"], {"series": [1]})
        self.assertEqual(got["season"], 2027)
        self.assertIn("hockey/nhl/playoffs?season=2027", g.call_args[0][0])

    def test_bracket_network_error_is_quiet_and_empty(self):
        out = io.StringIO()
        with mock.patch.object(self.cu._session, "get", side_effect=OSError("down")), contextlib.redirect_stdout(out):
            self.assertEqual(self.cu.fetch_nhl_playoff_bracket(date(2027, 5, 2)), {})
        self.assertNotIn("WARN", out.getvalue())

    def test_no_hardcoded_playoffs_season_left(self):
        self.assertNotIn("playoffs?season=2026", self.src)
        self.assertNotRegex(self.src, r"f\"[^\"]*season=20\d\d")      # no f-string URL with a literal season year

    def test_nhl_stats_season_env_override(self):
        with mock.patch.dict(os.environ, {"NHL_SEASON_START_YEAR": "2027"}):
            self.assertEqual(self.cu._nhl_current_season_id(), "20272028")
        with mock.patch.dict(os.environ, {"NHL_SEASON_START_YEAR": "junk"}):
            self.assertRegex(self.cu._nhl_current_season_id(), r"^20\d\d20\d\d$")

    # ── The Odds API is gone (item E) ──
    def test_no_the_odds_api_request_in_the_script(self):
        code = "\n".join(l for l in self.src.splitlines() if not l.lstrip().startswith("#"))
        self.assertNotIn("api.the-odds-api.com", code)
        self.assertNotIn('os.environ.get("ODDS_API_KEY"', code)
        self.assertNotIn("apiKey", code)

    def test_fetch_best_odds_is_espn_only_even_if_a_key_is_set(self):
        games = [{"home": "BOS", "away": "NYR", "homeML": -150, "awayML": 130, "ou": 5.5}, {"home": "A", "away": "B"}]
        with mock.patch.dict(os.environ, {"ODDS_API_KEY": "should-be-ignored"}), \
                mock.patch.object(self.cu, "fetch_json", side_effect=AssertionError("no HTTP")), \
                mock.patch.object(self.cu._session, "get", side_effect=AssertionError("no HTTP")):
            out = self.cu.fetch_best_odds("nhl", games)
            self.assertEqual(out["BOS:NYR"], {"homeML": -150, "awayML": 130, "ou": 5.5, "book": "ESPN"})
            self.assertEqual(self.cu.fetch_best_odds("cfb", []), {})
            self.assertEqual(self.cu.fetch_best_odds("pl", [], name_resolver=lambda n: n), {})

    def test_futures_stays_present_but_empty_and_makes_no_request(self):
        with mock.patch.dict(os.environ, {"ODDS_API_KEY": "x"}), mock.patch.object(self.cu, "fetch_json", side_effect=AssertionError("no HTTP")):
            self.assertEqual(self.cu.fetch_futures_odds(), {"mlb": [], "nba": [], "nhl": [], "golf": [], "source": "none"})

    def test_bundle_keys_still_present(self):
        for key in ('"futures":', '"bestOdds":', '"bestOddsExt":'):
            self.assertIn(key, self.src)
        for k in ("wnba", "nfl", "cfb", "pl", "liga", "bl", "mls", "wc"):
            self.assertIn(f'"{k}": ', self.src[self.src.index('"bestOddsExt"'):][:400])
        d = json.loads((ROOT / "docs" / "data.json").read_text())
        for k in ("futures", "bestOdds", "bestOddsExt"):
            self.assertIn(k, d)


class OddsApiNotConfigured(unittest.TestCase):
    def test_workflows_do_not_pass_the_key(self):
        for wf in (ROOT / ".github" / "workflows").glob("*.yml"):
            for line in wf.read_text().splitlines():
                if "ODDS_API_KEY" in line:
                    self.assertTrue(line.lstrip().startswith("#"), f"{wf.name}: {line.strip()}")

    def test_app_reads_futures_and_best_odds_defensively(self):
        html = (ROOT / "docs" / "app.html").read_text()
        self.assertIn("const fut=D.futures||{};", html)           # renderFuturesOdds: empty/none -> "no key" placeholder, no crash
        self.assertNotRegex(html, r"D\.bestOdds|__CV_DATA\.bestOdds|\.bestOddsExt")      # nothing in the app consumes these keys


if __name__ == "__main__":
    unittest.main(verbosity=1)
