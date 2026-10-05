#!/usr/bin/env python3
"""The adjusted-line shift exists twice: _alt_shift_leg() in scripts/auto_lock_settle.py (what the automated lock uses) and _altShift() in docs/app.html (the card's
ADJ. LINE chip, and manual locks). They must agree to the line and the probability, for every sport/market that is shifted -- including NBA spreads (added 2026-10-05).

    python3 scripts/test_alt_js_parity.py
"""
import functools, http.server, socketserver, sys, threading, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import auto_lock_settle as A  # noqa: E402

# (sport, market, side, label, model probability at the posted line)
CASES = [
    ("NBA", "SPREAD", "sprdFav", "BOS -7.5", .60), ("NBA", "SPREAD", "sprdDog", "MIA +7.5", .60), ("NBA", "SPREAD", "sprdFav", "BOS -3.5", .60),
    ("NBA", "SPREAD", "sprdFav", "BOS -1.5", .60), ("NBA", "SPREAD", "sprdDog", "MIA +14.5", .60), ("NBA", "SPREAD", "sprdFav", "BOS -10", .60),
    ("NBA", "OU", "over", "OVER 221.5", .60), ("NBA", "OU", "under", "UNDER 225.5", .60),
    ("NFL", "SPREAD", "sprdDog", "NYG +3.5", .60), ("NFL", "OU", "under", "UNDER 44.5", .60),
    ("CFB", "SPREAD", "sprdFav", "UGA -14.5", .60), ("CFB", "OU", "over", "OVER 58.5", .60),
    ("NHL", "OU", "over", "OVER 6.5", .62), ("SHL", "OU", "under", "UNDER 5.5", .60),
    ("NBA", "SPREAD", "sprdFav", "BOS -ATS", .60), ("NBA", "ML", "mlFav", "BOS ML", .60),
]


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a, **k):
        pass


class Parity(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        socketserver.TCPServer.allow_reuse_address = True
        cls.srv = socketserver.TCPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=str(ROOT / "docs")))
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch()
        cls.pg = cls.browser.new_page()
        cls.pg.goto(f"http://127.0.0.1:{cls.srv.server_address[1]}/app.html?nosb=1")
        cls.pg.wait_for_function("typeof _altShift==='function'")

    @classmethod
    def tearDownClass(cls):
        cls.browser.close(); cls.pw.stop(); cls.srv.shutdown()

    def test_every_case_matches(self):
        for sport, mkt, side, label, p in CASES:
            py = A._alt_shift_leg({"kind": "GAME", "sport": sport, "hA": "H", "awA": "A", "side": side, "label": label, "prob": p, "ml": "-110", "dec": 1.909,
                                   "tierN": 2, "evVal": .03})
            js = self.pg.evaluate("([s,m,l,p])=>_altShift(s,m,l,p)", [sport, mkt, label, p])
            if py is None:
                self.assertIsNone(js, f"{sport} {label}: python keeps the posted line but the card shifts it")
                continue
            self.assertIsNotNone(js, f"{sport} {label}: python shifts it but the card does not")
            self.assertEqual(js["label"], py["label"], f"{sport} {label}")
            self.assertAlmostEqual(js["prob"], py["prob"], places=3, msg=f"{sport} {label}")
            self.assertAlmostEqual(js["shift"], py["altLine"]["shift"], places=6, msg=f"{sport} {label}")
            self.assertAlmostEqual(js["dec"], py["dec"], places=3, msg=f"{sport} {label}")

    def test_nba_spread_is_really_shifted_by_the_curve(self):
        js = self.pg.evaluate("()=>_altShift('NBA','SPREAD','BOS -7.5',.6)")
        self.assertEqual((js["label"], js["shift"]), ("BOS -1.5", 6))
        js = self.pg.evaluate("()=>_altShift('NBA','SPREAD','MIA +7.5',.6)")
        self.assertEqual(js["label"], "MIA +13.5")


if __name__ == "__main__":
    unittest.main(verbosity=2)
