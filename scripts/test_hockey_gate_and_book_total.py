#!/usr/bin/env python3
"""Hockey changes of 2026-10-29 (docs/app.html):
  * the euro UNDER BIAS is gated: skipped when the projected total (book basis) is at/above the line, or the market itself leans OVER (no-vig P(over) above 50%); a balanced market keeps it; today's already
    locked picks are untouched because nothing in the lock engine removes a pending pick;
  * the projected hockey total is on the BOOK basis (regulation + the overtime goal): avgT = avgTReg + share x P(tie), avgH + avgA = avgTReg.

    python3 scripts/test_hockey_gate_and_book_total.py
"""
import functools, http.server, socketserver, threading, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a, **k):
        pass


class Gate(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        socketserver.TCPServer.allow_reuse_address = True
        cls.srv = socketserver.TCPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=str(ROOT / "docs")))
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.pw = sync_playwright().start(); cls.b = cls.pw.chromium.launch()
        cls.pg = cls.b.new_page()
        cls.pg.goto(f"http://127.0.0.1:{cls.srv.server_address[1]}/app.html?nosb=1&nosw=1")
        cls.pg.wait_for_function("typeof _hkBlendOU==='function'&&typeof shlMC==='function'&&typeof nhlMC==='function'"); cls.pg.wait_for_timeout(3000)

    @classmethod
    def tearDownClass(cls):
        cls.b.close(); cls.pw.stop(); cls.srv.shutdown()

    def blend(self, mkt_over, mean_t=None, p_model=0.40, p_raw=0.46, lg="SHL", line=5.5):
        """_hkBlendOU with a one-line ladder whose no-vig P(over) is mkt_over."""
        dec_o, dec_u = 1 / (mkt_over * 1.05), 1 / ((1 - mkt_over) * 1.05)
        return self.pg.evaluate("""([o,u,pm,pr,mt,lg,line])=>{const g={state:'pre',odds:{at:new Date().toISOString(),ou:[{line,over:o,under:u,books:1}]}};
          const b=_hkBlendOU(pm,g,line,20,lg,mt,pr);return b?{p:b.p,model:b.model,ub:b.underBias||0}:null}""", [dec_o, dec_u, p_model, p_raw, mean_t, lg, line])

    def test_shift_stays_on_when_the_market_leans_under_and_the_mean_is_below_the_line(self):
        b = self.blend(.42, mean_t=5.2)
        self.assertEqual(b["ub"], .5); self.assertAlmostEqual(b["model"], .40, places=6)        # the shifted model probability is blended

    def test_off_when_the_projected_total_is_at_or_above_the_line(self):
        for mt in (5.5, 5.8):
            b = self.blend(.42, mean_t=mt)
            self.assertEqual(b["ub"], 0, mt); self.assertAlmostEqual(b["model"], .46, places=6)     # the UNSHIFTED model probability
        self.assertEqual(self.blend(.42, mean_t=5.49)["ub"], .5)

    def test_off_when_the_market_leans_over(self):
        b = self.blend(.55, mean_t=5.2)
        self.assertEqual(b["ub"], 0); self.assertAlmostEqual(b["model"], .46, places=6)
        self.assertEqual(self.blend(.50, mean_t=5.2)["ub"], .5)                                  # a balanced market keeps the shift

    def test_without_engine_inputs_it_only_checks_the_market(self):
        self.assertEqual(self.blend(.42, mean_t=None)["ub"], .5)
        self.assertEqual(self.blend(.58, mean_t=None)["ub"], 0)

    def test_gate_moves_the_blended_probability_toward_the_over(self):
        on = self.blend(.45, mean_t=5.2)["p"]; off = self.blend(.45, mean_t=5.8, p_model=.40)["p"]
        self.assertGreater(off, on)

    def test_engine_overp_is_unshifted_when_the_mean_reaches_the_line(self):
        r = self.pg.evaluate("""()=>{const out={};
          const mk=g=>({gp:12,gf:g*12,ga:g*12,gm:g*2});   // g = goals per team per game
          for(const [nm,gm,line] of [['high',3.1,5.5],['low',2.4,5.5]]){_SHL_DATA={teams:{a:mk(gm),b:mk(gm)},games:[]};const mc=shlMC('a','b',line);out[nm]={got:mc.overP,raw:mc.overPRaw,t:mc.avgT}}
          return out}""")
        self.assertGreaterEqual(r["high"]["t"], 5.5); self.assertEqual(r["high"]["got"], r["high"]["raw"])      # gated: no shift
        self.assertLess(r["low"]["t"], 5.5); self.assertLess(r["low"]["got"], r["low"]["raw"])                  # shifted toward the under

    def test_total_is_on_the_book_basis(self):
        r = self.pg.evaluate("""()=>{const g=(_NHL_SCHEDULE_DATA.games||[]).find(x=>x.state==='post');const n=nhlMC(g.home,g.away,25000,6.5);
          const mk={gp:12,gf:36,ga:36,gm:6.2};_SHL_DATA={teams:{a:mk,b:mk},games:[]};const s=shlMC('a','b',5.5);
          return{n:{t:n.avgT,reg:n.avgTReg,h:n.avgH,a:n.avgA,ot:n.otP,share:_hkOtGoalShare('NHL')},s:{t:s.avgT,reg:s.avgTReg,h:s.avgH,a:s.avgA,ot:s.otP,share:_hkOtGoalShare('SHL')}}}""")
        for k in ("n", "s"):
            x = r[k]
            self.assertAlmostEqual(x["reg"], x["h"] + x["a"], places=9)
            self.assertAlmostEqual(x["t"], x["reg"] + x["share"] * x["ot"], places=9)
            self.assertGreater(x["t"] - x["reg"], 0.05); self.assertLess(x["t"] - x["reg"], 0.15)          # the ~+0.1 goals the audit estimated

    def test_locked_picks_are_never_removed_by_a_requalification(self):
        src = (ROOT / "scripts" / "auto_lock_settle.py").read_text()
        for bad in ("outcome'] = '_removed'", 'outcome"] = "_removed"', "unlock_pick", "def remove_unqualified"):
            self.assertNotIn(bad, src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
