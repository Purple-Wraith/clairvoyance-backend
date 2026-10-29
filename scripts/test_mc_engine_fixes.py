#!/usr/bin/env python3
"""MC engine fixes of the 2026-10-29 audit (docs/app.html):
  1. cfbMC expected total uses offense AND defence: (hPF + aPA)/2 + (aPF + hPA)/2 (it was points allowed only: -3.9 pts on 448 finished games), falling back to the old forms without scoring data;
  2. hockey O/U counts the overtime goal (books grade regulation + OT, no shootout goal): an even regulation tie at floor(line) goes over -> P(over) +~3 pts at 4.5 / 6.5, nothing at 5.5 / 7.5.
     Exact in the four euro engines (_hkOtGoalOverAdd), in expectation inside nhlMC's sim;
  3. nbaMC draws margin and total directly with NBA_SIGMA_MARGIN / NBA_SIGMA_TOTAL (it implied ~24 / ~24), and the spread chip uses the same margin sigma.

    python3 scripts/test_mc_engine_fixes.py
"""
import functools, http.server, math, re, socketserver, threading, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a, **k):
        pass


def pmf(l, k):
    return math.exp(-l + k * math.log(l) - math.lgamma(k + 1))


class Engines(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        socketserver.TCPServer.allow_reuse_address = True
        cls.srv = socketserver.TCPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=str(ROOT / "docs")))
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.pw = sync_playwright().start(); cls.b = cls.pw.chromium.launch()
        cls.pg = cls.b.new_page()
        cls.pg.goto(f"http://127.0.0.1:{cls.srv.server_address[1]}/app.html?nosb=1&nosw=1")
        cls.pg.wait_for_function("typeof nhlMC==='function'&&typeof cfbMC==='function'&&typeof nbaMC==='function'&&typeof shlMC==='function'"); cls.pg.wait_for_timeout(3500)

    @classmethod
    def tearDownClass(cls):
        cls.b.close(); cls.pw.stop(); cls.srv.shutdown()

    # ── 1. CFB ──────────────────────────────────────────────────────────────────────────
    CFB_JS = """([drop])=>{const D=_CFB_DATA,n=v=>{const x=parseFloat(v);return isNaN(x)?null:x};
      const g=Object.values(D.weeks).flat().find(x=>{const h=D.stats[x.home],a=D.stats[x.away];return h&&a&&[h.offense?.totalPointsPerGame,a.offense?.totalPointsPerGame,h.defenseAllowed?.totalPointsPerGame,a.defenseAllowed?.totalPointsPerGame].every(v=>n(v)!=null)});
      const h=D.stats[g.home],a=D.stats[g.away];
      const hPF=n(h.offense.totalPointsPerGame),aPF=n(a.offense.totalPointsPerGame),hPA=n(h.defenseAllowed.totalPointsPerGame),aPA=n(a.defenseAllowed.totalPointsPerGame);
      let saved=null;if(drop){saved=a.offense.totalPointsPerGame;a.offense.totalPointsPerGame=null;}
      const mc=cfbMC(g.home,g.away,40000,null,{neutralSite:true});
      if(drop)a.offense.totalPointsPerGame=saved;
      return{avg:mc.avgTotal,two:((hPF+aPA)+(aPF+hPA))/2,old:hPA+aPA}}"""

    def test_cfb_total_uses_offense_and_defence(self):
        r = self.pg.evaluate(self.CFB_JS, [False])
        self.assertAlmostEqual(r["avg"], r["two"], delta=0.6)
        if abs(r["two"] - r["old"]) > 2:
            self.assertGreater(abs(r["avg"] - r["old"]), 1.0)          # genuinely moved off the defence-only number

    def test_cfb_falls_back_to_defence_only_without_scoring_data(self):
        r = self.pg.evaluate(self.CFB_JS, [True])
        self.assertAlmostEqual(r["avg"], r["old"], delta=0.6)

    def test_cfb_backtest_bias_is_small(self):
        r = self.pg.evaluate("""()=>{const rows=[];Object.values(_CFB_DATA.weeks).flat().filter(g=>g.state==='post'&&g.homeScore!=null).forEach(g=>{
            try{const mc=cfbMC(g.home,g.away,1500,null,Object.assign({},g,{spread:null,homeML:null,awayML:null,overUnder:null}));if(mc)rows.push(mc.avgTotal-(g.homeScore+g.awayScore));}catch(e){}});
          return{n:rows.length,bias:rows.reduce((a,b)=>a+b,0)/rows.length}}""")
        self.assertGreater(r["n"], 200)
        self.assertLess(abs(r["bias"]), 1.5)                           # was -3.9

    # ── 2. hockey overtime goal ─────────────────────────────────────────────────────────
    def test_ot_goal_term_only_on_even_floor_lines(self):
        f = lambda line, lg="SHL": self.pg.evaluate("([l,g])=>_hkOtGoalOverAdd(3.1,3.0,l,g)", [line, lg])
        self.assertAlmostEqual(f(6.5), 0.52 * pmf(3.1, 3) * pmf(3.0, 3), places=9)
        self.assertAlmostEqual(f(4.5), 0.52 * pmf(3.1, 2) * pmf(3.0, 2), places=9)
        self.assertAlmostEqual(f(8.5), 0.52 * pmf(3.1, 4) * pmf(3.0, 4), places=9)
        self.assertEqual(f(5.5), 0); self.assertEqual(f(7.5), 0); self.assertEqual(f(0.5), 0)
        self.assertAlmostEqual(f(6.5, "NHL"), 0.57 * pmf(3.1, 3) * pmf(3.0, 3), places=9)
        self.assertGreater(f(6.5), 0.025); self.assertLess(f(6.5), 0.035)  # the ~3 points the audit measured

    def test_euro_engines_apply_it(self):
        r = self.pg.evaluate("""()=>{const out=[];
          [['SHL',shlMC,_SHL_DATA],['LIIGA',liigaMC,_LIIGA_DATA],['NLA',nlaMC,_NLA_DATA],['EXTRALIGA',extraligaMC,_EXTRALIGA_DATA]].forEach(([lg,fn,D])=>{
            const g=(D.games||[]).find(x=>x.state==='pre');if(!g)return;
            [4.5,5.5,6.5].forEach(line=>{const mc=fn(g.home,g.away,line);
              const tot=mc.avgH+mc.avgA;let cdf=0;for(let k=0;k<=Math.floor(line);k++)cdf+=_socPoissonPmf(tot,k);
              const want=_hkUnderBiasP(Math.min(.999,1-cdf+_hkOtGoalOverAdd(mc.avgH,mc.avgA,line,lg)),line,lg);
              const plain=_hkUnderBiasP(1-cdf,line,lg);
              out.push({lg,line,got:mc.overP,want,plain});});});
          return out}""")
        self.assertGreaterEqual(len(r), 9)
        for x in r:
            self.assertAlmostEqual(x["got"], x["want"], places=9, msg=x)
            if x["line"] == 5.5:
                self.assertAlmostEqual(x["got"], x["plain"], places=9, msg=x)      # untouched at 5.5
            else:
                self.assertGreater(x["got"], x["plain"], msg=x)

    def test_nhl_sim_counts_it_in_expectation(self):
        r = self.pg.evaluate("""()=>{const g=(_NHL_SCHEDULE_DATA.games||[]).find(x=>x.state==='post');const out={};
          [4.5,5.5,6.5].forEach(line=>{const mc=nhlMC(g.home,g.away,300000,line);let cdf=0;const tot=mc.avgH+mc.avgA;for(let k=0;k<=Math.floor(line);k++)cdf+=_socPoissonPmf(tot,k);
            out[line]={got:mc.overP,plain:1-cdf,add:_hkOtGoalOverAdd(mc.avgH,mc.avgA,line,'NHL'),conf:mc.gpConf};});return out}""")
        for line, x in r.items():
            self.assertEqual(x["conf"], 1)
            self.assertAlmostEqual(x["got"], x["plain"] + x["add"], delta=0.012, msg=(line, x))
        self.assertLess(abs(r["5.5"]["add"]), 1e-12)

    # ── 3. NBA ──────────────────────────────────────────────────────────────────────────
    def test_nba_sim_dispersion_matches_the_constants(self):
        r = self.pg.evaluate("""()=>{const g=(JSON.parse(JSON.stringify(window._remoteNBAGames||[]))[0])||null;
          const abbr=Object.keys(window.__CV_DATA.nba.teamAdv||{});const h=abbr[0],a=abbr[1];
          const line=226.5;const mc=nbaMC(h,a,300000,line);
          return{mc,line,sm:NBA_SIGMA_MARGIN,st:NBA_SIGMA_TOTAL,hwNormal:normalCDF(mc.spread/NBA_SIGMA_MARGIN),overNormal:1-normalCDF((line-mc.avgT)/NBA_SIGMA_TOTAL)}}""")
        self.assertEqual((r["sm"], r["st"]), (13.0, 18.5))
        self.assertAlmostEqual(r["mc"]["overP"], r["overNormal"], delta=0.012)
        self.assertAlmostEqual(r["mc"]["hwP"], r["hwNormal"], delta=0.02)      # ties are split, rounding to whole points adds ~1-2% ties

    def test_nba_spread_chip_uses_the_same_margin_sigma(self):
        html = (ROOT / "docs" / "app.html").read_text()
        self.assertIn("const _nbaSigma=NBA_SIGMA_MARGIN;", html)
        self.assertNotIn("const _nbaSigma=11.5;", html)

    def test_nba_means_unchanged_by_the_dispersion_fix(self):
        r = self.pg.evaluate("""()=>{const abbr=Object.keys(window.__CV_DATA.nba.teamAdv||{});const mc=nbaMC(abbr[2],abbr[3],200000,225.5);
          const adv=window.__CV_DATA.nba.teamAdv;return{avgT:mc.avgT,avgH:mc.avgH,avgA:mc.avgA}}""")
        self.assertTrue(180 < r["avgT"] < 260)
        self.assertAlmostEqual(r["avgT"], r["avgH"] + r["avgA"], delta=0.15)


if __name__ == "__main__":
    unittest.main(verbosity=2)
