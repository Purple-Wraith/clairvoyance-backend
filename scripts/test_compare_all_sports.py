#!/usr/bin/env python3
"""COMPARE STATS + FORM tabs for NBA, NFL, CFB and soccer (PL, La Liga, Serie A, Bundesliga, Champions League) (owner, 2026-10-17), built from each sport's own data.

    python3 scripts/test_compare_all_sports.py
"""
import functools, http.server, threading, unittest
from pathlib import Path
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent


class Sports(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        H = type("Q", (http.server.SimpleHTTPRequestHandler,), {"log_message": lambda *a: None})
        cls.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(H, directory=str(ROOT / "docs")))
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.pw = sync_playwright().start(); cls.b = cls.pw.chromium.launch()
        cls.pg = cls.b.new_page(viewport={"width": 390, "height": 800})
        cls.errors = []
        cls.pg.on("pageerror", lambda e: cls.errors.append(str(e)))
        cls.pg.route("**/*", lambda r: r.continue_() if r.request.url.startswith("http://127.0.0.1") else r.abort())
        cls.pg.goto(f"http://127.0.0.1:{cls.srv.server_address[1]}/app.html?nosb=1", wait_until="domcontentloaded")
        cls.pg.wait_for_function("typeof _nbaCompareTabs==='function'&&typeof _nflCompareTabs==='function'&&typeof _cfbCompareTabs==='function'&&typeof _socCompareTabs==='function'&&!!_NFL_DATA&&!!_CFB_DATA&&!!window._NBA_SCHED&&!!(window.__CV_DATA&&window.__CV_DATA.nba&&window.__CV_DATA.nba.teamAdv)&&!!window.__CV_FBREF", timeout=90000)
        cls.pg.evaluate("Promise.all(['pl','liga','ita','bl','cl'].map(k=>loadLeagueOptaStats(k)))")

    @classmethod
    def tearDownClass(cls):
        cls.b.close(); cls.pw.stop(); cls.srv.shutdown()

    def labels(self, tabs):
        return [t["label"] for t in tabs]

    def test_nba_stats_and_form(self):
        r = self.pg.evaluate("""() => { const t = _nbaCompareTabs('DEN', 'LAL', '2026-10-21T01:30Z'); return {labels: t.map(x => x.label), stats: t[0].svg, form: t[1].svg}; }""")
        self.assertEqual(r["labels"], ["STATS", "FORM"])
        for needle in ("NET RATING", "OFFENSIVE RATING", "DEFENSIVE RATING", "EFFECTIVE FG %", "ELO RATING", "RECORD (", "leads"):
            self.assertIn(needle, r["stats"])
        self.assertIn("Completed regular-season games only", r["form"])                     # the preseason is excluded from form

    def test_nba_form_ignores_the_preseason(self):
        r = self.pg.evaluate("""() => { const sav = window._NBA_SCHED; window._NBA_SCHED = {games: [
            {home: 'DEN', away: 'LAL', state: 'post', homeScore: 100, awayScore: 90, date: '2026-10-10T00:00Z', preseason: true, seasonType: 1},
            {home: 'DEN', away: 'BOS', state: 'post', homeScore: 120, awayScore: 110, date: '2026-10-21T00:00Z', preseason: false, seasonType: 2, homeName: 'Denver', awayName: 'Boston'}]};
          const t = _nbaCompareTabs('DEN', 'LAL', '2026-10-23T00:00Z'); window._NBA_SCHED = sav; return t[1].svg; }""")
        self.assertEqual(r.count("mom-pip w"), 1); self.assertNotIn("Los Angeles", r)         # one regular-season win for DEN; LAL has no counted games

    def test_nfl_stats_and_form_skip_the_preseason(self):
        r = self.pg.evaluate("""() => { const W = _NFL_DATA.weeks, flat = Object.values(W).flat(); const g = flat.find(x => x.seasonType === 2 && x.state === 'pre') || flat.find(x => x.seasonType === 2);
          const t = _nflCompareTabs(g); const pre = flat.filter(x => x.seasonType === 1 && x.state === 'post').length;
          return {labels: t.map(x => x.label), stats: t[0].svg, form: t[1].svg, pre, home: g.home, away: g.away}; }""")
        self.assertEqual(r["labels"], ["STATS", "FORM"])
        for needle in ("POINTS FOR /GAME", "POINT DIFF /GAME", "3RD-DOWN CONVERSION %", "TURNOVER DIFFERENTIAL", "DAYS OF REST", "leads"):
            self.assertIn(needle, r["stats"])
        self.assertIn("preseason excluded", r["form"])

    def test_cfb_stats_and_form(self):
        r = self.pg.evaluate("""() => { const flat = Object.values(_CFB_DATA.weeks).flat(); const g = flat.find(x => x.state === 'pre' && _CFB_DATA.fpi[x.home] && _CFB_DATA.fpi[x.away]) || flat.find(x => x.state === 'pre');
          const ens = cfbEns(g.home, g.away, g.overUnder, g), facts = _cfbAnalysisFacts(g, ens.mcD, ens); const t = _cfbCompareTabs(g, facts);
          return {labels: t.map(x => x.label), stats: t[0].svg, form: t[1].svg}; }""")
        self.assertEqual(r["labels"], ["STATS", "FORM"])
        for needle in ("PASSING YARDS /GAME", "ESPN FPI", "TURNOVER DIFFERENTIAL", "STRENGTH-OF-SCHEDULE RANK", "leads"):
            self.assertIn(needle, r["stats"])

    def test_every_soccer_league_gets_stats_and_a_draw_aware_form(self):
        out = self.pg.evaluate("""() => { const o = {}; const names = {pl: 'Premier League', liga: 'La Liga', ita: 'Serie A', bl: 'Bundesliga', cl: 'Champions League'};
          for (const k of Object.keys(names)) { const teams = Object.keys(window.__CV_FBREF[k].teams); const g = {home: teams[0], away: teams[1]};
            const t = _socCompareTabs(k, g, names[k]); o[k] = {labels: t.map(x => x.label), stats: t[0].svg, form: t[1].svg, note: names[k]}; }
          return o; }""")
        for k, v in out.items():
            self.assertEqual(v["labels"], ["STATS", "FORM"], k)
            for needle in ("GOALS FOR /GAME", "POINTS /GAME", "xG FOR /GAME", "POSSESSION %", "VENUE RECORD", "leads"):
                self.assertIn(needle, v["stats"], (k, needle))
            self.assertIn(v["note"], v["stats"], k)                                          # league named in the footnote
            self.assertIn("mom-pip", v["form"], k)
        self.assertIn("Champions League and domestic", out["cl"]["form"])                    # CL form merges domestic games
        self.assertIn("domestic league games", out["cl"]["stats"])

    def test_opta_finds_the_clubs_it_names_differently(self):
        """Man City / Spurs / FC Bayern / Paris SG ... were not matched, so their OFFENSE / DEFENSE radar axes read as the 50 midpoint (found 2026-10-17)."""
        r = self.pg.evaluate("""() => { const rows = ['Man City', 'Man Utd', 'Spurs', 'FC Bayern', 'Bayer 04', 'Köln', "M'gladbach", 'Paris SG', 'Inter', 'Milan', 'Atlético', 'Arsenal'].map(t => ({team: t}));
          const q = ['Manchester City', 'Manchester United', 'Tottenham Hotspur', 'Bayern Munich', 'Bayer Leverkusen', 'FC Cologne', 'Borussia Mönchengladbach', 'Paris Saint-Germain', 'Internazionale', 'AC Milan', 'Atletico Madrid', 'Arsenal', 'Nobody FC'];
          return q.map(n => { const t = _optaFindTeam(rows, n); return t ? t.team : null; }); }""")
        self.assertEqual(r, ["Man City", "Man Utd", "Spurs", "FC Bayern", "Bayer 04", "Köln", "M'gladbach", "Paris SG", "Inter", "Milan", "Atlético", "Arsenal", None])

    def test_draws_get_their_own_bubble_and_record(self):
        html = self.pg.evaluate("""() => _cmpFormHTML('A', 'H', [{date: '2026-10-02', home: true, opp: 'X', gf: 1, ga: 1, win: false, draw: true}, {date: '2026-10-01', home: false, opp: 'Y', gf: 2, ga: 0, win: true, draw: false}], [], [], [], '', {draws: true})""")
        self.assertIn("mom-pip d", html); self.assertIn("1-1-0", html); self.assertIn("= DRAW", html); self.assertIn("D1 streak", html)

    def test_missing_data_degrades_not_throws(self):
        out = self.pg.evaluate("""() => ({nba: _nbaCompareTabs('XXX', 'YYY', null).map(x => x.label), nfl: _nflCompareTabs({home: 'XXX', away: 'YYY', date: '2026-10-20T00:00Z'}).map(x => x.label),
          cfb: _cfbCompareTabs({home: 'XXX', away: 'YYY'}, {}).map(x => x.label), soc: _socCompareTabs('pl', {home: 'Nobody FC', away: 'Ghost United'}, 'Premier League').map(x => x.label)})""")
        self.assertEqual(out, {"nba": ["STATS", "FORM"], "nfl": ["STATS", "FORM"], "cfb": ["STATS", "FORM"], "soc": ["STATS", "FORM"]})
        self.assertEqual(self.errors, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
