#!/usr/bin/env python3
"""Grades and colours on the game cards are the same in every sport / league (owner, 2026-10-08): ONE name set (PREMIUM / OPTIMAL / LEAN / SKIP / HIGH PROB) and ONE hue per grade --
PREMIUM gold (--gc), OPTIMAL magenta (--pc), LEAN cyan (--nc), SKIP red-pink (--hc), HIGH PROB green -- for the pill tags AND for any text that names a grade.

    python3 scripts/test_grade_uniform.py
"""
import functools, http.server, re, socketserver, threading, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = (ROOT / "docs" / "app.html").read_text()


class Source(unittest.TestCase):
    def test_no_abbreviated_grade_names_in_scan_lines(self):
        self.assertFalse("['SKIP','LEAN','OPT','PREM']" in SRC)

    def test_no_old_grade_colour_maps_remain(self):
        # (player-prop / player-rating maps keep their own GOOD/FAIR/FADE vocabulary and are not game-card grades)
        for bad in ("LEAN:'var(--ic)'", "{PREMIUM:'var(--vc)',OPTIMAL:'var(--nc)',LEAN", "m.tierN===2?'var(--nc)'", "tier==='OPTIMAL'?'var(--nc)'",
                    "pk.grade==='OPTIMAL'?'#00f0ff'", "return{g:'OPTIMAL',c:'var(--nc)'}", "return{g:'LEAN',c:'var(--ic)'}"):
            self.assertFalse(bad in SRC, bad)

    def test_static_key_legends_use_the_scheme(self):
        for bad in ('color:var(--vc);font-weight:700">PREMIUM', 'color:var(--ic);font-weight:700">LEAN', 'color:var(--nc);font-weight:700">OPTIMAL'):
            self.assertFalse(bad in SRC, bad)


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a, **k):
        pass


class Browser(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        socketserver.TCPServer.allow_reuse_address = True
        cls.srv = socketserver.TCPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=str(ROOT / "docs")))
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.pw = sync_playwright().start()
        cls.b = cls.pw.chromium.launch()
        cls.pg = cls.b.new_page()
        cls.pg.goto(f"http://127.0.0.1:{cls.srv.server_address[1]}/app.html?nosb=1")
        cls.pg.wait_for_function("typeof _gradeColVar==='function'")
        cls.pg.wait_for_timeout(800)

    @classmethod
    def tearDownClass(cls):
        cls.b.close(); cls.pw.stop(); cls.srv.shutdown()

    def test_text_colours_match_the_pill_backgrounds(self):
        r = self.pg.evaluate("""()=>{
          const probe=(cls)=>{const e=document.createElement('span');e.className='pill '+cls;document.body.appendChild(e);const bg=getComputedStyle(e).backgroundColor;e.remove();return bg;};
          const col=(v)=>{const e=document.createElement('span');e.style.color=v;document.body.appendChild(e);const c=getComputedStyle(e).color;e.remove();return c;};
          return {pp:[probe('pp'),col(_gradeColVar('OPTIMAL'))],pn:[probe('pn'),col(_gradeColVar('LEAN'))],ph:[probe('ph'),col(_gradeColVar('SKIP'))],
                  pq:[probe('pq'),col(_gradeColVar(HOCKEY_LANE_LABEL))]}}""")
        for k, (pill, text) in r.items():
            self.assertEqual(pill, text, k)

    def test_names(self):
        r = self.pg.evaluate("[0,1,2,3].map(t=>_gradeName({tierN:t})).concat([_gradeName({tierN:1,hkLane:true}),_gradeName({tierN:2,hkLane:true}),_gradeName({tierN:3})])")
        self.assertEqual(r, ["SKIP", "LEAN", "OPTIMAL", "PREMIUM", "HIGH PROB", "OPTIMAL", "PREMIUM"])

    def test_box_tags_name_the_side_and_the_line(self):
        h = self.pg.evaluate("""()=>_boxGradeHTML({mkts:[{side:'over',label:'OVER 5.5',tierN:1},{side:'under',label:'UNDER 5.5',tierN:0}]},['over'],['under'])""")
        self.assertIn("OVER 5.5 · LEAN", h)
        self.assertIn("UNDER 5.5 · SKIP", h)
        h2 = self.pg.evaluate("""()=>_boxGradeHTML({mkts:[{side:'plFav',label:'OTT -1.5',tierN:0},{side:'plDog',label:'PHI +1.5',tierN:1,hkLane:true}]},['plFav'],['plDog'])""")
        self.assertIn("OTT -1.5 · SKIP", h2)
        self.assertIn("PHI +1.5 · HIGH PROB", h2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
