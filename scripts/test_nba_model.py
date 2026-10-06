#!/usr/bin/env python3
"""NBA model sanity (docs/app.html nbaEns / nbaMC / SPORT_CAL): pins the LEAGUE-WIDE averages across all 870 team pairings of the live ratings table, so a calibration or
units bug can't silently re-inflate the home side or the totals. Before the 2026-10-05 fixes: average home win probability 70.4% (real NBA ~58%), average projected total 240.4
(real ~230), because SPORT_CAL.NBA shifted every home probability up by +0.55 logit (fit on Finals props) and nbaMC multiplied per-100-possession ratings by pace/98.8 against a
113.5 'league average' while the real mean is ~115.7.

    python3 scripts/test_nba_model.py
"""
import functools, http.server, socketserver, threading, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a, **k):
        pass


class NbaModel(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        socketserver.TCPServer.allow_reuse_address = True
        cls.srv = socketserver.TCPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=str(ROOT / "docs")))
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch()
        cls.pg = cls.browser.new_page()
        cls.pg.goto(f"http://127.0.0.1:{cls.srv.server_address[1]}/app.html?nosb=1")
        cls.pg.wait_for_function("typeof nbaEns==='function'&&window.__CV_DATA&&window.__CV_DATA.nba&&window.__CV_DATA.nba.teamAdv&&Object.keys(window.__CV_DATA.nba.teamAdv).length>=30")
        cls.pg.wait_for_timeout(1500)
        cls.stats = cls.pg.evaluate("""()=>{
          const T=Object.keys(window.__CV_DATA.nba.teamAdv);let n=0,sp=0,st=0,sm=0,maxP=0,minP=1;
          for(const h of T)for(const a of T){if(h===a)continue;const e=nbaEns(h,a,null);if(!e.mcD)continue;
            n++;sp+=e.p;st+=e.mcD.avgT;sm+=e.mcD.avgH-e.mcD.avgA;maxP=Math.max(maxP,e.p);minP=Math.min(minP,e.p);}
          return {n,homeWinP:sp/n,total:st/n,margin:sm/n,maxP,minP,cal:SPORT_CAL.NBA}}""")

    @classmethod
    def tearDownClass(cls):
        cls.browser.close(); cls.pw.stop(); cls.srv.shutdown()

    def test_all_pairings_were_modelled(self):
        self.assertGreaterEqual(self.stats["n"], 800)

    def test_average_home_win_probability_is_realistic(self):
        self.assertGreater(self.stats["homeWinP"], 0.54)
        self.assertLess(self.stats["homeWinP"], 0.62, "NBA home teams win ~58% of games; a model averaging 70% invents PREMIUM home moneylines")

    def test_average_projected_total_matches_the_league(self):
        self.assertGreater(self.stats["total"], 225)
        self.assertLess(self.stats["total"], 235, "the league's real average total is ~230; 240 made nearly every game an OVER")

    def test_home_court_moves_the_margin_not_the_total(self):
        self.assertGreater(self.stats["margin"], 2.0)
        self.assertLess(self.stats["margin"], 4.0)

    def test_the_stale_home_inflating_calibration_is_gone(self):
        self.assertEqual(self.stats["cal"], {"B": 0})

    def test_model_is_held_within_the_market_cap(self):
        """With a posted moneyline the final home win % stays within 6.5 points of the no-vig market, however far the raw model is from it."""
        r = self.pg.evaluate("""()=>{const out=[];
          const T=Object.keys(window.__CV_DATA.nba.teamAdv);
          for(const [h,a] of [['MIN','MIL'],['LAL','DEN'],['BOS','NY'],['OKC','UTAH'],['UTAH','OKC']]){
            for(const [hl,al] of [[-110,-110],[-250,210],[180,-220],[+400,-550]]){
              const e=nbaEns(h,a,{hL:hl,aL:al});out.push({h,a,hl,al,p:e.p,mkt:e.mkt,capped:e.capped});}}
          return out}""")
        self.assertGreater(len(r), 10)
        for x in r:
            self.assertLessEqual(abs(x["p"] - x["mkt"]), 0.0651, x)
        self.assertTrue(any(x["capped"] for x in r), "at least one raw model probability should have been clamped")
        self.assertTrue(all(abs(x["mkt"] - 0.5) < 0.01 for x in r if x["hl"] == -110))                   # -110/-110 -> no-vig 50%

    def test_no_market_line_means_no_cap(self):
        r = self.pg.evaluate("()=>{const e=nbaEns('MIN','MIL',null);return {mkt:e.mkt,capped:e.capped}}")
        self.assertIsNone(r["mkt"])
        self.assertFalse(r["capped"])

    def test_home_only_line_uses_an_assumed_vig(self):
        r = self.pg.evaluate("()=>{const e=nbaEns('BOS','NY',{hL:-150});return {mkt:e.mkt}}")
        self.assertAlmostEqual(r["mkt"], (1 / 1.6667) / 1.045, places=2)

    def test_totals_symmetric_in_home_and_away(self):
        """Swapping home/away must not change the projected TOTAL (home court is a margin shift)."""
        r = self.pg.evaluate("""()=>{const a=nbaMC('BOS','NYK',40000,225.5),b=nbaMC('NYK','BOS',40000,225.5);return [a.avgT,b.avgT,a.avgH-a.avgA,b.avgH-b.avgA]}""")
        self.assertLess(abs(r[0] - r[1]), 0.8)


if __name__ == "__main__":
    unittest.main(verbosity=2)
