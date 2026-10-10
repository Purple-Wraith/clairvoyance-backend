#!/usr/bin/env python3
"""Home > PERIOD PERFORMANCE — LEAGUE: each recent-form bubble shows the event + result on hover / keyboard focus / tap (owner request 2026-10-10), and the bubbles are bigger.

    python3 scripts/test_league_form_tip.py
"""
import datetime as dt, functools, http.server, re, socketserver, threading, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a, **k):
        pass


def rows(now):
    out = []
    for k in range(1, 12):
        lk = now - k * 3600000
        out.append({"id": f"t{k}", "sport": "NHL", "betType": "OU", "betOn": "OVER 5.5", "hA": "NYR", "awA": "BOS", "date": dt.datetime.fromtimestamp(lk / 1000).strftime("%Y-%m-%d"),
                    "lockedAt": lk, "outcome": "win" if k % 3 else "loss", "decOdds": 1.9, "winProb": .6, "hScore": 3, "aScore": 4})
    return out


class LeagueFormTip(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        socketserver.TCPServer.allow_reuse_address = True
        cls.srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=str(ROOT / "docs")))
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.pw = sync_playwright().start()
        cls.b = cls.pw.chromium.launch()

    @classmethod
    def tearDownClass(cls):
        cls.b.close(); cls.pw.stop(); cls.srv.shutdown()

    def page(self, width=1300):
        pg = self.b.new_page(viewport={"width": width, "height": 900})
        self.errors = []
        pg.on("pageerror", lambda e: self.errors.append(str(e)))
        pg.route(re.compile(r"https://fonts\.(googleapis|gstatic)\.com/.*"), lambda r: r.abort())
        pg.goto(f"http://127.0.0.1:{self.srv.server_address[1]}/app.html?nosb=1", timeout=90000)
        pg.wait_for_function("typeof saveP==='function'&&typeof renderHomePage==='function'", timeout=90000)
        pg.wait_for_timeout(500)
        now = pg.evaluate("Date.now()")
        pg.evaluate("(r)=>{window._homeLgPerfPeriod='all';saveP(r);renderHomePage()}", rows(now))
        pg.wait_for_selector(".lg-dot")
        return pg

    def test_hover_shows_event_and_result(self):
        pg = self.page()
        win = pg.evaluate("[...document.querySelectorAll('.lg-dot')].findIndex(d=>d.dataset.tip.includes('WIN'))")
        loss = pg.evaluate("[...document.querySelectorAll('.lg-dot')].findIndex(d=>d.dataset.tip.includes('LOSS'))")
        pg.locator(".lg-dot").nth(win).hover()
        tip = pg.inner_text("#dot-tip")
        for want in ("NHL", "Boston Bruins", "New York Rangers", "OVER 5.5", "BOS 4 – 3 NYR", "WIN"):
            self.assertIn(want, tip)
        pg.mouse.move(5, 5)
        self.assertEqual(pg.locator("#dot-tip").count(), 0)                    # hidden again on leave
        pg.locator(".lg-dot").nth(loss).hover()
        self.assertIn("LOSS", pg.inner_text("#dot-tip"))
        self.assertEqual(self.errors, [])
        pg.close()

    def test_keyboard_focus_shows_the_same_tip_and_bubbles_are_big(self):
        pg = self.page()
        pg.locator(".lg-dot").first.focus()
        self.assertIn("OVER 5.5", pg.inner_text("#dot-tip"))
        self.assertGreaterEqual(pg.evaluate("document.querySelector('.lg-dot').getBoundingClientRect().width"), 12)
        pg.close()
        pg = self.page(390)
        self.assertGreaterEqual(pg.evaluate("document.querySelector('.lg-dot').getBoundingClientRect().width"), 9)
        self.assertLessEqual(pg.evaluate("document.documentElement.scrollWidth"), 391)
        pg.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
