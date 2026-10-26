#!/usr/bin/env python3
"""Top tab bar (docs/app.html #sbar) on desktop (owner 2026-10-25: "reduce text size of tabs so they all fit" -> "go with two row"): the 13 tabs sit on TWO rows -- HOME / SCORES / TOP PICKS / OVERALL / ANALYTICS / PROFIT, then BASKETBALL / FOOTBALL /
HOCKEY / SOCCER / SIMULATOR / NEWS / MAILING LIST -- fully on screen at every desktop width (no sideways scroll, nothing clipped); phones / touch keep the single horizontally scrolling row.

    python3 scripts/test_nav_layout.py
"""
import functools, http.server, os, socketserver, threading, unittest
from pathlib import Path

ROOT = Path(os.environ.get("CV_ROOT") or Path(__file__).resolve().parent.parent)
ROW1 = ["HOME", "SCORES", "TOP PICKS", "OVERALL", "ANALYTICS", "PROFIT"]
ROW2 = ["BASKETBALL", "FOOTBALL", "HOCKEY", "SOCCER", "SIMULATOR", "NEWS", "MAILING LIST"]
MEASURE = """()=>{const n=document.getElementById('sbar'),vw=document.documentElement.clientWidth;
  const bs=[...n.querySelectorAll('.sp')].map(b=>{const r=b.getBoundingClientRect();return{t:b.innerText.trim(),top:Math.round(r.top),l:Math.round(r.left),r:Math.round(r.right),h:Math.round(r.height)}});
  return{vw,sw:n.scrollWidth,cw:n.clientWidth,bs,sbr:getComputedStyle(n.querySelector('.sbr')).display,font:getComputedStyle(n.querySelector('.sp')).fontSize,navBottom:Math.round(n.getBoundingClientRect().bottom)}}"""


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a, **k):
        pass


class Nav(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        socketserver.TCPServer.allow_reuse_address = True
        cls.srv = socketserver.TCPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=str(ROOT / "docs")))
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.pw = sync_playwright().start(); cls.b = cls.pw.chromium.launch(); cls.errs = []

    @classmethod
    def tearDownClass(cls):
        cls.b.close(); cls.pw.stop(); cls.srv.shutdown()

    def measure(self, w, mobile=False):
        ctx = self.b.new_context(viewport={"width": w, "height": 800}, is_mobile=mobile, has_touch=mobile)
        pg = ctx.new_page(); pg.on("pageerror", lambda e: self.errs.append(str(e)))
        pg.route("**/*espn.com/**", lambda r: r.abort()); pg.route("**/*workers.dev/**", lambda r: r.abort())
        pg.goto(f"http://127.0.0.1:{self.srv.server_address[1]}/app.html?nosb=1&nosw=1"); pg.wait_for_function("typeof SS==='function'"); pg.wait_for_timeout(500)
        r = pg.evaluate(MEASURE); ctx.close(); return r

    def test_two_rows_and_everything_on_screen_at_every_desktop_width(self):
        for w in (1024, 1100, 1280, 1366, 1440, 1536, 1920, 2560):
            r = self.measure(w)
            self.assertEqual([b["t"] for b in r["bs"]], ROW1 + ROW2, w)                                  # order unchanged
            tops = sorted({b["top"] for b in r["bs"]})
            self.assertEqual(len(tops), 2, (w, tops))                                                    # exactly two rows
            self.assertEqual([b["t"] for b in r["bs"] if b["top"] == tops[0]], ROW1, w)
            self.assertEqual([b["t"] for b in r["bs"] if b["top"] == tops[1]], ROW2, w)
            self.assertTrue(all(b["l"] >= 0 and b["r"] <= r["vw"] for b in r["bs"]), (w, [(b["t"], b["l"], b["r"]) for b in r["bs"] if b["l"] < 0 or b["r"] > r["vw"]]))
            self.assertLessEqual(r["sw"], r["cw"], w)                                                    # no sideways scroll

    def test_text_is_readable_and_never_above_the_old_size(self):
        sizes = {w: float(self.measure(w)["font"][:-2]) for w in (1024, 1440, 2560)}
        self.assertGreaterEqual(sizes[1024], 10.9); self.assertLessEqual(sizes[2560], 15.01)
        self.assertGreater(sizes[1440], sizes[1024])

    def test_phones_keep_one_scrolling_row(self):
        for w in (390, 320):
            r = self.measure(w, mobile=True)
            self.assertEqual(r["sbr"], "none", w)
            self.assertEqual(len({b["top"] for b in r["bs"]}), 1, w)                                     # one row
            self.assertGreater(r["sw"], r["cw"], w)                                                       # it scrolls sideways, as before

    def test_no_script_errors(self):
        self.assertEqual(self.errs, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
