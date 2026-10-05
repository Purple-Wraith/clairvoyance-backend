#!/usr/bin/env python3
"""Tests for the NBA season-rollover code in scripts/clairvoyance_update.py (no network).

    python3 scripts/test_nba_rollover.py

Fixtures in scripts/fixtures/nba_rollover/ are the REAL `advanced-team` table markup from
Basketball-Reference, fetched 2026-10-03 (2025-26 final table with 30 rows; 2026-27 preseason
table where every stat cell is still empty and W-L is 0-0). The HTTP layer is faked
(`_ref_session.get` / `fetch_json` are patched) -- nothing here touches the network.

The incident these cover: fetch_nba_team_advanced()/fetch_nba_four_factors() parsed 0 teams on every
run, silently, because BBRef renamed its tables (data-stat "team" holds a full name now; the four
factors live inside `advanced-team`) and the BBRef abbreviations (BRK/CHO/PHO/WAS/NOP/UTA...) were
never mapped to ESPN's (BKN/CHA/PHX/WSH/NO/UTAH).
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
FIX = HERE / "fixtures" / "nba_rollover"

if importlib.util.find_spec("bs4") is None or importlib.util.find_spec("lxml") is None:
    # clairvoyance_update.py would pip-install these on import; don't do that from a test run.
    print("SKIP: bs4/lxml not installed in this interpreter")
    sys.exit(0)

sys.path.insert(0, str(HERE))
_spec = importlib.util.spec_from_file_location("cu_nba_test", HERE / "clairvoyance_update.py")
cu = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cu)

HTML_2026 = (FIX / "bbref_NBA_2026_advanced_team.html").read_text(encoding="utf-8")
HTML_2027_PRE = (FIX / "bbref_NBA_2027_advanced_team_preseason.html").read_text(encoding="utf-8")

ESPN_30 = {"ATL", "BKN", "BOS", "CHA", "CHI", "CLE", "DAL", "DEN", "DET", "GS", "HOU", "IND", "LAC", "LAL",
           "MEM", "MIA", "MIL", "MIN", "NO", "NY", "OKC", "ORL", "PHI", "PHX", "POR", "SA", "SAC", "TOR",
           "UTAH", "WSH"}


class FakeResp:
    def __init__(self, text="", status=200, server="cloudflare"):
        self.text, self.status_code, self.headers = text, status, {"server": server}


class FakeSession:
    """Stands in for requests.Session: routes by URL substring, records the calls."""
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def get(self, url, timeout=None):
        self.calls.append(url)
        for sub, resp in self.routes.items():
            if sub in url:
                if isinstance(resp, Exception):
                    raise resp
                return resp
        raise AssertionError(f"unexpected URL {url}")


def soup(html):
    return cu.BeautifulSoup(html, "lxml")


def games_row(abbr_href, name, w, l, **stats):
    """One synthetic <tr> in the live advanced-team layout (data-stat names as on BBRef)."""
    cols = dict(off_rtg="110.0", def_rtg="108.0", net_rtg="+2.0", pace="99.0", ts_pct=".580", efg_pct=".550",
                tov_pct="12.0", orb_pct="25.0", ft_rate=".220", opp_efg_pct=".530", opp_tov_pct="13.0",
                drb_pct="75.0", opp_ft_rate=".210", mov="2.00", sos="0.00", srs="2.00")
    cols.update(stats)
    cells = "".join(f'<td data-stat="{k}">{v}</td>' for k, v in cols.items())
    return (f'<tr><th data-stat="ranker">1</th><td data-stat="team"><a href="/teams/{abbr_href}/2027.html">{name}</a></td>'
            f'<td data-stat="wins">{w}</td><td data-stat="losses">{l}</td>{cells}</tr>')


def table(rows_html, wrap_in_comment=False):
    t = f'<table id="advanced-team"><thead><tr><th data-stat="ranker">Rk</th></tr></thead><tbody>{rows_html}</tbody></table>'
    body = f"<!-- {t} -->" if wrap_in_comment else t
    return f"<html><head><title>x</title></head><body>{body}</body></html>"


class SeasonYear(unittest.TestCase):
    def test_boundaries(self):
        f = cu.nba_season_end_year
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("NBA_SEASON_END_YEAR", None)
            self.assertEqual(f(date(2026, 6, 30)), 2026)    # Finals just ended -> still the 2025-26 season
            self.assertEqual(f(date(2026, 9, 30)), 2026)    # offseason
            self.assertEqual(f(date(2026, 10, 1)), 2027)    # flips Oct 1 (preseason)
            self.assertEqual(f(date(2026, 10, 3)), 2027)    # "today" in the rollover task
            self.assertEqual(f(date(2026, 10, 21)), 2027)   # opening night
            self.assertEqual(f(date(2026, 12, 31)), 2027)
            self.assertEqual(f(date(2027, 1, 1)), 2027)
            self.assertEqual(f(date(2027, 4, 15)), 2027)
            self.assertEqual(f(date(2027, 10, 21)), 2028)

    def test_accepts_datetime(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("NBA_SEASON_END_YEAR", None)
            self.assertEqual(cu.nba_season_end_year(datetime(2026, 11, 2, 18, 30)), 2027)

    def test_override_argument_and_env(self):
        self.assertEqual(cu.nba_season_end_year(date(2026, 10, 3), override=2026), 2026)
        with mock.patch.dict(os.environ, {"NBA_SEASON_END_YEAR": "2026"}):
            self.assertEqual(cu.nba_season_end_year(date(2026, 10, 3)), 2026)
        with mock.patch.dict(os.environ, {"NBA_SEASON_END_YEAR": " 2028 "}):
            self.assertEqual(cu.nba_season_end_year(date(2026, 10, 3)), 2028)

    def test_bad_override_is_ignored_with_warning(self):
        logs = []
        with mock.patch.object(cu, "log", lambda m, lvl="INFO": logs.append((lvl, m))):
            with mock.patch.dict(os.environ, {"NBA_SEASON_END_YEAR": "twenty27"}):
                self.assertEqual(cu.nba_season_end_year(date(2026, 10, 3)), 2027)
        self.assertTrue(any(l == "WARN" and "NBA_SEASON_END_YEAR" in m for l, m in logs))

    def test_playoffs_year(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("NBA_SEASON_END_YEAR", None)
            self.assertEqual(cu.nba_playoffs_year(date(2026, 10, 3)), 2026)   # last completed playoffs
            self.assertEqual(cu.nba_playoffs_year(date(2027, 3, 31)), 2026)
            self.assertEqual(cu.nba_playoffs_year(date(2027, 4, 20)), 2027)
            self.assertEqual(cu.nba_playoffs_year(date(2027, 7, 1)), 2027)

    def test_url(self):
        self.assertEqual(cu.nba_bbref_url(2027), "https://www.basketball-reference.com/leagues/NBA_2027.html")


class ParseAdvancedTeam(unittest.TestCase):
    def test_real_2026_table_gives_all_30_espn_abbrs(self):
        rows, diag = cu.parse_nba_advanced_team(soup(HTML_2026))
        self.assertEqual(diag, "")
        self.assertEqual(set(rows), ESPN_30)             # BRK->BKN, CHO->CHA, PHO->PHX, WAS->WSH, NOP->NO, UTA->UTAH ...
        okc = rows["OKC"]
        self.assertEqual(okc["name"], "Oklahoma City Thunder")   # trailing "*" (playoff team) stripped
        self.assertEqual((okc["w"], okc["l"], okc["gp"]), (64, 18, 82))
        self.assertAlmostEqual(okc["ortg"], 118.9)
        self.assertAlmostEqual(okc["drtg"], 107.7)
        self.assertAlmostEqual(okc["net_rtg"], 11.2)     # "+11.2" parsed
        self.assertAlmostEqual(okc["efg_pct"], 0.561)
        self.assertAlmostEqual(okc["opp_ft_rate"], 0.184)
        self.assertAlmostEqual(okc["srs"], 11.04)
        for ab in ("BKN", "CHA", "PHX", "WSH", "NO", "UTAH", "NY", "GS", "SA"):
            self.assertIn(ab, rows)

    def test_preseason_table_parses_with_empty_stats(self):
        rows, diag = cu.parse_nba_advanced_team(soup(HTML_2027_PRE))
        self.assertEqual(diag, "")
        self.assertEqual(len(rows), 30)
        for r in rows.values():
            self.assertEqual(r["gp"], 0)
            self.assertIsNone(r["ortg"])
            self.assertIsNone(r["net_rtg"])

    def test_table_inside_html_comment(self):
        html = table(games_row("BRK", "Brooklyn Nets", 3, 2), wrap_in_comment=True)
        rows, diag = cu.parse_nba_advanced_team(soup(html))
        self.assertEqual(diag, "")
        self.assertEqual(list(rows), ["BKN"])

    def test_name_fallback_when_no_href(self):
        row = games_row("XXX", "Utah Jazz", 1, 0).replace('<a href="/teams/XXX/2027.html">Utah Jazz</a>', "Utah Jazz")
        rows, _ = cu.parse_nba_advanced_team(soup(table(row)))
        self.assertEqual(list(rows), ["UTAH"])

    def test_net_rtg_derived_when_missing(self):
        rows, _ = cu.parse_nba_advanced_team(soup(table(games_row("DEN", "Denver Nuggets", 4, 1, net_rtg="", off_rtg="115.5", def_rtg="110.0"))))
        self.assertAlmostEqual(rows["DEN"]["net_rtg"], 5.5)

    def test_league_average_row_and_unmapped_team_skipped(self):
        logs = []
        html = table(games_row("MIA", "Miami Heat", 2, 2) + games_row("LGA", "League Average", 0, 0)
                     + games_row("SEA", "Seattle SuperSonics", 1, 0))
        with mock.patch.object(cu, "log", lambda m, lvl="INFO": logs.append((lvl, m))):
            rows, diag = cu.parse_nba_advanced_team(soup(html))
        self.assertEqual(list(rows), ["MIA"])
        self.assertTrue(any(l == "WARN" and "Seattle SuperSonics" in m for l, m in logs))

    def test_missing_table_has_specific_diagnostic(self):
        rows, diag = cu.parse_nba_advanced_team(soup("<html><head><title>Oops</title></head><body><table id='per_game-team'></table></body></html>"))
        self.assertEqual(rows, {})
        self.assertIn("no advanced-team table", diag)
        self.assertIn("per_game-team", diag)        # shows which tables ARE there
        self.assertIn("Oops", diag)

    def test_old_layout_regression(self):
        """The pre-fix parser looked for data-stat team_id/team_name and a four_factors table. A page
        in the current layout has neither -- prove the new parser doesn't depend on them, and that the
        four factors come out of advanced-team."""
        self.assertNotIn('data-stat="team_id"', HTML_2026)
        self.assertNotIn('data-stat="team_name"', HTML_2026)
        self.assertNotIn("four_factors", HTML_2026)
        rows, _ = cu.parse_nba_advanced_team(soup(HTML_2026))
        for k in ("efg_pct", "tov_pct", "orb_pct", "ft_rate", "opp_efg_pct", "opp_tov_pct", "drb_pct", "opp_ft_rate"):
            self.assertIsNotNone(rows["SA"][k], k)


class FetchPage(unittest.TestCase):
    def setUp(self):
        cu._NBA_STATS_CACHE.clear()
        self._d = mock.patch.object(cu, "NBA_BBREF_DELAY", 0)
        self._d.start()
        self.logs = []
        self._l = mock.patch.object(cu, "log", lambda m, lvl="INFO": self.logs.append((lvl, m)))
        self._l.start()

    def tearDown(self):
        self._d.stop()
        self._l.stop()
        cu._NBA_STATS_CACHE.clear()

    def run_fetch(self, resp, year=2026):
        sess = FakeSession({"NBA_%d.html" % year: resp})
        with mock.patch.object(cu, "_ref_session", sess):
            return cu.fetch_nba_bbref_season(year), sess

    def warns(self):
        return [m for l, m in self.logs if l == "WARN"]

    def test_ok(self):
        (rows, info), sess = self.run_fetch(FakeResp(HTML_2026))
        self.assertEqual(len(rows), 30)
        self.assertTrue(info["ok"])
        self.assertEqual(self.warns(), [])

    def test_cached_one_request(self):
        sess = FakeSession({"NBA_2026.html": FakeResp(HTML_2026)})
        with mock.patch.object(cu, "_ref_session", sess):
            cu.fetch_nba_bbref_season(2026)
            cu.fetch_nba_bbref_season(2026)
        self.assertEqual(len(sess.calls), 1)

    def test_cloudflare_403_is_loud(self):
        (rows, info), _ = self.run_fetch(FakeResp("denied", status=403))
        self.assertEqual(rows, {})
        self.assertFalse(info["ok"])
        w = " ".join(self.warns())
        self.assertIn("FETCH FAILED", w)
        self.assertIn("HTTP 403", w)
        self.assertIn("Cloudflare", w)

    def test_challenge_page_with_200_is_loud(self):
        (rows, _), _ = self.run_fetch(FakeResp("<html><title>Just a moment...</title></html>"))
        self.assertEqual(rows, {})
        self.assertIn("anti-bot challenge", " ".join(self.warns()))

    def test_network_exception_is_loud(self):
        (rows, _), _ = self.run_fetch(ConnectionError("boom"))
        self.assertEqual(rows, {})
        self.assertIn("request failed: boom", " ".join(self.warns()))

    def test_table_renamed_is_loud_not_silent(self):
        (rows, info), _ = self.run_fetch(FakeResp("<html><title>BBRef</title><table id='some_new_name'></table></html>"))
        self.assertEqual(rows, {})
        w = " ".join(self.warns())
        self.assertIn("0 teams parsed", w)
        self.assertIn("some_new_name", w)

    def test_partial_table_warns(self):
        (rows, _), _ = self.run_fetch(FakeResp(table(games_row("BOS", "Boston Celtics", 1, 0))))
        self.assertEqual(len(rows), 1)
        self.assertIn("expected 30", " ".join(self.warns()))


def mk_rows(gp, ortg=112.0, drtg=110.0, teams=None, **kw):
    out = {}
    for a in (teams or sorted(ESPN_30)):
        out[a] = {"name": a, "w": gp // 2, "l": gp - gp // 2, "gp": gp, "ortg": ortg, "drtg": drtg,
                  "net_rtg": None if ortg is None or drtg is None else ortg - drtg, "pace": 99.0, "ts_pct": .58, "efg_pct": .55, "tov_pct": 12.0, "orb_pct": 25.0, "ft_rate": .22,
                  "opp_efg_pct": .53, "opp_tov_pct": 13.0, "drb_pct": 75.0, "opp_ft_rate": .21, "mov": 2.0, "srs": 2.0, **kw}
    return out


class SelectSeason(unittest.TestCase):
    def test_preseason_keeps_prior(self):
        s = cu.select_nba_team_stats(mk_rows(0, ortg=None, drtg=None, net_rtg=None), mk_rows(82), 2027, 2026)
        self.assertEqual(s["mode"], "prior")
        self.assertEqual(s["seasonUsed"], 2026)
        self.assertEqual({r["season"] for r in s["teams"].values()}, {2026})
        self.assertEqual(len(s["teams"]), 30)

    def test_four_games_not_enough(self):
        s = cu.select_nba_team_stats(mk_rows(4), mk_rows(82), 2027, 2026)
        self.assertEqual(s["mode"], "prior")

    def test_five_games_everywhere_switches_to_current(self):
        s = cu.select_nba_team_stats(mk_rows(5), mk_rows(82), 2027, 2026)
        self.assertEqual(s["mode"], "current")
        self.assertEqual(s["seasonUsed"], 2027)
        self.assertEqual({r["season"] for r in s["teams"].values()}, {2027})

    def test_few_teams_ready_stays_prior(self):
        cur = mk_rows(5)
        for a in sorted(cur)[:7]:                 # 7 teams still at 3 GP -> only 23 ready (< 24)
            cur[a]["gp"] = 3
        self.assertEqual(cu.select_nba_team_stats(cur, mk_rows(82), 2027, 2026)["mode"], "prior")

    def test_laggards_keep_their_own_prior_row_when_current_is_active(self):
        cur = mk_rows(6)
        for a in sorted(cur)[:3]:
            cur[a]["gp"] = 4
        s = cu.select_nba_team_stats(cur, mk_rows(82), 2027, 2026)
        self.assertEqual(s["mode"], "mixed")
        self.assertEqual(s["teams"][sorted(cur)[0]]["season"], 2026)
        self.assertEqual(s["teams"][sorted(cur)[10]]["season"], 2027)

    def test_prior_page_missing_preseason_gives_nothing(self):
        s = cu.select_nba_team_stats(mk_rows(0, ortg=None, drtg=None, net_rtg=None), {}, 2027, 2026)
        self.assertEqual(s["mode"], "none")
        self.assertEqual(s["teams"], {})

    def test_prior_page_missing_but_thin_current_is_used(self):
        s = cu.select_nba_team_stats(mk_rows(6, teams=["BOS", "NY"]), {}, 2027, 2026)
        self.assertEqual(s["mode"], "current")
        self.assertEqual(set(s["teams"]), {"BOS", "NY"})


class SelectionEndToEnd(unittest.TestCase):
    """fetch_nba_team_advanced / fetch_nba_four_factors over a fake BBRef: 2027 page + 2026 page."""
    def setUp(self):
        cu._NBA_STATS_CACHE.clear()
        self.logs = []
        self._p = [mock.patch.object(cu, "NBA_BBREF_DELAY", 0),
                   mock.patch.object(cu, "NBA_TEAM_STATS_SOURCE", "bbref"),     # these tests are about the Basketball-Reference path (ESPN-primary: test_nba_espn.py)
                   mock.patch.object(cu, "log", lambda m, lvl="INFO": self.logs.append((lvl, m))),
                   mock.patch.dict(os.environ, {"NBA_SEASON_END_YEAR": "2027"})]
        for p in self._p:
            p.start()

    def tearDown(self):
        for p in self._p:
            p.stop()
        cu._NBA_STATS_CACHE.clear()

    def sess(self, html27):
        return FakeSession({"NBA_2027.html": FakeResp(html27), "NBA_2026.html": FakeResp(HTML_2026)})

    def test_preseason_uses_2026_and_logs_it(self):
        sess = self.sess(HTML_2027_PRE)
        with mock.patch.object(cu, "_ref_session", sess):
            adv = cu.fetch_nba_team_advanced()
            ff = cu.fetch_nba_four_factors()
        self.assertEqual(set(adv), ESPN_30)
        self.assertEqual(set(ff), ESPN_30)
        self.assertEqual({v["season"] for v in adv.values()}, {2026})
        self.assertAlmostEqual(adv["OKC"]["net_rtg"], 11.2)
        self.assertAlmostEqual(ff["DET"]["opp_ft_rate"], 0.251)
        self.assertEqual(len(sess.calls), 2)       # one request per season page, shared by both functions
        self.assertTrue(any("using season 2026 [prior]" in m for l, m in self.logs))
        self.assertEqual([m for l, m in self.logs if l == "WARN"], [])

    def test_regular_season_underway_uses_2027(self):
        bbref_abbr = {"BKN": "BRK", "CHA": "CHO", "PHX": "PHO", "WSH": "WAS", "NO": "NOP", "UTAH": "UTA",
                      "NY": "NYK", "GS": "GSW", "SA": "SAS"}
        full_name = {ab: n for n, ab in cu._NBA_NAME_TO_ESPN.items()}
        rows27 = "".join(games_row(bbref_abbr.get(a, a), full_name[a], 3, 3) for a in sorted(ESPN_30))
        sess = self.sess(table(rows27))
        with mock.patch.object(cu, "_ref_session", sess):
            adv = cu.fetch_nba_team_advanced()
        self.assertEqual(set(adv), ESPN_30)
        self.assertEqual({v["season"] for v in adv.values()}, {2027})
        self.assertEqual({v["gp"] for v in adv.values()}, {6})
        self.assertTrue(any("using season 2027 [current]" in m for l, m in self.logs))

    def test_total_failure_is_loud(self):
        sess = FakeSession({"NBA_2027.html": FakeResp("x", status=503), "NBA_2026.html": FakeResp("x", status=503)})
        with mock.patch.object(cu, "_ref_session", sess):
            adv = cu.fetch_nba_team_advanced()
            ff = cu.fetch_nba_four_factors()
        self.assertEqual((adv, ff), ({}, {}))
        warns = " ".join(m for l, m in self.logs if l == "WARN")
        self.assertIn("FETCH FAILED", warns)
        self.assertIn("no team stats at all", warns)
        self.assertIn("only 0/30", warns)

    def test_env_pins_old_season(self):
        with mock.patch.dict(os.environ, {"NBA_SEASON_END_YEAR": "2026"}):
            sess = FakeSession({"NBA_2026.html": FakeResp(HTML_2026), "NBA_2025.html": FakeResp("x", status=404)})
            with mock.patch.object(cu, "_ref_session", sess):
                adv = cu.fetch_nba_team_advanced()
        self.assertEqual({v["season"] for v in adv.values()}, {2026})     # 2026 page is "current" and complete


class Ratings(unittest.TestCase):
    def espn(self, w, l, diff):
        return {"w": str(w), "l": str(l), "diff": diff}

    def test_prior_only_regressed_and_ordered(self):
        prior = {"OKC": {"name": "OKC", "w": 64, "l": 18, "srs": 11.0, "mov": 11.2, "net_rtg": 11.2, "ortg": 118.9, "drtg": 107.7, "pace": 99.3},
                 "WSH": {"name": "WSH", "w": 20, "l": 62, "srs": -9.0, "mov": -9.5, "net_rtg": -9.0, "ortg": 105.0, "drtg": 114.0, "pace": 100.0}}
        out = cu.build_nba_team_ratings(prior, {}, {}, {"OKC": self.espn(0, 0, "0.0"), "WSH": self.espn(0, 0, "0.0")}, 2027, 2026)
        t = out["teamRatings"]["teams"]
        self.assertEqual(t["OKC"]["source"], "prior")
        self.assertAlmostEqual(t["OKC"]["strength"], 0.75 * 11.0, places=2)
        self.assertEqual(t["OKC"]["elo"], round(1550 + 28 * 0.75 * 11.0))
        self.assertLess(t["WSH"]["elo"], 1550)
        self.assertEqual(out["eloSeed"], {"OKC": t["OKC"]["elo"], "WSH": t["WSH"]["elo"]})
        self.assertEqual(t["OKC"]["prior"]["w"], 64)
        self.assertEqual(t["OKC"]["current"]["gp"], 0)
        self.assertAlmostEqual(t["OKC"]["priorWinPct"], 0.5 + 0.75 * (64 / 82 - 0.5), places=3)

    def test_current_results_blend_in(self):
        prior = {"BOS": {"name": "BOS", "w": 55, "l": 27, "srs": 6.0, "mov": 6.0, "net_rtg": 6.0, "ortg": 115, "drtg": 109, "pace": 98}}
        out0 = cu.build_nba_team_ratings(prior, {}, {}, {"BOS": self.espn(0, 0, "0.0")}, 2027, 2026)
        out20 = cu.build_nba_team_ratings(prior, {}, {}, {"BOS": self.espn(5, 15, "-8.0")}, 2027, 2026)   # 20 GP, bad start
        t = out20["teamRatings"]["teams"]["BOS"]
        self.assertEqual(t["source"], "prior+current")
        self.assertAlmostEqual(t["strength"], (20 * -8.0 + 20 * 0.75 * 6.0) / 40, places=2)   # equal weight at 20 GP
        self.assertLess(out20["eloSeed"]["BOS"], out0["eloSeed"]["BOS"])

    def test_no_data_defaults_to_mean(self):
        out = cu.build_nba_team_ratings({}, {}, {}, {"NY": {"w": "0", "l": "0"}}, 2027, 2026)
        self.assertEqual(out["eloSeed"]["NY"], 1550)
        self.assertEqual(out["teamRatings"]["teams"]["NY"]["source"], "default")

    def test_espn_prior_fallback_when_bbref_missing(self):
        out = cu.build_nba_team_ratings({}, {}, {"DET": self.espn(60, 22, "+8.2")}, {"DET": self.espn(0, 0, "0.0")}, 2027, 2026)
        t = out["teamRatings"]["teams"]["DET"]
        self.assertEqual(t["source"], "prior")
        self.assertAlmostEqual(t["strength"], 0.75 * 8.2, places=2)
        self.assertEqual((t["prior"]["w"], t["prior"]["l"]), (60, 22))

    def test_elo_clamped(self):
        out = cu.build_nba_team_ratings({"X": {"name": "X", "w": 1, "l": 81, "srs": -40.0, "w": 1, "l": 81}}, {}, {}, {}, 2027, 2026)
        self.assertEqual(out["eloSeed"]["X"], 1300)

    def test_block_shape(self):
        out = cu.build_nba_team_ratings(mk_rows(82), {}, {}, {}, 2027, 2026)
        b = out["teamRatings"]
        for k in ("seasonCurrent", "seasonPrior", "statsSeasonUsed", "statsMode", "params", "generated", "teams"):
            self.assertIn(k, b)
        self.assertEqual(set(out["eloSeed"]), ESPN_30)
        json.dumps(out)       # must be JSON-serializable


class StandingsAndPlayers(unittest.TestCase):
    def fake_standings(self, season):
        def entry(abbr):
            stats = [{"name": "wins", "displayValue": "0"}, {"name": "losses", "displayValue": "0"},
                     {"name": "winPercent", "displayValue": ".000"}, {"name": "gamesBehind", "displayValue": "-"},
                     {"name": "avgPointsFor", "displayValue": "0.0"}, {"name": "avgPointsAgainst", "displayValue": "0.0"},
                     {"name": "differential", "displayValue": "+3.5"}]
            return {"team": {"abbreviation": abbr}, "stats": stats}
        return {"children": [{"standings": {"entries": [entry(a) for a in sorted(ESPN_30)]}}]}

    def test_standings_season_in_url_and_diff(self):
        urls = []
        def fake_fetch(url, **kw):
            urls.append(url)
            return self.fake_standings(2027)
        with mock.patch.object(cu, "fetch_json", fake_fetch), mock.patch.dict(os.environ, {"NBA_SEASON_END_YEAR": "2027"}):
            out = cu.fetch_nba_standings()
            cu.fetch_nba_standings(2026)
        self.assertIn("season=2027&seasontype=2", urls[0])        # ESPN ignores plain `type=2` and returns preseason standings
        self.assertIn("season=2026&seasontype=2", urls[1])
        self.assertNotIn("&type=2", urls[0])
        self.assertEqual(len(out), 30)
        self.assertEqual(out["OKC"]["diff"], "+3.5")
        self.assertEqual(out["OKC"]["w"], "0")

    def test_standings_short_response_warns(self):
        logs = []
        with mock.patch.object(cu, "fetch_json", lambda url, **kw: {"children": []}), \
                mock.patch.object(cu, "log", lambda m, lvl="INFO": logs.append((lvl, m))):
            self.assertEqual(cu.fetch_nba_standings(2027), {})
        self.assertTrue(any(l == "WARN" and "0 teams" in m for l, m in logs))

    def test_player_stats_season_rule(self):
        zero = {a: {"w": "0", "l": "0"} for a in ESPN_30}
        six = {a: {"w": "3", "l": "3"} for a in ESPN_30}
        fifteen = {a: {"w": "8", "l": "7"} for a in ESPN_30}
        self.assertEqual(cu.nba_player_stats_season(zero, 2027), 2026)
        # tiers need >= NBA_TIER_MIN_GP games per player, so six team games must NOT flip yet
        self.assertEqual(cu.nba_player_stats_season(six, 2027), 2026)
        self.assertEqual(cu.nba_player_stats_season(fifteen, 2027), 2027)
        self.assertEqual(cu.nba_player_stats_season({}, 2027), 2026)

    def test_apply_tiers_only_missing(self):
        roster = {"a star": {"team": "LAL", "pos": "G", "rating": "PREMIUM", "ppg": 30.0},
                  "b star": {"team": "LAL", "pos": "G"}}
        prior = [{"name": "A Star", "gp": 70, "ppg": 12.0, "team": "LAL"},
                 {"name": "B Star", "gp": 70, "ppg": 30.0, "team": "LAL"}]
        cu.apply_nba_player_tiers(roster, prior, only_missing=True)
        self.assertEqual(roster["a star"]["ppg"], 30.0)   # current-season tier untouched
        self.assertTrue(roster["b star"].get("rating"))    # gap filled from last season

    def test_apply_tiers(self):
        roster = {"luka doncic": {"team": "LAL", "pos": "G"}, "role player": {"team": "LAL", "pos": "F"},
                  "bench guy": {"team": "LAL", "pos": "G"}, "injured vet": {"team": "LAL", "pos": "C"}}
        players = [{"name": "Luka Doncic", "gp": 64, "ppg": 33.5}, {"name": "Role Player", "gp": 70, "ppg": 14.0},
                   {"name": "Bench Guy", "gp": 70, "ppg": 6.0}, {"name": "Injured Vet", "gp": 8, "ppg": 26.0},
                   {"name": "Not On Roster", "gp": 70, "ppg": 30.0}]
        n = cu.apply_nba_player_tiers(roster, players)
        self.assertEqual(n, 2)
        self.assertEqual(roster["luka doncic"]["rating"], "PREMIUM")
        self.assertEqual(roster["role player"]["rating"], "GOOD")
        self.assertNotIn("rating", roster["bench guy"])
        self.assertNotIn("rating", roster["injured vet"])      # < 15 GP


class SeasonTypeHandling(unittest.TestCase):
    def event(self, stype):
        return {"id": "1", "date": "2026-10-04T23:00Z", "season": {"year": 2027, "type": stype},
                "status": {"type": {"state": "pre"}},
                "competitions": [{"competitors": [
                    {"homeAway": "home", "team": {"abbreviation": "DEN"}},
                    {"homeAway": "away", "team": {"abbreviation": "UTAH"}}],
                    "odds": [{"homeTeamOdds": {"moneyLine": -250}, "awayTeamOdds": {"moneyLine": 200}, "overUnder": 225.5}]}]}

    def test_espn_game_carries_season_type(self):
        g = cu._espn_game(self.event(1), "NBA")
        self.assertEqual((g["seasonType"], g["seasonYear"]), (1, 2027))
        g2 = cu._espn_game({k: v for k, v in self.event(2).items() if k != "season"}, "NBA")
        self.assertIsNone(g2["seasonType"])

    def best_bets(self, stype):
        g = cu._espn_game(self.event(stype), "NBA")
        g["homeML"], g["awayML"] = -250, 200
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(cu, "DATA", Path(tmp)), \
                mock.patch.object(cu, "log", lambda *a, **k: None):     # calculate_best_bets writes DATA/best_bets.json
            picks = cu.calculate_best_bets([g], [], [], {}, best_odds={})
        return [p for p in picks if p["sport"] == "NBA"]

    def test_preseason_games_make_no_picks_but_regular_season_does(self):
        self.assertEqual(self.best_bets(1), [])
        self.assertTrue(self.best_bets(2))            # same game labelled regular season -> picks appear automatically
        self.assertTrue(self.best_bets(None))         # unknown season type is not suppressed

    def test_week_schedule_carries_season_type(self):
        payload = {"events": [self.event(1)]}
        with mock.patch.object(cu, "fetch_json", lambda url, **kw: payload), mock.patch.object(cu.time, "sleep", lambda s: None):
            sched = cu.fetch_week_schedule("basketball/nba", "nba", 15)
        self.assertTrue(sched)
        self.assertEqual({s["seasonType"] for s in sched}, {1})


class CarryForward(unittest.TestCase):
    def test_empty_fresh_uses_previous_loudly(self):
        logs = []
        with mock.patch.object(cu, "log", lambda m, lvl="INFO": logs.append((lvl, m))):
            self.assertEqual(cu._nba_carry_forward("teamAdv", {}, {"teamAdv": {"OKC": {"ortg": 1}}}, "teamAdv"), {"OKC": {"ortg": 1}})
            self.assertEqual(cu._nba_carry_forward("teamAdv", {"A": 1}, {"teamAdv": {"OKC": 1}}, "teamAdv"), {"A": 1})
            self.assertEqual(cu._nba_carry_forward("teamAdv", {}, {}, "teamAdv"), {})
        self.assertEqual([l for l, _ in logs], ["WARN", "WARN"])

    def test_collect_with_everything_failing_carries_ratings(self):
        prev = {"teamRatings": {"teams": {"OKC": {}}}, "eloSeed": {"OKC": 1700}, "teamAdv": {"OKC": {"ortg": 1.0}}, "fourFactors": {"OKC": {"efg_pct": .5}}}
        with mock.patch.object(cu, "fetch_json", lambda *a, **k: None), \
                mock.patch.object(cu, "fetch_nba_roster", lambda: {}), \
                mock.patch.object(cu, "NBA_BBREF_DELAY", 0), \
                mock.patch.object(cu, "_nba_http_get", mock.Mock(side_effect=ConnectionError("ESPN down"))), \
                mock.patch.object(cu.time, "sleep", lambda s: None), \
                mock.patch.object(cu, "log", lambda *a, **k: None), \
                mock.patch.object(cu, "_ref_session", FakeSession({"basketball-reference.com": FakeResp("x", status=503)})), \
                mock.patch.dict(os.environ, {"NBA_SEASON_END_YEAR": "2027"}):
            cu._NBA_STATS_CACHE.clear()
            out = cu.collect_nba_season_data(prev_nba=prev)
            cu._NBA_STATS_CACHE.clear()
        self.assertEqual(out["eloSeed"], {"OKC": 1700})
        self.assertEqual(out["teamAdv"], {"OKC": {"ortg": 1.0}})
        self.assertEqual(out["fourFactors"], {"OKC": {"efg_pct": .5}})


if __name__ == "__main__":
    unittest.main(verbosity=1)
