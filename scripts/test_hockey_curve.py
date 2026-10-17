#!/usr/bin/env python3
"""European hockey (SHL/Liiga/NLA/Extraliga) blends last season with this one on the NHL's curve (owner, 2026-10-17): this season's share is 10/30/60/80/100% at 2/5/10/15/20 games."""
import functools, http.server, threading, unittest
from pathlib import Path
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent


class Curve(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        H = type("Q", (http.server.SimpleHTTPRequestHandler,), {"log_message": lambda *a: None})
        cls.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(H, directory=str(ROOT / "docs")))
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.pw = sync_playwright().start(); cls.b = cls.pw.chromium.launch(); cls.pg = cls.b.new_page()
        cls.pg.route("**/*", lambda r: r.continue_() if r.request.url.startswith("http://127.0.0.1") else r.abort())
        cls.pg.goto(f"http://127.0.0.1:{cls.srv.server_address[1]}/app.html?nosb=1", wait_until="domcontentloaded")
        cls.pg.wait_for_function("typeof _hkThisW==='function'&&typeof _liigaBlendedRates==='function'&&typeof _shlBlendedRates==='function'&&typeof _nlaBlendedRates==='function'&&typeof _extraligaBlendedRates==='function'", timeout=90000)

    @classmethod
    def tearDownClass(cls):
        cls.b.close(); cls.pw.stop(); cls.srv.shutdown()

    def test_curve_points_and_lines_between_them(self):
        w = lambda g: self.pg.evaluate("g=>_hkThisW(g)", g)
        for g, want in [(0, 0), (2, .1), (5, .3), (10, .6), (15, .8), (20, 1), (30, 1), (1, .05), (3.5, .2), (12.5, .7)]:
            self.assertAlmostEqual(w(g), want, places=6, msg=g)
        self.assertEqual(self.pg.evaluate("[_hkThisW(null),_hkThisW('x'),_hkThisW(-3)]"), [0, 0, 0])

    def test_matches_the_nhl_curve_in_python(self):
        import sys; sys.path.insert(0, str(ROOT / "scripts"))
        try:
            import clairvoyance_update as C
        except Exception:
            self.skipTest("bs4/lxml not installed in this interpreter")
        for g in (0, 1, 2, 3, 5, 7, 10, 12, 15, 17, 20, 25):
            self.assertAlmostEqual(self.pg.evaluate("g=>_hkThisW(g)", g), 1 - C._nhl_prior_weight(g), places=3, msg=g)

    def test_all_four_leagues_blend_on_it(self):
        # current 3.0 GF/GA per game over 10 games, last season 4.0 over 60 -> at 10 games: 60% this season = 3.4
        for fn in ("_liigaBlendedRates", "_shlBlendedRates", "_nlaBlendedRates", "_extraligaBlendedRates"):
            r = self.pg.evaluate("f=>{const r=window[f]||eval(f);return r({gp:10,gf:30,ga:30,prevSeason:{gp:60,gf:240,ga:240}})}", fn)
            self.assertAlmostEqual(r["gf"], 3.4, places=6, msg=fn); self.assertAlmostEqual(r["ga"], 3.4, places=6, msg=fn)
            r = self.pg.evaluate("f=>eval(f)({gp:20,gf:60,ga:60,prevSeason:{gp:60,gf:240,ga:240}})", fn)
            self.assertAlmostEqual(r["gf"], 3.0, places=6, msg=fn)                                            # 20 games: this season only


if __name__ == "__main__":
    unittest.main(verbosity=2)
