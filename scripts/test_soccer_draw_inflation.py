#!/usr/bin/env python3
"""Soccer draw inflation (Dixon-Coles low-score correction, SOC_DC_RHO = -0.05, docs/app.html 2026-10-29).  The correction keeps total probability and every total of 2+ goals, lifts draws by
2|rho| lh la e^-(lh+la) and BTTS by |rho| lh la e^-(lh+la); the soccer AH uses it; the hockey engines (which share the grid helpers) do not.

    python3 scripts/test_soccer_draw_inflation.py
"""
import functools, http.server, math, socketserver, threading, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a, **k):
        pass


class Dc(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        socketserver.TCPServer.allow_reuse_address = True
        cls.srv = socketserver.TCPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=str(ROOT / "docs")))
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.pw = sync_playwright().start(); cls.b = cls.pw.chromium.launch()
        cls.pg = cls.b.new_page()
        cls.pg.goto(f"http://127.0.0.1:{cls.srv.server_address[1]}/app.html?nosb=1&nosw=1")
        cls.pg.wait_for_function("typeof _soccerMC==='function'&&typeof _socSpreadProb==='function'&&typeof SOC_DC_RHO!=='undefined'"); cls.pg.wait_for_timeout(2500)

    @classmethod
    def tearDownClass(cls):
        cls.b.close(); cls.pw.stop(); cls.srv.shutdown()

    def mc(self, h, a, rho=None):
        return self.pg.evaluate("([h,a,r])=>_soccerMC(h,a,0,r)", [h, a, rho])

    def test_constant_and_default(self):
        self.assertEqual(self.pg.evaluate("SOC_DC_RHO"), -0.05)
        self.assertEqual(self.mc(1.5, 1.2)["dcRho"], -0.05)
        self.assertEqual(self.mc(1.5, 1.2, 0)["dcRho"], 0)

    def test_draws_up_by_the_closed_form_amount_and_probability_conserved(self):
        for h, a in ((1.5, 1.2), (0.9, 2.1), (2.0, 2.0), (1.1, 1.0)):
            base, dc = self.mc(h, a, 0), self.mc(h, a)
            self.assertAlmostEqual(dc["draw"] - base["draw"], 2 * 0.05 * h * a * math.exp(-(h + a)), places=9)
            self.assertAlmostEqual(dc["hWin"] + dc["draw"] + dc["aWin"], 1, places=9)
            self.assertAlmostEqual(dc["hWin"] + dc["aWin"] - (base["hWin"] + base["aWin"]), -(dc["draw"] - base["draw"]), places=9)

    def test_over_2_5_and_3_5_do_not_move_but_btts_does(self):
        for h, a in ((1.5, 1.2), (0.9, 2.1)):
            base, dc = self.mc(h, a, 0), self.mc(h, a)
            self.assertAlmostEqual(dc["over25"], base["over25"], places=12)
            self.assertAlmostEqual(dc["over35"], base["over35"], places=12)
            self.assertAlmostEqual(dc["btts"] - base["btts"], 0.05 * h * a * math.exp(-(h + a)), places=9)

    def test_rho_zero_is_independent_poisson(self):
        r = self.mc(1.5, 1.2, 0)
        pm = lambda l, k: math.exp(-l + k * math.log(l) - math.lgamma(k + 1)) if l > 0 else float(k == 0)
        want = sum(pm(1.5, i) * pm(1.2, i) for i in range(40))
        self.assertAlmostEqual(r["draw"], want, places=9)
        self.assertAlmostEqual(r["btts"], (1 - math.exp(-1.5)) * (1 - math.exp(-1.2)), places=12)

    def test_goal_spread_uses_it_for_soccer_only(self):
        r = self.pg.evaluate("""()=>({dc:_socSpreadProb(1.4,1.1,-0.5,SOC_DC_RHO),ind:_socSpreadProb(1.4,1.1,-0.5,0),dflt:_socSpreadProb(1.4,1.1,-0.5),
          hk:_socSpreadProb(3.1,2.9,-1.5),hk0:_socSpreadProb(3.1,2.9,-1.5,0),dist:_socMarginDist(1.4,1.1,SOC_DC_RHO)})""")
        self.assertLess(r["dc"], r["ind"])                                        # the home side must win outright to cover -0.5: draws are up, so it falls
        self.assertEqual(r["dflt"], r["ind"])                                      # callers that pass no rho (the hockey engines) are untouched
        self.assertEqual(r["hk"], r["hk0"])
        self.assertAlmostEqual(sum(r["dist"].values()), 1, places=9)
        html = (ROOT / "docs" / "app.html").read_text()
        self.assertIn("_socSpreadProb(hxg,axg,ahHomeLine,SOC_DC_RHO)", html)

    def test_reasoning_text_names_the_adjustment(self):
        t = self.pg.evaluate("()=>_mcLineSoccer(_soccerMC(1.4,1.1),'over',2.5)")
        self.assertIn("Dixon-Coles draw adjustment", t)

    def test_pooled_league_draw_rate_is_matched_better(self):
        pm = lambda l, k: math.exp(-l + k * math.log(l) - math.lgamma(k + 1))
        # league-average pairing of this season's standings: 3.1 goals a match, 55/45 home split -> ~23% draws independent, ~24% with rho -0.05 against 24.4% observed
        base, dc = self.mc(1.7, 1.4, 0)["draw"], self.mc(1.7, 1.4)["draw"]
        self.assertLess(abs(dc - 0.244), abs(base - 0.244))


if __name__ == "__main__":
    unittest.main(verbosity=2)
