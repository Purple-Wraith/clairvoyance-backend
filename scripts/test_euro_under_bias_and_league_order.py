#!/usr/bin/env python3
"""2026-10-26 owner requests: (1) UNDER BIAS on the SHL / Extraliga / NLA / Liiga totals (books price overs too high at 5.5-6.5: the market share of the blend is moved toward the under) and (2) the home page
PERIOD PERFORMANCE — LEAGUE list is ordered by win % (highest first).

    python3 scripts/test_euro_under_bias_and_league_order.py
"""
import datetime as dt, functools, http.server, math, re, socketserver, threading, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a, **k):
        pass


class T(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        socketserver.TCPServer.allow_reuse_address = True
        cls.srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=str(ROOT / "docs")))
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.pw = sync_playwright().start()
        cls.b = cls.pw.chromium.launch()
        cls.pg = cls.b.new_page(viewport={"width": 1300, "height": 900})
        cls.pg.route(re.compile(r"https://fonts\.(googleapis|gstatic)\.com/.*"), lambda r: r.abort())
        for pat in ("**/*espn*/**", "**/*workers.dev*"):
            cls.pg.route(pat, lambda r: r.abort())
        cls.pg.goto(f"http://127.0.0.1:{cls.srv.server_address[1]}/app.html?nosb=1&nosw=1", timeout=90000)
        cls.pg.wait_for_function("typeof saveP==='function'&&typeof renderHomePage==='function'&&typeof _hkBlendOU==='function'", timeout=90000)
        cls.pg.wait_for_timeout(500)

    @classmethod
    def tearDownClass(cls):
        cls.b.close(); cls.pw.stop(); cls.srv.shutdown()

    def blend(self, line, lg, p_over=.5, mkt_over=.5):
        dec = 1 / (mkt_over * 1.05)
        dec_u = 1 / ((1 - mkt_over) * 1.05)
        return self.pg.evaluate("""([line,lg,p,o,u])=>{const g={state:'pre',odds:{at:new Date().toISOString(),ou:[{line:5.5,over:o,under:u,books:1},{line:6.5,over:o,under:u,books:1},{line:4.5,over:o,under:u,books:1}]}};
          const b=_hkBlendOU(p,g,line,20,lg);return b?{p:b.p,model:b.model,market:b.market,alpha:b.alpha,ub:b.underBias||0}:null}""", [line, lg, p_over, dec, dec_u])

    def test_each_league_moves_the_blend_toward_the_under(self):
        for lg, sh in (("SHL", .5), ("EXTRALIGA", .4), ("NLA", .3), ("LIIGA", .25)):
            b = self.blend(5.5, lg)
            self.assertEqual(b["ub"], sh, lg)
            base = self.blend(5.5, "NHL-like-none")["p"]
            self.assertAlmostEqual(base, .5, places=3)
            want = 1 / (1 + math.exp(b["alpha"] * sh))          # logit(.5) = 0, minus alpha x shift
            self.assertAlmostEqual(b["p"], want, places=4, msg=lg)
            self.assertLess(b["p"], .5, lg)

    def test_only_lines_5_5_to_6_5_and_only_with_a_market(self):
        self.assertEqual(self.blend(4.5, "SHL")["ub"], 0)
        self.assertAlmostEqual(self.blend(4.5, "SHL")["p"], .5, places=3)
        self.assertEqual(self.blend(6.5, "SHL")["ub"], .5)
        self.assertEqual(self.blend(5.5, None)["ub"], 0)
        self.assertEqual(self.pg.evaluate("_hkBlendOU(.5,{state:'pre'},5.5,20,'SHL')"), None)     # no odds -> no blend, nothing to correct

    def test_the_note_shows_on_the_card_text(self):
        t = self.pg.evaluate("""()=>{const g={state:'pre',odds:{at:new Date().toISOString(),ou:[{line:5.5,over:1.9,under:1.9,books:1}]}};const b=_hkBlendOU(.5,g,5.5,20,'SHL');return [_hkBlendText(b,'O/U'),_hkBlendText(_hkBlComp(b),'O/U')]}""")
        for s in t:
            self.assertIn("under-bias", s)

    def test_the_model_mc_itself_leans_under(self):
        """liigaMC / nlaMC / extraliga / shl: the exact-Poisson over probability is the plain Poisson tail minus the league shift (5.5-6.5 only)."""
        out = self.pg.evaluate("""()=>{
          const team={gp:12,gf:36,ga:36,gm:6.2},res={};
          const set={LIIGA:()=>{_LIIGA_DATA={teams:{a:team,b:team},games:[]}},NLA:()=>{_NLA_DATA={teams:{a:team,b:team},games:[]}},EXTRALIGA:()=>{_EXTRALIGA_DATA={teams:{a:team,b:team},games:[]}},SHL:()=>{_SHL_DATA={teams:{a:team,b:team},games:[]}}};
          const fn={LIIGA:liigaMC,NLA:nlaMC,EXTRALIGA:extraligaMC,SHL:shlMC};
          for(const lg of Object.keys(fn)){set[lg]();res[lg]={};
            for(const L of [4.5,5.5,6.5,7.5]){const mc=fn[lg]('a','b',L);let cdf=0;for(let k=0;k<=Math.floor(L);k++)cdf+=_socPoissonPmf(mc.avgT,k);
              res[lg][L]={got:mc.overP,plain:1-cdf};}}
          return res}""")
        shifts = {"SHL": .5, "EXTRALIGA": .4, "NLA": .3, "LIIGA": .25}
        for lg, rows in out.items():
            for line, r in rows.items():
                sh = shifts[lg] if 5.5 <= float(line) <= 6.5 else 0
                want = 1 / (1 + math.exp(-(math.log(r["plain"] / (1 - r["plain"])) - sh)))
                self.assertAlmostEqual(r["got"], want, places=6, msg=f"{lg} {line}")

    def test_league_list_is_ordered_by_win_pct(self):
        now = self.pg.evaluate("Date.now()")
        day = dt.datetime.fromtimestamp(now / 1000).strftime("%Y-%m-%d")
        plan = [("NHL", 8, 2), ("NBA", 3, 7), ("SHL", 6, 1), ("LIIGA", 5, 5), ("NFL", 9, 1), ("CFB", 1, 0), ("NLA", 12, 3)]   # NLA has the most picks, NFL / CFB the best rates
        rows, k = [], 0
        for sport, w, l in plan:
            for i in range(w + l):
                k += 1
                rows.append({"id": f"o{k}", "sport": sport, "league": sport, "betType": "ML", "betOn": f"{sport}H", "hA": f"{sport}H", "awA": f"{sport}A", "date": day, "lockedAt": now - 1000 * k,
                             "outcome": "win" if i < w else "loss", "decOdds": 1.9, "winProb": .6, "hScore": 3, "aScore": 2})
        self.pg.evaluate("(r)=>{window._homeLgPerfPeriod='all';saveP(r);renderHomePage()}", rows)
        self.pg.wait_for_function("document.body.innerText.includes('PERIOD PERFORMANCE')")
        labels = self.pg.evaluate("""()=>{const box=[...document.querySelectorAll('div')].find(d=>d.textContent.trim()==='// PERIOD PERFORMANCE — LEAGUE');
          return [...box.parentElement.querySelectorAll('.card > div')].map(r=>r.firstElementChild.textContent.trim())}""")
        self.assertEqual(labels, ["CFB", "NFL", "SHL", "NLA", "NHL", "LIIGA", "NBA"], labels)       # 100%, 90%, 86%, 80%, 80% (NLA has more picks), 50%, 30%


if __name__ == "__main__":
    unittest.main()
