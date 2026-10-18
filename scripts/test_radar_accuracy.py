#!/usr/bin/env python3
"""Spider-chart (radar) accuracy, found by auditing every axis of every sport on 2026-10-17: units, windows, per-game rates and data that was silently reading as the 50 midpoint.

    python3 scripts/test_radar_accuracy.py
"""
import functools, http.server, threading, unittest
from pathlib import Path
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent


class Radar(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        H = type("Q", (http.server.SimpleHTTPRequestHandler,), {"log_message": lambda *a: None})
        cls.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(H, directory=str(ROOT / "docs")))
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.pw = sync_playwright().start(); cls.b = cls.pw.chromium.launch(); cls.pg = cls.b.new_page()
        cls.pg.route("**/*", lambda r: r.continue_() if r.request.url.startswith("http://127.0.0.1") else r.abort())
        cls.pg.goto(f"http://127.0.0.1:{cls.srv.server_address[1]}/app.html?nosb=1", wait_until="domcontentloaded")
        cls.pg.wait_for_function("typeof _nbaOffenseRadarValues==='function'&&!!_NFL_DATA&&!!_CFB_DATA&&!!(window.__CV_DATA&&window.__CV_DATA.nba&&window.__CV_DATA.nba.teamAdv)&&!!window.__CV_FBREF&&typeof NHL!=='undefined'&&Object.keys(NHL).length>20&&NHL.COL&&NHL.COL.gp>0", timeout=90000)
        cls.pg.evaluate("Promise.all([loadLiigaScheduleData(),loadShlScheduleData(),loadNlaScheduleData(),loadExtraligaScheduleData(),loadLiigaQuantHockeyData(),loadShlQuantHockeyData(),loadNlaQuantHockeyData(),loadExtraligaQuantHockeyData(),...['pl','liga','ita','bl'].map(k=>loadLeagueOptaStats(k))])")

    @classmethod
    def tearDownClass(cls):
        cls.b.close(); cls.pw.stop(); cls.srv.shutdown()

    def test_every_axis_is_in_range_and_most_teams_are_not_stuck_at_the_midpoint(self):
        """Loose, data-light guard: every value is 0-100, and no axis (outside a short allow-list of axes whose source is known to be empty) has most of the league parked at 50."""
        r = self.pg.evaluate("""() => { const out = {}; const add = (k, labels, vecs) => { labels.forEach((l, i) => { const v = vecs.map(x => x[i]); out[k + ' / ' + l] = {n: v.length, bad: v.filter(x => !(x >= 0 && x <= 100)).length, mid: v.filter(x => x === 50).length / v.length}; }); };
          const nt = Object.keys(NHL).filter(t => NHL[t].gp > 0);
          add('NHL off', ['GF', 'PP', 'CF', 'HDCF', 'xGF', 'FIN'], nt.map(t => _nhlOffenseRadarValues(NHL[t], t))); add('NHL def', ['GA', 'PK', 'SV', 'HDSV', 'GAA', 'GSAX'], nt.map(t => _nhlDefenseRadarValues(NHL[t])));
          add('NHL mat', ['O', 'D', 'G', 'ST', 'SPD', 'XG'], nt.map(t => _nhlRadarAxisValues(NHL[t], NHL[nt[0]])));
          const nb = Object.keys(window.__CV_DATA.nba.teamAdv);
          add('NBA mat', ['O', 'D', 'N', 'P', 'T', 'B'], nb.map(_nbaMatchupRadarValues)); add('NBA off', ['O', 'e', 'T', 'R', 'F', 'B'], nb.map(_nbaOffenseRadarValues)); add('NBA def', ['D', 'e', 'F', 'R', 'N', 'W'], nb.map(_nbaDefenseRadarValues));
          const ft = Object.keys(_NFL_DATA.stats || {});
          add('NFL mat', ['O', 'D', 'PE', 'TO', 'PD', 'H'], ft.map(_nflMatchupRadarValues)); add('NFL off', ['P', 'R', '3', 'TO', 'PT', 'RT'], ft.map(_nflOffenseRadarValues)); add('NFL def', ['P', 'Y', 'PY', 'RY', 'PT', 'RT'], ft.map(_nflDefenseRadarValues));
          for (const k of ['pl', 'liga', 'ita', 'bl']) { const names = Object.keys(window.__CV_FBREF[k].teams);
            add('SOC ' + k + ' off', ['x', 'c', 'p', 'f', 'd', 'g'], names.map(n => _socOffenseRadarValues(n, k))); add('SOC ' + k + ' def', ['xa', 'ga', 'oc', 'gx', 'pp', 'da'], names.map(n => _socDefenseRadarValues(n, k))); }
          return out; }""")
        allow = {"NHL off / HDCF",          # MoneyPuck's high-danger share is empty until the scraper fix lands in the next refresh
                 "NFL mat / H"}             # home edge is one model-wide constant
        for k, v in r.items():
            self.assertEqual(v["bad"], 0, k)
            if k not in allow:
                self.assertLess(v["mid"], 0.5, (k, v))

    def test_european_points_percentage_uses_three_points_per_game(self):
        r = self.pg.evaluate("""() => { const f = (pts) => _hkBasicRadarValues({gp: 10, pts}, {gf: 3, ga: 3, gm: 5.5})[2];
          return [f(30), f(15), f(0)]; }""")
        self.assertAlmostEqual(r[0], 100 * (100 - 20) / 60 if False else 100, places=6)          # 3 points a game (a perfect record) = the top of the chart
        self.assertAlmostEqual(r[1], 50.0, places=6)                                              # 1.5 a game = exactly average = the middle (it was divided by 2: 75% -> off the top)
        self.assertEqual(r[2], 0)

    def test_soccer_defense_axes_use_games_played_from_the_attacking_row(self):
        r = self.pg.evaluate("""() => { window.__CV_OPTA = window.__CV_OPTA || {}; const keep = window.__CV_OPTA.zz;
          window.__CV_OPTA.zz = {attacking: [{team: 'Alpha', played: 4}], defending: [{team: 'Alpha', goals_against: 4, xg_against: 6, shot_conv_against: 10}], pressing: [{team: 'Alpha', ppda: 10, defensive_actions: 120}], passing: [], sequences: []};
          const v = _socDefenseRadarValues('Alpha', 'zz'); window.__CV_OPTA.zz = keep; return v; }""")
        self.assertNotEqual(r[0], 50); self.assertNotEqual(r[1], 50); self.assertNotEqual(r[3], 50); self.assertNotEqual(r[5], 50)   # xGA, GA, goals-vs-xG and defensive actions were all stuck at 50
        self.assertAlmostEqual(r[0], 100 - (1.5 - .6) / 1.8 * 100, places=4)                                                        # 6 xGA over 4 games = 1.5
        self.assertAlmostEqual(r[1], 100 - (1.0 - .5) / 2.0 * 100, places=4)                                                        # 4 GA over 4 games = 1.0

    def test_nba_offense_and_defense_axes_use_live_numbers_for_every_team(self):
        r = self.pg.evaluate("""() => { const t = Object.keys(window.__CV_DATA.nba.teamAdv).filter(k => !NBA_BBREF[k]); return t.length ? [_nbaOffenseRadarValues(t[0]), _nbaDefenseRadarValues(t[0])] : null; }""")
        self.assertIsNotNone(r, "need a team that is not in the static NBA_BBREF table")
        for vec in r:
            self.assertLessEqual(sum(1 for x in vec if x == 50), 1, vec)                         # live ratings / four factors, not the static table

    def test_nfl_touchdown_axes_are_per_game_rates(self):
        r = self.pg.evaluate("""() => { const keep = [_NFL_DATA.standings, _NFL_DATA.stats]; _NFL_DATA.standings = {ZZZ: {wins: 3, losses: 2, ties: 0}}; _NFL_DATA.stats = {ZZZ: {offense: {passingTouchdowns: 8, rushingTouchdowns: 5}, defenseAllowed: {passingTouchdowns: 8, rushingTouchdowns: 5}}};
          const o = _nflOffenseRadarValues('ZZZ'), d = _nflDefenseRadarValues('ZZZ'); [_NFL_DATA.standings, _NFL_DATA.stats] = keep; return {o, d}; }""")
        self.assertAlmostEqual(r["o"][4], (8 / 5 - .4) / 2.4 * 100, places=4); self.assertAlmostEqual(r["o"][5], (5 / 5 - .2) / 2.2 * 100, places=4)   # 1.6 pass TD and 1.0 rush TD a game
        self.assertAlmostEqual(r["d"][4], 100 - (8 / 5 - .5) / 2.3 * 100, places=4)

    def test_nfl_third_axis_is_no_longer_a_copy_of_the_defense_axis(self):
        r = self.pg.evaluate("""() => { const ft = Object.keys(_NFL_DATA.stats || {}); return ft.map(k => { const v = _nflMatchupRadarValues(k); return v[1] === v[2]; }).filter(Boolean).length; }""")
        self.assertLess(r, 4)

    def test_nhl_xg_share_falls_back_to_the_live_moneypuck_numbers(self):
        r = self.pg.evaluate("""() => { const nt = Object.keys(NHL).filter(t => NHL[t].gp > 0 && _nhlLiveCf(t)?.xgfPct != null); const a = nt[0], b = nt[1];
          const vals = [_nhlRadarAxisValues(NHL[a], NHL[b])[5], _nhlRadarAxisValues(NHL[b], NHL[a])[5]]; return vals; }""")
        self.assertNotEqual(r[0], 50); self.assertAlmostEqual(r[0] + r[1], 100, places=4)         # the two sides mirror each other around 50

    def test_nhl_non_share_high_danger_numbers_are_ignored(self):
        r = self.pg.evaluate("""() => { MONEYPUCK.teams.ZZ9 = {'5on5': {cfPct: .5, hdcfPct: 12, xgfPct: .5}}; const v = _nhlLiveCf('ZZ9').hdcfPct; delete MONEYPUCK.teams.ZZ9; return v; }""")
        self.assertIsNone(r)


if __name__ == "__main__":
    unittest.main(verbosity=2)
