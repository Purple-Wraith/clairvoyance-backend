#!/usr/bin/env python3
"""FORM tabs (every sport and league share _cmpFormBlockHTML) print the per-game differential with the BETTING sign convention (owner 2026-10-24):
a team that scores more than it allows is MINUS (22 for, 18 against = -4.0 -- like -4 on the spread), one that allows more is PLUS (18 for, 22 against = +4.0).

    python3 scripts/test_form_sign.py
"""
import functools, http.server, os, re, socketserver, threading, unittest
from pathlib import Path

ROOT = Path(os.environ.get("CV_ROOT") or Path(__file__).resolve().parent.parent)


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a, **k):
        pass


def games(pairs):
    return [{"gf": f, "ga": a, "win": f > a, "draw": f == a, "home": True, "opp": "X", "date": "Oct 1"} for f, a in pairs]


class Sign(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        socketserver.TCPServer.allow_reuse_address = True
        cls.srv = socketserver.TCPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=str(ROOT / "docs")))
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.pw = sync_playwright().start(); cls.b = cls.pw.chromium.launch(); cls.pg = cls.b.new_page()
        cls.pg.route("**/*espn.com/**", lambda r: r.abort()); cls.pg.route("**/*workers.dev/**", lambda r: r.abort())
        cls.pg.goto(f"http://127.0.0.1:{cls.srv.server_address[1]}/app.html?nosb=1")
        cls.pg.wait_for_function("typeof _cmpFormBlockHTML==='function'"); cls.pg.wait_for_timeout(800)

    @classmethod
    def tearDownClass(cls):
        cls.b.close(); cls.pw.stop(); cls.srv.shutdown()

    def diff(self, pairs, **opt):
        txt = self.pg.evaluate("""([g,o])=>{const d=document.createElement('div');d.innerHTML=_cmpFormBlockHTML('T','#0ff',g,g,o);return d.innerText.replace(/\\s+/g,' ')}""", [games(pairs), opt])
        m = re.search(r"diff ([+-]?\d+(?:\.\d+)?)", txt)
        return m.group(1) if m else None

    def test_outscoring_opponents_is_minus(self):
        self.assertEqual(self.diff([(22, 18)] * 5, gf="PF", ga="PA", dec=1), "-4.0")

    def test_being_outscored_is_plus(self):
        self.assertEqual(self.diff([(18, 22)] * 5, gf="PF", ga="PA", dec=1), "+4.0")

    def test_an_even_record_is_zero_with_no_sign(self):
        self.assertEqual(self.diff([(3, 3)] * 5), "0.0")
        self.assertEqual(self.diff([(30, 20), (20, 30)], dec=1), "0.0")

    def test_a_tiny_edge_that_rounds_to_zero_is_not_signed(self):
        self.assertEqual(self.diff([(2, 2)] * 4 + [(3, 2)], dec=0), "0")                      # -0.2 / game at 0 decimals

    def test_goal_sports_use_the_same_signs(self):
        self.assertEqual(self.diff([(4, 1), (3, 2), (2, 2), (5, 1), (1, 0)]), "-1.8")        # soccer / hockey: 15 for, 6 against over 5 games = 9 / 5
        self.assertEqual(self.diff([(0, 2), (1, 1), (0, 1)]), "+1.0")

    def test_the_label_explains_the_sign(self):
        html = self.pg.evaluate("_cmpFormBlockHTML('T','#0ff',%s,%s,{})" % (str(games([(22, 18)]) ).replace("True", "true").replace("False", "false"), "[]"))
        self.assertIn("Minus = outscoring opponents", html)


if __name__ == "__main__":
    unittest.main(verbosity=2)
