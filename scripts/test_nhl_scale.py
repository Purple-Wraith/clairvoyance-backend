#!/usr/bin/env python3
"""nhlMC scoring scale (2026-10-13): lambdas were gf60*.55 (~1.7/team), so every projection ran 3-4.5 goals vs a real ~6.1 and nearly every UNDER 6.5 graded 85-97%.

Pins the model to the real scoring environment using a FIXED synthetic league (no live data), plus the current-season-only fade (priorW):
  - a league-average matchup projects ~6 goals, not ~3.5
  - the league-wide mean projected total sits in 5.5-6.4
  - a 70%-prior (5 games played) team's current-only signals count 30%, so extreme gsax/hv do not swing the total
"""
import functools, http.server, threading, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
from playwright.sync_api import sync_playwright  # noqa: E402

SETUP = """() => {
  const T = Object.keys(NHL);
  // fixed league: every team gf60=ga60=3.0, neutral extras; real structure kept (hv/edge objects), live signals zeroed
  for (const t of T) { const n = NHL[t]; n.gf60 = 3.0; n.ga60 = 3.0; n.gp = 8; n.gsax = 0; n.hdsv = null; n.sv = .91; n.pp = _NHL_LG_PP; n.pk = _NHL_LG_PK; n.hv = {off:0, fin:0, def:0, pp:0, pk:0}; n.edge = {}; n.priorW = .3; n._syntheticEntry = true; }
  return T.length;
}"""


class Scale(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        H = type("Q", (http.server.SimpleHTTPRequestHandler,), {"log_message": lambda *a: None})
        cls.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(H, directory=str(ROOT / "docs")))
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.pw = sync_playwright().start()
        cls.b = cls.pw.chromium.launch()
        cls.pg = cls.b.new_page()
        cls.pg.route("**/*", lambda r: r.continue_() if r.request.url.startswith("http://127.0.0.1") else r.abort())
        cls.pg.goto(f"http://127.0.0.1:{cls.srv.server_address[1]}/app.html?nosb=1", wait_until="domcontentloaded")
        cls.pg.wait_for_function("typeof nhlMC==='function'&&typeof NHL!=='undefined'&&Object.keys(NHL).length>20&&typeof _NHL_LG_PP!=='undefined'", timeout=90000)
        cls.pg.wait_for_timeout(3000)    # let boot-time data patches land BEFORE we overwrite the league
        cls.n = cls.pg.evaluate(SETUP)

    @classmethod
    def tearDownClass(cls):
        cls.b.close(); cls.pw.stop(); cls.srv.shutdown()

    def mc(self, h, a, line=6.5):
        return self.pg.evaluate("([h,a,l]) => { const r = nhlMC(h, a, 20000, l); return {t: r.avgT, o: r.overP, hw: r.hwP}; }", [h, a, line])

    def test_league_average_matchup_projects_about_six_goals(self):
        r = self.mc("BOS", "CHI")
        self.assertGreater(r["t"], 5.6, r); self.assertLess(r["t"], 6.4, r)
        self.assertGreater(r["o"], 0.3, r); self.assertLess(r["o"], 0.6, r)        # a 6.5 line on an average game is nowhere near a 90% under

    def test_all_pairs_mean_total_is_the_real_scoring_environment(self):
        m = self.pg.evaluate("""() => { const T = Object.keys(NHL).slice(0, 12); let s = 0, c = 0; for (const a of T) for (const b of T) { if (a === b) continue; s += nhlMC(a, b, 400, 6.5).avgT; c++; } return s / c; }""")
        self.assertGreater(m, 5.5); self.assertLess(m, 6.4)       # neutral synthetic league lands ~5.75 (the .93 norm is fitted so the REAL, spread-out league lands ~6.1)

    def test_current_only_signals_are_faded_by_the_prior_weight(self):
        j = self.pg.evaluate("""() => { NHL.COL.gsax = -9; NHL.COL.hdsv = .4; NHL.COL.priorW = .9; const lo = nhlMC('BOS','COL',20000,6.5).avgT;
          NHL.COL.priorW = 0; const hi = nhlMC('BOS','COL',20000,6.5).avgT; NHL.COL.gsax = 0; NHL.COL.hdsv = null; NHL.COL.priorW = .3; return {lo, hi}; }""")
        self.assertLess(j["lo"], j["hi"] - 0.1, j)          # same awful goalie numbers: 90% last-season weight mutes them, 0% lets them through
        self.assertLess(j["lo"], 6.6, j)


if __name__ == "__main__":
    unittest.main(verbosity=2)
