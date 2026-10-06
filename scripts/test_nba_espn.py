#!/usr/bin/env python3
"""Offline tests for the ESPN-sourced NBA team ratings + player-prop inputs (scripts/_nba_espn.py and its wiring in
scripts/clairvoyance_update.py).  No network: the HTTP layer is faked with small recorded ESPN payloads in
scripts/fixtures/nba_espn/ (recorded 2026-10-03: statistics/byteam for the 2025-26 regular season, a page of
statistics/byathlete sorted by minutes, Nikola Jokic's athletes/{id}/gamelog, a slice of the /injuries feed).

    python3 scripts/test_nba_espn.py                 # pure-module tests always run; wiring tests need bs4/lxml
    /usr/bin/python3 scripts/test_nba_espn.py        # everything (the interpreter test_nba_rollover.py uses)
"""
from __future__ import annotations

import copy
import importlib.util
import json
import os
import statistics
import sys
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
FIX = HERE / "fixtures" / "nba_espn"
BB_FIX = HERE / "fixtures" / "nba_rollover"
sys.path.insert(0, str(HERE))
import _nba_espn as ne  # noqa: E402

BYTEAM_2026 = json.loads((FIX / "byteam_2026.json").read_text())
BYATHLETE = json.loads((FIX / "byathlete_2026_sample.json").read_text())
GAMELOG = json.loads((FIX / "gamelog_jokic_2026.json").read_text())
INJURIES = json.loads((FIX / "injuries_sample.json").read_text())
JOKIC_ID = "3112335"

ESPN_30 = {"ATL", "BKN", "BOS", "CHA", "CHI", "CLE", "DAL", "DEN", "DET", "GS", "HOU", "IND", "LAC", "LAL",
           "MEM", "MIA", "MIL", "MIN", "NO", "NY", "OKC", "ORL", "PHI", "PHX", "POR", "SA", "SAC", "TOR",
           "UTAH", "WSH"}

HAVE_CU = importlib.util.find_spec("bs4") is not None and importlib.util.find_spec("lxml") is not None
cu = None
if HAVE_CU:
    _spec = importlib.util.spec_from_file_location("cu_nba_espn_test", HERE / "clairvoyance_update.py")
    cu = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(cu)
    HTML_2026 = (BB_FIX / "bbref_NBA_2026_advanced_team.html").read_text(encoding="utf-8")


# ── fakes ────────────────────────────────────────────────────────────────────────────────────────────────────
class Resp:
    def __init__(self, status=200, body=None, text=None):
        self.status_code, self._body = status, body
        self.text = text if text is not None else json.dumps(body)

    def json(self):
        if self._body is None:
            raise ValueError("no JSON")
        return self._body


NOT_FOUND = lambda: Resp(500, {"code": 2404, "detail": "http error: not found"})   # what ESPN really returns for season 2027 byteam


class Scripted:
    """getter(url, params, timeout): pops scripted outcomes (Resp or Exception) in order."""
    def __init__(self, *outcomes):
        self.outcomes, self.calls = list(outcomes), []

    def __call__(self, url, params=None, timeout=None):
        self.calls.append((url, params))
        o = self.outcomes.pop(0)
        if isinstance(o, Exception):
            raise o
        return o


def scale_byteam(data, season, gp, frac):
    """Synthetic 'season underway' response from the 2025-26 fixture: every total x frac, gamesPlayed = gp."""
    d = copy.deepcopy(data)
    d["requestedSeason"]["year"] = season
    names = {c["name"]: c["names"] for c in d["categories"]}
    for t in d["teams"]:
        for c in t["categories"]:
            gi = names[c["name"]].index("gamesPlayed") if c["name"] == "general" else None
            c["values"] = [round(v * frac, 3) if i != gi else gp for i, v in enumerate(c["values"])]
    return d


class FakeEspn:
    """Routes ESPN URLs to fixtures.  `overrides` maps a label ("byteam:2027", "gamelog", "injuries", "byathlete") to a callable
    returning Resp (or an Exception to raise).  Records every request."""
    def __init__(self, overrides=None):
        self.overrides, self.calls = overrides or {}, []
        self.gamelog_ids = []

    def __call__(self, url, params=None, timeout=None):
        params = params or {}
        self.calls.append((url, dict(params)))
        if "statistics/byteam" in url:
            key = f"byteam:{params.get('season')}"
            if key in self.overrides:
                return self._ov(key)
            if params.get("season") == 2026:
                return Resp(200, BYTEAM_2026)
            return NOT_FOUND()
        if "statistics/byathlete" in url:
            if "byathlete" in self.overrides:
                return self._ov("byathlete")
            return Resp(200, BYATHLETE) if params.get("page") == 1 else Resp(200, {"athletes": [], "categories": BYATHLETE["categories"]})
        if "/gamelog" in url:
            pid = url.split("/athletes/")[1].split("/")[0]
            self.gamelog_ids.append(pid)
            if "gamelog" in self.overrides:
                return self._ov("gamelog")
            return Resp(200, GAMELOG)
        if "/injuries" in url:
            if "injuries" in self.overrides:
                return self._ov("injuries")
            return Resp(200, INJURIES)
        raise AssertionError(f"unexpected URL {url}")

    def _ov(self, key):
        o = self.overrides[key]
        o = o() if callable(o) else o
        if isinstance(o, Exception):
            raise o
        return o


def mk_ctx(getter, **kw):
    logs = []
    sleeps = []
    ctx = ne.Ctx(getter, log=lambda m, lvl="INFO": logs.append((lvl, m)), sleep=sleeps.append, **kw)
    ctx.logs, ctx.sleeps = logs, sleeps
    return ctx


def raw_team(gp=10, **over):
    own = dict(gamesPlayed=gp, points=1100.0, fieldGoalsMade=400.0, fieldGoalsAttempted=900.0, threePointFieldGoalsMade=120.0,
               freeThrowsMade=180.0, freeThrowsAttempted=220.0, offensiveRebounds=100.0, defensiveRebounds=330.0, turnovers=130.0, rebounds=430.0)
    opp = dict(gamesPlayed=gp, points=1000.0, fieldGoalsMade=370.0, fieldGoalsAttempted=880.0, threePointFieldGoalsMade=100.0,
               freeThrowsMade=160.0, freeThrowsAttempted=200.0, offensiveRebounds=90.0, defensiveRebounds=300.0, turnovers=120.0, rebounds=390.0)
    own.update(over.get("own", {}))
    opp.update(over.get("opp", {}))
    return {"id": "1", "name": "Test Team", "own": own, "opp": opp}


# ══════════════════════════════════════════════════════════════════════════════════════════════════════════════
class PossessionMath(unittest.TestCase):
    def test_hand_computed_team(self):
        r = ne.row_from_raw(raw_team(), tov_adj=0.0, ot_factor=1.0)
        # tm  = 900 + 0.4*220 - 1.07*(100/(100+300))*(900-400) + 130 = 900 + 88 - 133.75 + 130 = 984.25
        # opp = 880 + 0.4*200 - 1.07*(90/(90+330))*(880-370)  + 120 = 880 + 80 - 116.9 + 120   = 963.1
        poss = (984.25 + 963.1) / 2                       # 973.675 over 10 games
        self.assertAlmostEqual(r["ortg"], round(100 * 1100 / poss, 1), places=6)
        self.assertAlmostEqual(r["drtg"], round(100 * 1000 / poss, 1), places=6)
        self.assertAlmostEqual(r["pace"], round(poss / 10, 1), places=6)
        self.assertAlmostEqual(r["net_rtg"], round(r["ortg"] - r["drtg"], 1), places=6)
        self.assertAlmostEqual(r["mov"], 10.0)
        self.assertEqual(r["gp"], 10)

    def test_four_factors_hand_computed(self):
        r = ne.row_from_raw(raw_team(), tov_adj=0.0, ot_factor=1.0)
        self.assertAlmostEqual(r["efg_pct"], round((400 + 60) / 900, 3))
        self.assertAlmostEqual(r["ts_pct"], round(1100 / (2 * (900 + 0.44 * 220)), 3))
        self.assertAlmostEqual(r["tov_pct"], round(100 * 130 / (900 + 0.44 * 220 + 130), 1))
        self.assertAlmostEqual(r["orb_pct"], round(100 * 100 / (100 + 300), 1))
        self.assertAlmostEqual(r["ft_rate"], round(180 / 900, 3))
        self.assertAlmostEqual(r["opp_efg_pct"], round((370 + 50) / 880, 3))
        self.assertAlmostEqual(r["opp_ft_rate"], round(160 / 880, 3))
        self.assertAlmostEqual(r["drb_pct"], round(100 - 100 * 90 / (90 + 330), 1))     # own DRB / (own DRB + opp ORB)

    def test_calibration_constants_move_numbers_the_documented_way(self):
        base = ne.row_from_raw(raw_team(), tov_adj=0.0, ot_factor=1.0)
        adj = ne.row_from_raw(raw_team())               # defaults: +0.75 team TO per game, x0.995 pace
        self.assertLess(adj["ortg"], base["ortg"])      # more possessions -> lower rating per 100
        self.assertLess(adj["pace"], base["pace"] + 0.75)
        self.assertEqual(ne.NBA_ESPN_TEAM_TOV_PER_GAME, 0.75)
        self.assertEqual(ne.NBA_ESPN_OT_PACE_FACTOR, 0.995)

    def test_zero_games_has_no_ratings(self):
        r = ne.row_from_raw(raw_team(gp=0))
        self.assertEqual((r["ortg"], r["drtg"], r["pace"], r["net_rtg"], r["gp"]), (None, None, None, None, 0))

    def test_missing_stat_column_keeps_row_without_ratings(self):
        raw = raw_team()
        del raw["own"]["fieldGoalsAttempted"]
        r = ne.row_from_raw(raw)
        self.assertIsNone(r["ortg"])
        self.assertEqual(r["gp"], 10)

    def test_opp_drb_fallback_from_total_rebounds(self):
        raw = raw_team()
        want = ne.row_from_raw(raw)
        for side in ("own", "opp"):
            del raw[side]["defensiveRebounds"]
        self.assertEqual(ne.row_from_raw(raw)["ortg"], want["ortg"])   # rebounds - offensiveRebounds == defensiveRebounds here


class ByteamFixture(unittest.TestCase):
    def setUp(self):
        self.rows, self.diag = ne.rows_from_byteam(BYTEAM_2026, 2026)

    def test_all_30_teams_with_espn_abbreviations(self):
        self.assertEqual(self.diag, "")
        self.assertEqual(set(self.rows), ESPN_30)

    def test_row_schema_matches_bbref_parser_rows(self):
        keys = {"name", "w", "l", "gp", "mov", "sos", "srs", "ortg", "drtg", "net_rtg", "pace", "ts_pct", "efg_pct", "tov_pct",
                "orb_pct", "ft_rate", "opp_efg_pct", "opp_tov_pct", "drb_pct", "opp_ft_rate"}
        for a, r in self.rows.items():
            self.assertEqual(set(r), keys, a)
            self.assertEqual(r["gp"], 82)
            self.assertIsNone(r["srs"])                 # ESPN publishes no SRS/SOS
        self.assertEqual(self.rows["OKC"]["name"], "Oklahoma City Thunder")

    def test_known_teams_close_to_basketball_reference(self):
        # Basketball-Reference 2025-26 (bbref_NBA_2026_advanced_team.html): OKC 118.9/107.7/99.3, DEN 122.6/117.4/98.4
        for a, (o, d, p) in {"OKC": (118.9, 107.7, 99.3), "DEN": (122.6, 117.4, 98.4)}.items():
            r = self.rows[a]
            self.assertAlmostEqual(r["ortg"], o, delta=0.5, msg=a)
            self.assertAlmostEqual(r["drtg"], d, delta=0.5, msg=a)
            self.assertAlmostEqual(r["pace"], p, delta=1.0, msg=a)

    def test_league_is_zero_sum_on_margin(self):
        self.assertAlmostEqual(sum(r["mov"] for r in self.rows.values()), 0.0, delta=0.5)

    def test_wrong_season_is_rejected(self):
        rows, diag = ne.rows_from_byteam(BYTEAM_2026, 2027)
        self.assertEqual(rows, {})
        self.assertIn("asked for 2027", diag)

    def test_garbage_is_loud_not_empty_success(self):
        for bad, frag in (({}, "no `teams`"), ({"teams": [{"team": {"abbreviation": "X"}, "categories": []}]}, "stat-name"),
                          ({"teams": [{"team": {"abbreviation": "X"}, "categories": []}],
                            "categories": [{"name": "general", "names": ["gamesPlayed"]}]}, "none had own+opponent")):
            rows, diag = ne.rows_from_byteam(bad)
            self.assertEqual(rows, {})
            self.assertIn(frag, diag)

    def test_attach_records(self):
        ne.attach_records(self.rows, {"OKC": {"w": "64", "l": "18"}, "DEN": {"w": "x", "l": "y"}})
        self.assertEqual((self.rows["OKC"]["w"], self.rows["OKC"]["l"]), (64, 18))
        self.assertEqual((self.rows["DEN"]["w"], self.rows["DEN"]["l"]), (0, 0))

    @unittest.skipUnless(HAVE_CU, "needs bs4/lxml to parse the Basketball-Reference fixture")
    def test_validation_against_basketball_reference_2026(self):
        bb, diag = cu.parse_nba_advanced_team(cu.BeautifulSoup(HTML_2026, "lxml"))
        self.assertEqual(diag, "")
        cmp = ne.compare_rows(self.rows, bb)
        self.assertEqual(cmp["ortg"]["n"], 30)
        self.assertLess(cmp["ortg"]["mae"], 0.25)
        self.assertLess(cmp["drtg"]["mae"], 0.25)
        self.assertLess(cmp["net_rtg"]["mae"], 0.15)
        self.assertLess(cmp["pace"]["mae"], 0.45)
        self.assertLess(abs(cmp["ortg"]["bias"]), 0.1)          # no systematic offset left (it was +2.2 with the textbook formula)
        for k in ("efg_pct", "ts_pct", "orb_pct", "ft_rate", "opp_efg_pct", "drb_pct", "opp_ft_rate"):
            self.assertLess(cmp[k]["mae"], 0.002, k)            # shooting/rebounding factors are exact (same raw counts)
        for k in ("tov_pct", "opp_tov_pct"):
            self.assertLess(cmp[k]["mae"], 0.25, k)
        # MOV is Basketball-Reference's MOV exactly
        self.assertLess(max(abs(self.rows[a]["mov"] - bb[a]["mov"]) for a in bb), 0.02)

    def test_textbook_possession_formula_is_worse(self):
        """Why the module does not use FGA + 0.44*FTA - OREB + TOV: it is ~2.5% high vs Basketball-Reference (pace 98.4 for DEN)."""
        raw, _ = ne.parse_byteam(BYTEAM_2026, 2026)
        r = raw["DEN"]
        simple = lambda t: t["fieldGoalsAttempted"] + 0.44 * t["freeThrowsAttempted"] - t["offensiveRebounds"] + t["turnovers"]
        pace_simple = 0.5 * (simple(r["own"]) + simple(r["opp"])) / 82
        self.assertGreater(pace_simple - 98.4, 2.0)
        self.assertLess(abs(ne.row_from_raw(r)["pace"] - 98.4), 1.0)


class Retries(unittest.TestCase):
    URL = "https://example.test/x"

    def test_two_failures_then_success(self):
        ctx = mk_ctx(Scripted(Resp(503, text="busy"), Exception("boom"), Resp(200, {"ok": 1})))
        data, st = ctx.get_json(self.URL, None, "t")
        self.assertEqual((data, st), ({"ok": 1}, "ok"))
        self.assertEqual(ctx.sleeps, [1.0, 2.0])                       # backoff between tries
        self.assertEqual(ctx.health["t"]["requests"], 3)
        self.assertEqual(ctx.health["t"]["retries"], 2)
        self.assertEqual(ctx.health["t"]["failures"], 0)
        self.assertEqual([l for l, _ in ctx.logs], ["WARN", "WARN"])   # each retry is logged with its reason
        self.assertIn("HTTP 503", ctx.logs[0][1])
        self.assertIn("boom", ctx.logs[1][1])

    def test_three_failures_is_loud_and_returns_none(self):
        ctx = mk_ctx(Scripted(Resp(500, text="x"), Resp(502, text="x"), Resp(504, text="x")))
        data, st = ctx.get_json(self.URL, None, "t")
        self.assertIsNone(data)
        self.assertTrue(st.startswith("failed: HTTP 504"), st)
        self.assertEqual(ctx.health["t"]["requests"], 3)
        self.assertEqual(ctx.health["t"]["failures"], 1)
        self.assertIn("FAILED after", ctx.logs[-1][1])
        self.assertEqual(ctx.logs[-1][0], "WARN")
        self.assertEqual(len(ctx.sleeps), 2)                           # no sleep after the last attempt

    def test_espn_2404_is_not_found_not_retried(self):
        g = Scripted(NOT_FOUND())
        ctx = mk_ctx(g)
        self.assertEqual(ctx.get_json(self.URL, None, "t"), (None, "not_found"))
        self.assertEqual(len(g.calls), 1)
        self.assertEqual(ctx.health["t"]["failures"], 0)
        self.assertEqual(ctx.sleeps, [])

    def test_plain_404_and_403_are_permanent(self):
        for sc, want in ((404, "not_found"), (403, "failed: HTTP 403")):
            g = Scripted(Resp(sc, text="no"))
            ctx = mk_ctx(g)
            _, st = ctx.get_json(self.URL, None, "t")
            self.assertEqual(st, want)
            self.assertEqual(len(g.calls), 1)

    def test_html_with_200_is_retried_as_invalid_json(self):
        ctx = mk_ctx(Scripted(Resp(200, text="<html>"), Resp(200, {"a": 1})))
        self.assertEqual(ctx.get_json(self.URL, None, "t"), ({"a": 1}, "ok"))
        self.assertIn("invalid JSON", ctx.logs[0][1])

    def test_timing_is_recorded_per_source(self):
        ticks = iter(range(0, 100, 2))
        ctx = ne.Ctx(Scripted(Resp(200, {})), clock=lambda: float(next(ticks)), log=lambda *a, **k: None, sleep=lambda s: None)
        ctx.get_json(self.URL, None, "src")
        self.assertEqual(ctx.health["src"]["seconds"], 2.0)
        self.assertEqual(ctx.summary()["src"], {"req": 1, "retries": 0, "sec": 2.0, "last": "ok"})
        self.assertEqual(ctx.totals()["requests"], 1)


class TeamRowFetch(unittest.TestCase):
    def test_ok(self):
        ctx = mk_ctx(FakeEspn())
        rows, info = ne.fetch_team_rows(ctx, 2026)
        self.assertEqual(len(rows), 30)
        self.assertEqual((info["status"], info["teams"], info["hardFail"], info["gpMedian"]), ("ok", 30, False, 82))

    def test_season_not_started_is_info_not_failure(self):
        ctx = mk_ctx(FakeEspn())
        rows, info = ne.fetch_team_rows(ctx, 2027)
        self.assertEqual(rows, {})
        self.assertEqual((info["status"], info["hardFail"]), ("not_found", False))
        self.assertEqual([l for l, _ in ctx.logs], ["INFO"])

    def test_outage_is_hard_failure_with_retries(self):
        g = FakeEspn({"byteam:2026": lambda: Resp(503, text="down")})
        ctx = mk_ctx(g)
        rows, info = ne.fetch_team_rows(ctx, 2026)
        self.assertEqual(rows, {})
        self.assertTrue(info["hardFail"])
        self.assertEqual(len(g.calls), 3)
        self.assertTrue(any(l == "WARN" and "FAILED" in m for l, m in ctx.logs))

    def test_wrong_season_payload_is_hard_failure(self):
        ctx = mk_ctx(FakeEspn({"byteam:2025": lambda: Resp(200, BYTEAM_2026)}))
        rows, info = ne.fetch_team_rows(ctx, 2025)
        self.assertEqual(rows, {})
        self.assertTrue(info["hardFail"])
        self.assertIn("asked for 2025", info["reason"])


# ══════════════════════════════════════════════════════════════════════════════════════════════════════════════
class GameLog(unittest.TestCase):
    def test_parse_sorted_newest_first_regular_season_only(self):
        games = ne.parse_gamelog(GAMELOG)
        dates = [g["date"] for g in games]
        self.assertEqual(dates, sorted(dates, reverse=True))
        self.assertEqual(len(games), 65)          # 66 listed under "Regular Season": 65 real games + the All-Star game (opponent "STARS"), which is dropped; postseason excluded
        self.assertEqual(games[0]["date"], "2026-04-13")
        self.assertTrue(all(g["date"] <= "2026-04-13" for g in games))
        self.assertEqual({"date", "min", "pts", "reb", "ast", "tpm", "opp", "home"}, set(games[0]))

    def test_last5_and_stdev(self):
        games = ne.parse_gamelog(GAMELOG)
        f = ne.form_from_games(games)
        l5 = games[:5]
        self.assertEqual(f["last5"]["n"], 5)
        self.assertAlmostEqual(f["last5"]["ppg"], round(sum(g["pts"] for g in l5) / 5, 1))
        self.assertAlmostEqual(f["last5"]["rpg"], round(sum(g["reb"] for g in l5) / 5, 1))
        self.assertAlmostEqual(f["last5"]["mpg"], round(sum(g["min"] for g in l5) / 5, 1))
        self.assertAlmostEqual(f["stdev"]["pts"], round(statistics.stdev(g["pts"] for g in games), 2))
        self.assertAlmostEqual(f["stdev"]["ast"], round(statistics.stdev(g["ast"] for g in games), 2))
        self.assertEqual(f["lastGame"], "2026-04-13")
        self.assertEqual(f["n"], 65)
        # sanity vs ESPN's own season averages in the same payload: 27.7 pts / 12.9 reb / 10.7 ast
        self.assertAlmostEqual(statistics.mean(g["pts"] for g in games), 27.7, delta=0.1)
        self.assertAlmostEqual(statistics.mean(g["reb"] for g in games), 12.9, delta=0.1)

    def test_short_log_has_no_stdev(self):
        f = ne.form_from_games(ne.parse_gamelog(GAMELOG)[:7])
        self.assertIsNone(f["stdev"])
        self.assertEqual(f["last5"]["n"], 5)

    def test_empty_and_unknown_layout(self):
        self.assertEqual(ne.parse_gamelog(None), [])
        self.assertEqual(ne.parse_gamelog({"names": ["minutes"], "seasonTypes": []}), [])
        self.assertEqual(ne.form_from_games([]), {"last5": None, "stdev": None, "lastGame": None, "n": 0})

    def test_did_not_play_rows_are_dropped(self):
        d = copy.deepcopy(GAMELOG)
        cat = next(s for s in d["seasonTypes"] if "Regular" in s["displayName"])["categories"][0]
        cat["events"][0]["stats"][0] = "0"
        self.assertEqual(len(ne.parse_gamelog(d)), 64)


class PlayerPool(unittest.TestCase):
    def test_parse_byathlete(self):
        pool = ne.parse_byathlete(BYATHLETE)
        self.assertEqual(len(pool), 14)
        p = pool[0]
        self.assertEqual(set(p), {"id", "name", "team", "pos", "gp", "min_total", "mpg", "ppg", "rpg", "apg", "tpm", "spg", "bpg"})
        self.assertTrue(p["id"].isdigit())
        self.assertGreater(p["mpg"], 20)
        self.assertGreater(p["ppg"], 0)

    def test_build_roster_team_overrides_status_and_filters(self):
        pool = [dict(id="1", name="Star One", team="OLD", pos="G", gp=60, min_total=2000.0, mpg=33.0, ppg=25.0, rpg=5.0, apg=6.0, tpm=2.5, spg=1.0, bpg=0.2),
                dict(id="2", name="Role Two", team="BOS", pos="F", gp=70, min_total=1500.0, mpg=21.0, ppg=9.0, rpg=4.0, apg=1.0, tpm=1.0, spg=.5, bpg=.5),
                dict(id="3", name="Few Games", team="BOS", pos="F", gp=3, min_total=9000.0, mpg=40.0, ppg=30.0, rpg=9.0, apg=9.0, tpm=3.0, spg=1, bpg=1),
                dict(id="4", name="Not On Roster", team="NY", pos="C", gp=60, min_total=1800.0, mpg=30.0, ppg=15.0, rpg=9.0, apg=2.0, tpm=0.1, spg=1, bpg=1)]
        roster = {"star one": {"team": "NEW", "pos": "SG", "id": "1"}, "role two": {"team": "BOS", "pos": "PF", "id": "2"}}
        inj = [{"id": "1", "name": "Star One", "status": "Out"}, {"id": "", "name": "Role Two", "status": "Day-To-Day"}]
        forms = {"1": {"last5": {"n": 5, "ppg": 30.0, "rpg": 5, "apg": 6, "tpm": 3, "mpg": 35}, "stdev": {"pts": 8.0}, "lastGame": "2026-04-13"}}
        b = ne.build_player_props(pool, roster, inj, forms, season=2026, generated="2026-10-03")
        self.assertEqual([p["name"] for p in b["players"]], ["Star One", "Role Two"])           # minutes order; gp<5 and off-roster dropped
        self.assertEqual((b["players"][0]["team"], b["players"][0]["pos"]), ("NEW", "SG"))      # current roster beats the stats row
        self.assertEqual([p["status"] for p in b["players"]], ["Out", "Day-To-Day"])           # by id, then by name
        self.assertEqual(b["players"][0]["last5"]["ppg"], 30.0)
        self.assertIsNone(b["players"][1]["last5"])
        self.assertEqual((b["n"], b["droppedNoRoster"], b["withForm"]), (2, 1, 1))
        self.assertEqual(b["source"], "espn")
        self.assertIn("no player prop lines", b["propLines"])

    def test_empty_roster_keeps_stats_team(self):
        pool = [dict(id="1", name="A", team="BOS", pos="G", gp=60, min_total=2000.0, mpg=33.0, ppg=25.0, rpg=5.0, apg=6.0, tpm=2.5, spg=1.0, bpg=0.2)]
        b = ne.build_player_props(pool, {}, [], {}, season=2026, generated="x")
        self.assertEqual(b["players"][0]["team"], "BOS")
        self.assertEqual(b["players"][0]["status"], "active")

    def test_max_players_cap(self):
        pool = [dict(id=str(i), name=f"P{i}", team="BOS", pos="G", gp=60, min_total=float(3000 - i), mpg=30.0, ppg=10.0, rpg=3.0, apg=2.0, tpm=1.0, spg=1, bpg=0)
                for i in range(200)]
        b = ne.build_player_props(pool, {}, [], {}, season=2026, generated="x")
        self.assertEqual(b["n"], ne.PROPS_MAX_PLAYERS)
        self.assertEqual(b["players"][0]["name"], "P0")


class PropsFetch(unittest.TestCase):
    def fetch(self, espn=None, prev=None, roster=None, ctx=None):
        espn = espn or FakeEspn()
        ctx = ctx or mk_ctx(espn)
        block, info = ne.fetch_player_props(ctx, 2026, roster, [], prev, "2026-10-03")
        return block, info, espn, ctx

    def test_one_gamelog_per_player_then_all_cached(self):
        block, info, espn, _ = self.fetch()
        self.assertEqual(block["n"], 14)
        self.assertEqual(info["gamelogFetched"], 14)
        self.assertEqual(len(espn.gamelog_ids), 14)
        self.assertTrue(all(p["last5"] and p["stdev"] for p in block["players"]))
        espn2 = FakeEspn()
        block2, info2, _, _ = self.fetch(espn=espn2, prev=block)
        self.assertEqual(espn2.gamelog_ids, [])                      # same gp -> no game-log requests at all
        self.assertEqual((info2["gamelogCached"], info2["gamelogFetched"]), (14, 0))
        self.assertEqual(block2["players"], block["players"])

    def test_only_changed_gp_refetched(self):
        block, *_ = self.fetch()
        prev = copy.deepcopy(block)
        prev["players"][0]["gp"] -= 1
        espn = FakeEspn()
        _, info, _, _ = self.fetch(espn=espn, prev=prev)
        self.assertEqual(espn.gamelog_ids, [prev["players"][0]["id"]])
        self.assertEqual(info["gamelogCached"], 13)

    def test_prev_block_from_another_season_is_ignored(self):
        block, *_ = self.fetch()
        prev = dict(copy.deepcopy(block), season=2025)
        espn = FakeEspn()
        self.fetch(espn=espn, prev=prev)
        self.assertEqual(len(espn.gamelog_ids), 14)

    def test_gamelog_failures_do_not_blank_averages_and_keep_cached_form(self):
        good, *_ = self.fetch()
        prev = copy.deepcopy(good)
        for p in prev["players"][:3]:
            p["gp"] -= 1                                            # these three need a refetch...
        espn = FakeEspn({"gamelog": lambda: Resp(503, text="x")})   # ...which fails
        block, info, _, ctx = self.fetch(espn=espn, prev=prev)
        self.assertEqual(block["n"], 14)
        self.assertEqual(info["gamelogFailed"], 3)
        self.assertEqual(info["staleFormFromPrev"], 3)
        self.assertTrue(all(p["ppg"] > 0 for p in block["players"]))
        self.assertTrue(all(p["last5"] for p in block["players"]))   # stale cached form kept for the failed three
        self.assertTrue(any(l == "WARN" and "game-log failure" in m for l, m in ctx.logs))

    def test_gamelog_failure_without_cache_gives_averages_only(self):
        block, info, _, _ = self.fetch(FakeEspn({"gamelog": lambda: Resp(503, text="x")}))
        self.assertEqual(info["gamelogFailed"], 14)
        self.assertEqual(block["n"], 14)
        self.assertTrue(all(p["last5"] is None and p["stdev"] is None for p in block["players"]))
        self.assertEqual(block["withForm"], 0)

    def test_time_budget_stops_gamelog_fetching(self):
        t = {"now": 0.0}
        espn = FakeEspn()
        ctx = ne.Ctx(espn, log=lambda *a, **k: None, sleep=lambda s: t.__setitem__("now", t["now"] + 100.0), clock=lambda: t["now"])
        block, info, _, _ = self.fetch(espn=espn, ctx=ctx)
        self.assertGreater(info["gamelogSkippedBudget"], 0)
        self.assertLess(len(espn.gamelog_ids), 14)
        self.assertEqual(block["n"], 14)                              # still all players, just without form

    def test_no_pool_returns_empty_block_for_carry_forward(self):
        espn = FakeEspn({"byathlete": lambda: Resp(200, {"athletes": []})})
        block, info, _, ctx = self.fetch(espn=espn)
        self.assertEqual(block, {})
        self.assertEqual(info["poolStatus"], "empty")
        self.assertTrue(any(l == "WARN" for l, _ in ctx.logs))

    def test_season_with_no_data_yet_is_empty(self):
        d = {"currentSeason": {"year": 2027}}                         # what ESPN returns for byathlete season=2027 in the preseason
        self.assertEqual(ne.parse_byathlete(d), [])


# ══════════════════════════════════════════════════════════════════════════════════════════════════════════════
@unittest.skipUnless(HAVE_CU, "needs bs4/lxml (run with /usr/bin/python3)")
class Wiring(unittest.TestCase):
    """clairvoyance_update.get_nba_team_stats_selection / collect_nba_season_data over a fake ESPN (and fake Basketball-Reference)."""

    def setUp(self):
        cu._NBA_STATS_CACHE.clear()
        self.logs = []
        self.espn = FakeEspn()
        self.bb_calls = []

        class BB:
            def __init__(s, outer, html=None, status=200):
                s.outer, s.html, s.status = outer, html, status

            def get(s, url, timeout=None):
                s.outer.bb_calls.append(url)
                return type("R", (), {"status_code": s.status, "text": s.html or "x", "headers": {"server": "cloudflare"}})()
        self.BB = BB
        self._p = [mock.patch.object(cu, "NBA_BBREF_DELAY", 0),
                   mock.patch.object(cu, "NBA_TEAM_STATS_SOURCE", "espn"),
                   mock.patch.object(cu, "log", lambda m, lvl="INFO": self.logs.append((lvl, m))),
                   mock.patch.object(cu.time, "sleep", lambda s: None),
                   mock.patch.object(cu, "_nba_http_get", lambda url, params=None, timeout=20: self.espn(url, params, timeout)),
                   mock.patch.object(cu, "_ref_session", BB(self)),
                   mock.patch.dict(os.environ, {"NBA_SEASON_END_YEAR": "2027"})]
        for p in self._p:
            p.start()
        cu._nba_ctx(reset=True)

    def tearDown(self):
        for p in self._p:
            p.stop()
        cu._NBA_STATS_CACHE.clear()

    def bbref(self, html=None, status=200):
        return mock.patch.object(cu, "_ref_session", self.BB(self, html, status))

    def warns(self):
        return [m for l, m in self.logs if l == "WARN"]

    # ── selection / sources ───────────────────────────────────────────────────────────────────────────────
    def test_preseason_espn_primary_never_touches_bbref(self):
        sel = cu.get_nba_team_stats_selection()
        self.assertEqual(set(sel["teams"]), ESPN_30)
        self.assertEqual((sel["mode"], sel["seasonUsed"]), ("prior", 2026))
        self.assertEqual(sel["bbref"], "skipped")
        self.assertEqual(self.bb_calls, [])
        self.assertEqual(sel["origin"], {"prior": {"season": 2026, "espn": 30, "bbref": 0}, "current": {"season": 2027, "espn": 0, "bbref": 0}})
        self.assertEqual(self.warns(), [])
        byteam = [c for c in self.espn.calls if "byteam" in c[0]]
        self.assertEqual([c[1]["season"] for c in byteam], [2027, 2026])
        self.assertTrue(all(c[1]["seasontype"] == 2 for c in byteam))

    def test_adv_and_four_factors_schema_unchanged(self):
        adv = cu.fetch_nba_team_advanced()
        ff = cu.fetch_nba_four_factors()
        self.assertEqual(set(adv), ESPN_30)
        self.assertEqual(set(ff), ESPN_30)
        self.assertEqual(set(adv["OKC"]), {"ortg", "drtg", "pace", "efg_pct", "ts_pct", "net_rtg", "season", "gp"})
        self.assertEqual(set(ff["OKC"]), {"efg_pct", "tov_pct", "orb_pct", "ft_rate", "opp_efg_pct", "opp_tov_pct", "drb_pct", "opp_ft_rate", "season", "gp"})
        self.assertEqual({v["season"] for v in adv.values()}, {2026})
        self.assertEqual(len([c for c in self.espn.calls if "byteam" in c[0]]), 2)     # cached across both functions

    def test_season_underway_switches_to_current(self):
        g = cu.NBA_MIN_GAMES_FOR_CURRENT + 1                      # 15 since 2026-10-05 (was 5): a few games of raw ratings must not replace the prior
        cur = scale_byteam(BYTEAM_2026, 2027, g, g / 82)
        self.espn.overrides["byteam:2027"] = lambda: Resp(200, cur)
        sel = cu.get_nba_team_stats_selection()
        self.assertEqual((sel["mode"], sel["seasonUsed"]), ("current", 2027))
        self.assertEqual({r["season"] for r in sel["teams"].values()}, {2027})
        self.assertEqual({r["gp"] for r in sel["teams"].values()}, {g})

    def test_few_games_keep_the_prior(self):
        g = cu.NBA_MIN_GAMES_FOR_CURRENT - 1
        cur = scale_byteam(BYTEAM_2026, 2027, g, g / 82)
        self.espn.overrides["byteam:2027"] = lambda: Resp(200, cur)
        sel = cu.get_nba_team_stats_selection()
        self.assertEqual((sel["mode"], sel["seasonUsed"]), ("prior", 2026))
        self.assertEqual(self.bb_calls, [])
        # scaling every total by the same factor leaves the per-possession ratings unchanged
        self.assertAlmostEqual(sel["teams"]["OKC"]["drtg"], sel["priorRows"]["OKC"]["drtg"], delta=0.11)

    def test_four_games_stays_on_prior(self):
        self.espn.overrides["byteam:2027"] = lambda: Resp(200, scale_byteam(BYTEAM_2026, 2027, 4, 4 / 82))
        sel = cu.get_nba_team_stats_selection()
        self.assertEqual(sel["mode"], "prior")

    def test_few_teams_missing_filled_from_bbref(self):
        short = copy.deepcopy(BYTEAM_2026)
        short["teams"] = [t for t in short["teams"] if t["team"]["abbreviation"] not in ("UTAH", "WSH", "NY")]
        self.espn.overrides["byteam:2026"] = lambda: Resp(200, short)
        with self.bbref(HTML_2026):
            sel = cu.get_nba_team_stats_selection()
        self.assertEqual(set(sel["teams"]), ESPN_30)
        self.assertEqual(sel["bbref"], "ok")
        self.assertEqual(sel["origin"]["prior"], {"season": 2026, "espn": 27, "bbref": 3})
        self.assertIsNotNone(sel["priorRows"]["NY"]["srs"])                 # the filled rows are Basketball-Reference rows
        self.assertIsNone(sel["priorRows"]["OKC"]["srs"])                   # the rest stayed ESPN
        self.assertTrue(any("filled from Basketball-Reference" in m for m in self.warns()))
        self.assertEqual(len(self.bb_calls), 1)                             # only the 2026 page; 2027 is not asked (ESPN said "not yet")

    def test_espn_down_bbref_ok_falls_back_completely(self):
        self.espn.overrides["byteam:2026"] = lambda: Resp(503, text="x")
        self.espn.overrides["byteam:2027"] = lambda: Resp(503, text="x")
        with self.bbref(HTML_2026):
            sel = cu.get_nba_team_stats_selection()
        self.assertEqual(set(sel["teams"]), ESPN_30)
        self.assertEqual(sel["origin"]["prior"], {"season": 2026, "espn": 0, "bbref": 30})
        self.assertEqual(sel["bbref"], "ok")
        self.assertTrue(any("FAILED" in m for m in self.warns()))

    def test_espn_down_bbref_blocked_is_loud(self):
        self.espn.overrides["byteam:2026"] = lambda: Resp(503, text="x")
        with self.bbref("blocked", 403):
            sel = cu.get_nba_team_stats_selection()
        self.assertEqual(sel["teams"], {})
        self.assertEqual(sel["bbref"], "blocked")
        self.assertIn("HTTP 403", sel["bbrefReason"])
        w = " ".join(self.warns())
        self.assertIn("fallback unavailable", w)
        self.assertIn("no team stats at all", w)

    def test_espn_only_never_asks_bbref(self):
        self.espn.overrides["byteam:2026"] = lambda: Resp(503, text="x")
        with mock.patch.object(cu, "NBA_TEAM_STATS_SOURCE", "espn-only"), self.bbref(HTML_2026):
            sel = cu.get_nba_team_stats_selection()
        self.assertEqual((sel["teams"], sel["bbref"], self.bb_calls), ({}, "disabled", []))

    def test_no_reference_flag_blocks_bbref_not_espn(self):
        self.espn.overrides["byteam:2026"] = lambda: Resp(503, text="x")
        with self.bbref(HTML_2026):
            sel = cu.get_nba_team_stats_selection(allow_bbref=False)
        self.assertEqual((sel["bbref"], self.bb_calls), ("disabled", []))

    def test_bbref_only_mode_is_the_old_behaviour(self):
        with mock.patch.object(cu, "NBA_TEAM_STATS_SOURCE", "bbref"), self.bbref(HTML_2026):
            sel = cu.get_nba_team_stats_selection()
        self.assertEqual(set(sel["teams"]), ESPN_30)
        self.assertEqual([c for c in self.espn.calls if "byteam" in c[0]], [])

    # ── collect_nba_season_data ───────────────────────────────────────────────────────────────────────────
    def collect_patches(self, roster=None, standings=None, players=None, prior_standings=None):
        pool = ne.parse_byathlete(BYATHLETE)
        if roster is None:
            roster = {p["name"].lower(): {"team": p["team"], "pos": p["pos"], "id": p["id"]} for p in pool}
        st = standings if standings is not None else {a: {"w": "0", "l": "0", "pct": ".000", "gb": "-", "rs": "0", "ra": "0", "diff": "0"} for a in ESPN_30}
        prior_st = prior_standings if prior_standings is not None else {a: {"w": "41", "l": "41", "diff": "0"} for a in ESPN_30}
        return [mock.patch.object(cu, "fetch_nba_roster", lambda: roster),
                mock.patch.object(cu, "fetch_nba_standings", lambda season=None: st if season == 2027 else prior_st),
                mock.patch.object(cu, "fetch_nba_player_stats",
                                  lambda season=None, pages=2: players if players is not None else [{"name": p["name"], "team": p["team"], "gp": p["gp"], "mpg": p["mpg"], "ppg": p["ppg"], "season": 2026} for p in pool])]

    def run_collect(self, prev=None, **kw):
        ps = self.collect_patches(**kw)
        for p in ps:
            p.start()
        try:
            cu._NBA_STATS_CACHE.clear()
            return cu.collect_nba_season_data(prev_nba=prev if prev is not None else {})
        finally:
            for p in ps:
                p.stop()

    def test_collect_full_schema(self):
        out = self.run_collect()
        self.assertTrue({"season", "standings", "players", "roster", "teamAdv", "fourFactors", "teamRatings", "eloSeed",
                         "playerProps", "sources", "health"} <= set(out))
        self.assertEqual(out["season"], 2027)
        tr = out["teamRatings"]
        self.assertEqual((tr["seasonCurrent"], tr["seasonPrior"], tr["statsSeasonUsed"], tr["statsMode"]), (2027, 2026, 2026, "prior"))
        self.assertEqual(set(tr["teams"]), ESPN_30)
        t = tr["teams"]["OKC"]
        self.assertEqual(set(t), {"name", "prior", "current", "priorWinPct", "strength", "elo", "source"})
        self.assertEqual(set(t["prior"]), {"season", "w", "l", "winPct", "mov", "srs", "netRtg", "ortg", "drtg", "pace"})
        self.assertEqual(set(t["current"]), {"season", "w", "l", "gp", "mov", "netRtg"})
        self.assertEqual((t["prior"]["season"], t["prior"]["w"], t["prior"]["l"], t["source"]), (2026, 41, 41, "prior"))
        self.assertIsNone(t["prior"]["srs"])
        self.assertAlmostEqual(t["prior"]["ortg"], 118.9, delta=0.5)
        self.assertGreater(t["elo"], 1700)                       # OKC +11 MOV -> well above the 1550 mean
        self.assertLess(tr["teams"]["UTAH"]["elo"], 1450)
        self.assertEqual(set(out["eloSeed"]), ESPN_30)
        # players / roster keep their old shapes
        self.assertEqual(set(out["players"][0]), {"name", "team", "gp", "mpg", "ppg", "season"})
        self.assertEqual(set(next(iter(out["roster"].values()))) - {"rating", "ppg"}, {"team", "pos", "id"})
        # new blocks
        s = out["sources"]
        self.assertEqual((s["teamRatings"], s["bbref"], s["roster"], s["players"], s["playerProps"], s["standings"], s["injuries"]),
                         ("espn", "skipped", "espn", "espn", "espn", "espn", "espn"))
        self.assertRegex(s["generatedAt"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        h = out["health"]
        self.assertEqual((h["teams_with_ratings"], h["games_played_median"], h["stale_sources"], h["failures"]), (30, 0, [], 0))
        self.assertTrue({"byteam", "byathlete", "gamelog", "injuries"} <= {k.split(".")[1] for k in h["endpoints"]})
        self.assertEqual(self.bb_calls, [])
        self.assertEqual(self.warns(), [])

    def test_player_props_block(self):
        out = self.run_collect()
        pp = out["playerProps"]
        self.assertEqual((pp["season"], pp["source"], pp["n"], pp["withForm"]), (2026, "espn", 14, 14))
        p = pp["players"][0]
        self.assertEqual(set(p), {"id", "name", "team", "pos", "gp", "mpg", "ppg", "rpg", "apg", "tpm", "spg", "bpg", "last5", "stdev", "lastGame", "status"})
        self.assertEqual(set(p["last5"]), {"n", "ppg", "rpg", "apg", "tpm", "mpg"})
        self.assertEqual(set(p["stdev"]), {"pts", "reb", "ast", "tpm"})
        self.assertLess(len(json.dumps(pp)), 150 * 1024)
        self.assertEqual(len(self.espn.gamelog_ids), 14)
        self.assertEqual(len([c for c in self.espn.calls if "byathlete" in c[0]]), 1)    # the fake returns a short first page -> stops

    def test_second_run_with_previous_output_makes_no_gamelog_requests(self):
        first = self.run_collect()
        self.espn.gamelog_ids.clear()
        second = self.run_collect(prev=dict(first))
        self.assertEqual(self.espn.gamelog_ids, [])
        self.assertEqual(second["playerProps"]["players"], first["playerProps"]["players"])
        self.assertEqual(second["health"]["stale_sources"], [])

    def test_one_failing_endpoint_cannot_blank_other_fields(self):
        # injuries + gamelog + byathlete all down: team ratings, roster, players, standings still come through
        self.espn.overrides.update({"injuries": lambda: Resp(500, text="x"), "gamelog": lambda: Resp(500, text="x"),
                                    "byathlete": lambda: Resp(500, text="x")})
        out = self.run_collect()
        self.assertEqual(len(out["teamRatings"]["teams"]), 30)
        self.assertEqual(out["sources"]["teamRatings"], "espn")
        self.assertEqual(len(out["teamAdv"]), 30)
        self.assertEqual(len(out["roster"]), 14)
        self.assertEqual(len(out["players"]), 14)
        self.assertEqual(out["playerProps"], {})
        self.assertEqual(out["sources"]["playerProps"], "none")
        self.assertIn("playerProps", out["health"]["stale_sources"])
        self.assertEqual(out["health"]["teams_with_ratings"], 30)

    def test_everything_down_carries_previous_values_loudly(self):
        good = self.run_collect()
        prev = {k: good[k] for k in ("season", "standings", "players", "roster", "teamAdv", "fourFactors", "teamRatings", "eloSeed", "playerProps")}
        self.logs.clear()
        self.espn.overrides.update({"byteam:2026": lambda: Resp(503, text="x"), "byteam:2027": lambda: Resp(503, text="x"),
                                    "injuries": lambda: Resp(503, text="x"), "gamelog": lambda: Resp(503, text="x"),
                                    "byathlete": lambda: Resp(503, text="x")})
        with self.bbref("blocked", 403):
            out = self.run_collect(prev=prev, roster={}, standings={}, players=[], prior_standings={})   # standings/roster/players empty too
        self.assertEqual(out["teamRatings"], good["teamRatings"])
        self.assertEqual(out["eloSeed"], good["eloSeed"])
        self.assertEqual(out["teamAdv"], good["teamAdv"])
        self.assertEqual(out["fourFactors"], good["fourFactors"])
        self.assertEqual(out["playerProps"], good["playerProps"])
        self.assertEqual(out["players"], good["players"])
        self.assertEqual(out["standings"], good["standings"])
        self.assertEqual(len(out["roster"]), 14)
        s, h = out["sources"], out["health"]
        self.assertEqual((s["teamRatings"], s["playerProps"], s["players"], s["standings"], s["roster"]),
                         ("carried", "carried", "carried", "carried", "carried"))
        self.assertEqual(s["bbref"], "blocked")
        for k in ("teamRatings", "teamAdv", "fourFactors", "playerProps", "players", "standings", "roster"):
            self.assertIn(k, h["stale_sources"], k)
        self.assertGreater(len(self.warns()), 5)
        self.assertEqual(h["teams_with_ratings"], 0)

    def test_standings_not_carried_across_a_season_change(self):
        prev = {"season": 2026, "standings": {"OKC": {"w": "64", "l": "18"}}}
        ps = [mock.patch.object(cu, "fetch_nba_standings", lambda season=None: {}), mock.patch.object(cu, "fetch_nba_roster", lambda: {}),
              mock.patch.object(cu, "fetch_nba_player_stats", lambda season=None, pages=2: [])]
        for p in ps:
            p.start()
        try:
            cu._NBA_STATS_CACHE.clear()
            out = cu.collect_nba_season_data(prev_nba=prev)
        finally:
            for p in ps:
                p.stop()
        self.assertEqual(out["standings"], {})
        self.assertEqual(out["sources"]["standings"], "none")

    def test_roster_teams_missing_are_filled_from_previous(self):
        prev_roster = {"old guy": {"team": "BOS", "pos": "G", "id": "9"}, "other": {"team": "NY", "pos": "F", "id": "8"}}
        fresh = {"new guy": {"team": "BOS", "pos": "C", "id": "7"}}
        merged, carried = cu._nba_roster_with_carry(fresh, prev_roster)
        self.assertTrue(carried)
        self.assertIn("other", merged)                  # NY had no fresh player
        self.assertNotIn("old guy", merged)             # BOS did: fresh wins, old BOS entries are not resurrected
        self.assertIn("new guy", merged)
        self.assertEqual(cu._nba_roster_with_carry(fresh, None), (fresh, False))
        full = {f"p{i}": {"team": a} for i, a in enumerate(sorted(ESPN_30))}
        self.assertEqual(cu._nba_roster_with_carry(full, prev_roster), (full, False))

    def test_retry_inside_collect_recovers(self):
        n = {"i": 0}

        def flaky():
            n["i"] += 1
            return Resp(503, text="x") if n["i"] < 3 else Resp(200, BYTEAM_2026)
        self.espn.overrides["byteam:2026"] = flaky
        out = self.run_collect()
        self.assertEqual(out["health"]["teams_with_ratings"], 30)
        self.assertEqual(out["health"]["retries"], 2)
        self.assertEqual(out["health"]["failures"], 0)
        self.assertEqual(out["sources"]["teamRatings"], "espn")

    def test_mixed_source_provenance(self):
        short = copy.deepcopy(BYTEAM_2026)
        short["teams"] = [t for t in short["teams"] if t["team"]["abbreviation"] != "NY"]
        self.espn.overrides["byteam:2026"] = lambda: Resp(200, short)
        with self.bbref(HTML_2026):
            out = self.run_collect()
        self.assertEqual(out["sources"]["teamRatings"], "espn+bbref")
        self.assertEqual(out["sources"]["bbref"], "ok")
        self.assertEqual(out["health"]["teams_with_ratings"], 30)


if __name__ == "__main__":
    unittest.main(verbosity=1)
