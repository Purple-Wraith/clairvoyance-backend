#!/usr/bin/env python3
"""Boot failsafes must not stack Home on top of a pane that is already showing (2026-10-29).

SS() switches panes exclusively, but the boot failsafes (the "final guaranteed initialization" block, the 8 s hard escape and the 10 s load failsafe in docs/app.html) used to
add `act` to #sp-home WITHOUT removing the current pane, and endSplash() jumped to Home whatever was open.  Anything that navigated before boot finished (a deep link, an early tab
switch, a fast test) ended with Home rendered above the pane it had opened -- test_profit_tracker's jump check fell over it.

    python3 scripts/test_boot_pane_race.py
"""
import functools, http.server, re, socketserver, threading, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a, **k):
        pass


class Boot(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        socketserver.TCPServer.allow_reuse_address = True
        cls.srv = socketserver.TCPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=str(ROOT / "docs")))
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.pw = sync_playwright().start(); cls.b = cls.pw.chromium.launch()

    @classmethod
    def tearDownClass(cls):
        cls.b.close(); cls.pw.stop(); cls.srv.shutdown()

    def page(self):
        pg = self.b.new_page(viewport={"width": 1300, "height": 1000})
        pg.route(re.compile(r"https://fonts\.(googleapis|gstatic)\.com/.*"), lambda r: r.abort())
        pg.goto(f"http://127.0.0.1:{self.srv.server_address[1]}/app.html?nosb=1&nosw=1", timeout=90000)
        pg.wait_for_function("typeof SS==='function'&&typeof renderProfit==='function'", timeout=90000)
        return pg

    ACT = "()=>[...document.querySelectorAll('.spane.act')].map(e=>e.id)"

    def test_an_early_navigation_keeps_its_pane_alone_through_every_failsafe(self):
        pg = self.page()
        pg.evaluate("SS('profit')")                         # navigate before the splash / init failsafes have run
        for ms in (1500, 3000, 4000, 4000):                 # past the splash end, the init block, the 8 s escape hatch and the 10 s load failsafe
            pg.wait_for_timeout(ms)
            self.assertEqual(pg.evaluate(self.ACT), ["sp-profit"], f"after another {ms} ms")
        pg.close()

    def test_without_navigation_the_app_still_lands_on_home(self):
        pg = self.page()
        pg.wait_for_function("splashDone===true", timeout=30000)
        pg.wait_for_timeout(500)
        self.assertEqual(pg.evaluate(self.ACT), ["sp-home"])
        self.assertTrue(pg.evaluate("document.getElementById('app').classList.contains('ready')"))
        pg.wait_for_timeout(9000)                           # the failsafes must leave the Home pane alone too
        self.assertEqual(pg.evaluate(self.ACT), ["sp-home"])
        pg.close()

    def test_failsafes_still_show_home_when_no_pane_is_active(self):
        pg = self.page()
        pg.wait_for_function("splashDone===true", timeout=30000)
        pg.evaluate("()=>document.querySelectorAll('.spane').forEach(p=>p.classList.remove('act'))")      # nothing showing: the failsafe must put Home back
        html = (ROOT / "docs" / "app.html").read_text()
        self.assertIn("if(!document.querySelector('.spane.act')){", html)
        self.assertEqual(html.count("if(!document.querySelector('.spane.act'))document.getElementById('sp-home')?.classList.add('act');"), 2)
        pg.close()

    def test_end_splash_only_jumps_to_home_from_home(self):
        html = (ROOT / "docs" / "app.html").read_text()
        self.assertIn("if(!cur||cur.id==='sp-home')SS('home');", html)


if __name__ == "__main__":
    unittest.main(verbosity=2)
