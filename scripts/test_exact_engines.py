#!/usr/bin/env python3
"""Exact MC engines (docs/app.html, 2026-10-29): nhlMC, cfbMC, nflMC, nbaMC, _soccerMC and propMC compute their probabilities in closed form (Poisson goal grid / normal distribution) instead of
sampling.  Checks: deterministic (identical on repeat), probabilities normalise, they agree with an in-page sampler of the same model within sampling error, they are fast, and the card wording no
longer claims simulations.

    python3 scripts/test_exact_engines.py
"""
import functools, http.server, socketserver, threading, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a, **k):
        pass


class Exact(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        socketserver.TCPServer.allow_reuse_address = True
        cls.srv = socketserver.TCPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=str(ROOT / "docs")))
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.pw = sync_playwright().start(); cls.b = cls.pw.chromium.launch()
        cls.pg = cls.b.new_page()
        cls.errors = []; cls.pg.on("pageerror", lambda e: cls.errors.append(str(e)))
        cls.pg.goto(f"http://127.0.0.1:{cls.srv.server_address[1]}/app.html?nosb=1&nosw=1")
        cls.pg.wait_for_function("typeof nhlMC==='function'&&typeof cfbMC==='function'&&typeof nbaMC==='function'&&typeof _soccerMC==='function'"); cls.pg.wait_for_timeout(3500)

    @classmethod
    def tearDownClass(cls):
        cls.b.close(); cls.pw.stop(); cls.srv.shutdown()

    GAMES = """()=>{const nhl=(_NHL_SCHEDULE_DATA.games||[]).find(x=>x.state==='post');
      const cfb=Object.values(_CFB_DATA.weeks).flat().find(x=>x.state==='post'&&_CFB_DATA.stats[x.home]&&_CFB_DATA.stats[x.away]);
      const nfl=Object.values(_NFL_DATA.weeks).flat().find(x=>x.state==='post'&&x.seasonType!==1);
      const ab=Object.keys(window.__CV_DATA.nba.teamAdv||{});return{nhl,cfb,nfl,ab}}"""

    def test_deterministic_and_flagged_exact(self):
        r = self.pg.evaluate("""()=>{const G=(%s)();const out={};
          const runs={nhl:()=>nhlMC(G.nhl.home,G.nhl.away,25000,6.5),cfb:()=>cfbMC(G.cfb.home,G.cfb.away,25000,52.5,{neutralSite:false}),nfl:()=>nflMC(G.nfl.home,G.nfl.away,25000,45.5,G.nfl),
                      nba:()=>nbaMC(G.ab[0],G.ab[1],25000,226.5),soc:()=>_soccerMC(1.6,1.2,25000),prop:()=>propMC(22,6,19.5,5000)};
          for(const k in runs){const a=JSON.stringify(runs[k](),(x,v)=>(typeof v==='object'&&v&&('temp' in v||'snow' in v))?undefined:v),b=JSON.stringify(runs[k](),(x,v)=>(typeof v==='object'&&v&&('temp' in v||'snow' in v))?undefined:v);
            out[k]={same:a===b,exact:runs[k]().exact===true}}
          return out}""" % self.GAMES)
        for k, v in r.items():
            self.assertTrue(v["same"], k); self.assertTrue(v["exact"], k)

    def test_probabilities_normalise(self):
        r = self.pg.evaluate("""()=>{const G=(%s)();const n=nhlMC(G.nhl.home,G.nhl.away,25000,6.5),s=_soccerMC(1.6,1.2,25000);
          return{nhl:n.hwP+n.awP,nhlG:n.gpConf,soc:s.hWin+s.draw+s.aWin,over:[n.overP,s.over25,s.over35,s.btts]}}""" % self.GAMES)
        self.assertAlmostEqual(r["nhl"], 1, places=12)
        self.assertAlmostEqual(r["soc"], 1, places=9)
        self.assertTrue(all(0 < x < 1 for x in r["over"]))

    def test_agree_with_a_sampler_of_the_same_model(self):
        r = self.pg.evaluate("""()=>{
          const pois=l=>{const L=Math.exp(-l);let k=0,p=1;do{k++;p*=Math.random()}while(p>L);return k-1};
          const N=400000,hx=1.6,ax=1.2;let hW=0,d=0,o25=0,bt=0;for(let i=0;i<N;i++){const h=pois(hx),a=pois(ax);if(h>a)hW++;else if(h===a)d++;if(h+a>2.5)o25++;if(h>0&&a>0)bt++}
          const e=_soccerMC(hx,ax,10,0);   // rho 0: the sampler is plain independent Poisson (the live default adds the Dixon-Coles draw adjustment)
          const sd=(p)=>Math.sqrt(p*(1-p)/N);
          return{z:[(e.hWin-hW/N)/sd(hW/N),(e.draw-d/N)/sd(d/N),(e.over25-o25/N)/sd(o25/N),(e.btts-bt/N)/sd(bt/N)]}}""")
        for z in r["z"]:
            self.assertLess(abs(z), 4.5)

    def test_nhl_agrees_with_its_old_sampler(self):
        r = self.pg.evaluate("""()=>{const g=(_NHL_SCHEDULE_DATA.games||[]).find(x=>x.state==='post');const m=nhlMC(g.home,g.away,10,6.5);
          const hL=m.avgH,aL=m.avgA,N=400000,share=_hkOtGoalShare('NHL');let hw=0,ov=0,rl=0;
          const pois=l=>{const L=Math.exp(-l);let k=0,p=1;do{k++;p*=Math.random()}while(p>L);return k-1};
          for(let i=0;i<N;i++){const h=pois(hL),a=pois(aL),t=h+a;if(t>6.5)ov++;if(h>a){hw++;if(h-a>=2)rl++}else if(h===a){hw+=.5;if(t<=6.5&&t+1>6.5)ov+=share}}
          return{conf:m.gpConf,d:[Math.abs(m.hwP-hw/N),Math.abs(m.overP-ov/N)]}}""")
        self.assertEqual(r["conf"], 1)
        self.assertLess(max(r["d"]), 0.004)

    def test_fast(self):
        ms = self.pg.evaluate("""()=>{const g=(_NHL_SCHEDULE_DATA.games||[]).find(x=>x.state==='post');nhlMC(g.home,g.away,25000,6.5);
          const t=performance.now();for(let i=0;i<300;i++)nhlMC(g.home,g.away,25000,6.5);return (performance.now()-t)/300}""")
        self.assertLess(ms, 0.5)          # was ~1.5-2 ms per call with 25k samples

    def test_reasoning_text_no_longer_claims_simulations(self):
        r = self.pg.evaluate("""()=>{const G=(%s)();
          const n=nhlMC(G.nhl.home,G.nhl.away,25000,6.5);const cf=cfbMC(G.cfb.home,G.cfb.away,25000,52.5,{neutralSite:false});
          return[_mcLineScore(n,'over',6.5,{n:25000}),_mcLineMargin(cf,'over','A','B',3.5,.5,.5,52.5,2),_mcLineSoccer(_soccerMC(1.4,1.1),'over',2.5),_mcProjBarHTML(cf,'52.5','H','A',{dec:1})]}""" % self.GAMES)
        txt = " ".join(r)
        self.assertNotIn("25k", txt); self.assertNotIn("-sim Monte Carlo", txt); self.assertNotIn("simulated outcomes", txt)
        self.assertIn("exact Poisson distribution", r[0]); self.assertIn("exact normal distribution", r[1]); self.assertIn("exact Poisson distribution", r[2])

    def test_no_page_errors(self):
        self.assertEqual(self.errors, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
