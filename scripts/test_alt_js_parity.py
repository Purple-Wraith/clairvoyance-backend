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
    ("NHL", "OU", "over", "OVER 5.5", .62), ("NHL", "OU", "over", "OVER 5.5", .55), ("NHL", "OU", "over", "OVER 5.5", .70), ("NHL", "OU", "under", "UNDER 6.5", .66),
    ("NHL", "OU", "under", "UNDER 5.5", .60), ("SHL", "OU", "over", "OVER 5.5", .64), ("LIIGA", "OU", "under", "UNDER 6.5", .62), ("NLA", "OU", "over", "OVER 6.0", .62),
    ("NBA", "SPREAD", "sprdFav", "BOS -ATS", .60), ("NBA", "ML", "mlFav", "BOS ML", .60),
    # a posted line that is not a half-point (the model's raw margin stands in for a missing spread): the adjusted line still lands on x.5, identically in both implementations
    ("CFB", "SPREAD", "sprdFav", "UGA -13.3", .60), ("CFB", "SPREAD", "sprdDog", "UGA +13.7", .60), ("NFL", "SPREAD", "sprdFav", "KC -6.6", .60), ("CFB", "OU", "over", "OVER 62.3", .60),
    ("CFB", "OU", "under", "UNDER 62.6", .60), ("NBA", "OU", "over", "OVER 225.3", .60), ("NBA", "OU", "under", "UNDER 225.8", .60), ("NHL", "OU", "over", "OVER 5.3", .60), ("SHL", "OU", "under", "UNDER 5.7", .60),
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

    def test_every_adjusted_line_ends_in_point_five(self):
        """Owner 2026-11-01: 'all adjusted lines ever need to be ending with .5'.  Sweep posted lines in 0.1 steps (half-points, whole numbers, and the in-between values a raw model margin gives)."""
        def ends_half(x):
            return abs(((x % 1) + 1) % 1 - 0.5) < 1e-9
        grid = {"CFB": ("SPREAD", 3, 35), "NFL": ("SPREAD", 1, 17), "NBA": ("SPREAD", 1, 20)}
        seen = 0
        for sport, (mkt, lo, hi) in grid.items():
            for i in range(lo * 10, hi * 10 + 1):
                v = i / 10
                for sign in ("-", "+"):
                    label = f"AAA {sign}{v:.1f}"
                    js = self.pg.evaluate("([s,m,l,p])=>_altShift(s,m,l,p)", [sport, mkt, label, .6])
                    py = A._alt_shift_leg({"kind": "GAME", "sport": sport, "hA": "H", "awA": "A", "side": "sprdFav", "label": label, "prob": .6, "ml": "-110", "dec": 1.909, "tierN": 2, "evVal": .03})
                    self.assertEqual(js is None, py is None, f"{sport} {label}")
                    if js is None:
                        continue
                    seen += 1
                    self.assertTrue(ends_half(js["line"]), f"{sport} {label} -> {js['label']}")
                    self.assertTrue(ends_half(py["altLine"]["line"]), f"{sport} {label} -> {py['label']}")
                    self.assertEqual(js["label"], py["label"], f"{sport} {label}")
                    self.assertAlmostEqual(js["prob"], py["prob"], places=3, msg=f"{sport} {label}")
                    self.assertGreaterEqual(js["shift"], 1, label)                       # the cushion never shrinks below a point
        self.assertGreater(seen, 500)
        totals = {"CFB": (30, 90), "NFL": (30, 60), "NBA": (190, 250), "NHL": (4, 8), "SHL": (4, 8), "LIIGA": (4, 8), "NLA": (4, 8), "EXTRALIGA": (4, 8)}
        for sport, (lo, hi) in totals.items():
            step = 1 if hi - lo > 20 else .1
            v = float(lo)
            while v <= hi:
                for side in ("OVER", "UNDER"):
                    label = f"{side} {v:.1f}"
                    for mp in (.50, .60, .70):
                        js = self.pg.evaluate("([s,m,l,p])=>_altShift(s,m,l,p)", [sport, "OU", label, mp])
                        py = A._alt_shift_leg({"kind": "GAME", "sport": sport, "hA": "H", "awA": "A", "side": side.lower(), "label": label, "prob": mp, "ml": "-110", "dec": 1.909, "tierN": 2, "evVal": .03})
                        self.assertEqual(js is None, py is None, f"{sport} {label} {mp}")
                        if js is None:
                            continue
                        self.assertTrue(ends_half(js["line"]), f"{sport} {label} -> {js['label']}")
                        self.assertTrue(ends_half(py["altLine"]["line"]), f"{sport} {label} -> {py['label']}")
                        self.assertEqual(js["label"], py["label"], f"{sport} {label} {mp}")
                v = round(v + step, 4)

    def test_snapping_only_ever_moves_toward_safety(self):
        """The cushion measured from the posted line is at least the whole-point step the curve asked for, so the stated probability can only be kept or improved."""
        for label, sport in (("UGA -13.3", "CFB"), ("OVER 62.3", "CFB"), ("UNDER 62.6", "CFB"), ("OVER 5.3", "NHL")):
            js = self.pg.evaluate("([s,l])=>_altShift(s,l.startsWith('OVER')||l.startsWith('UNDER')?'OU':'SPREAD',l,.5)", [sport, label])
            self.assertIsNotNone(js, label)
            posted, line = js["posted"], js["line"]
            if label.startswith("OVER"):
                self.assertLess(line, posted, label)
            elif label.startswith("UNDER"):
                self.assertGreater(line, posted, label)
            else:
                self.assertGreater(line, posted, label)

    def test_nba_spread_is_really_shifted_by_the_curve(self):
        js = self.pg.evaluate("()=>_altShift('NBA','SPREAD','BOS -7.5',.6)")
        self.assertEqual((js["label"], js["shift"]), ("BOS -1.5", 6))
        js = self.pg.evaluate("()=>_altShift('NBA','SPREAD','MIA +7.5',.6)")
        self.assertEqual(js["label"], "MIA +13.5")


if __name__ == "__main__":
    unittest.main(verbosity=2)
