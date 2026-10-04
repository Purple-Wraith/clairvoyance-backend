#!/usr/bin/env python3
"""Adjusted-line tracker (docs/app.html _altRows/_altStats/_altTrackerHTML...): grading parity with scripts/alt_line_scorecard.py, totals, Home/Overall/Analytics rendering,
the original -> shifted notation under pick rows, and the 'scores do not reproduce the stored outcome' guard.  Uses a synthetic ledger (no network)."""
import functools, http.server, json, socketserver, sys, threading, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import alt_line_scorecard as sc  # noqa: E402


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a, **k):
        pass


def pick(i, sport, bt, bet, posted, line, h, a, outcome, hA="HOM", awA="AWY", dec=1.4, label=None, shift=None, date="2026-10-04"):
    return {"id": f"alt{i}", "sport": sport, "betType": bt, "betOn": bet, "hA": hA, "awA": awA, "hScore": h, "aScore": a, "outcome": outcome,
            "date": date, "lockedAt": 1791100000000 + i, "decOdds": dec, "winProb": 0.68, "ml": "-250", "priceSource": "estimated",
            "altLine": {"posted": posted, "line": line, "shift": shift if shift is not None else abs(line - posted), "postedLabel": label or f"{bet.split()[0]} {posted}", "postedDec": 1.909}}


# (picks, expected alt result, expected posted result)
CASES = [
    (pick(1, "NFL", "SPREAD", "HOM -3.5", -10.0, -3.5, 27, 20, "win", label="HOM -10.0"), "win", "loss"),      # won by 7: covers -3.5, not -10  -> saved
    (pick(2, "CFB", "OU", "UNDER 55.5", 49.5, 55.5, 28, 24, "win"), "win", "loss"),                            # 52 total: under 55.5 wins, under 49.5 loses -> saved
    (pick(3, "NHL", "OU", "OVER 3.5", 5.5, 3.5, 3, 2, "win"), "win", "loss"),                                    # 5 goals: over 3.5 wins, over 5.5 loses -> saved
    (pick(4, "NFL", "SPREAD", "AWY +9.5", 3.5, 9.5, 30, 20, "loss", label="AWY +3.5"), "loss", "loss"),         # lost by 10: loses both
    (pick(5, "CFB", "SPREAD", "AWY +10.0", 3.0, 10.0, 30, 20, "push", label="AWY +3.0"), "push", "loss"),       # lost by 10 -> push at +10, loss at +3
    (pick(6, "NBA", "OU", "UNDER 235.5", 229.5, 235.5, 110, 105, "win"), "win", "win"),                          # 215 total: wins at both lines
    (pick(7, "NFL", "OU", "OVER 38.5", 44.5, 38.5, None, None, "win"), "win", None),                              # settled but no scores -> posted unknown
    (pick(8, "NFL", "OU", "UNDER 50.5", 44.5, 50.5, 21, 20, "loss"), "loss", None),                               # scores say UNDER 50.5 WINS (41) but stored outcome is loss -> do not trust scores
]
PENDING = pick(9, "NFL", "SPREAD", "HOM -3.5", -10.0, -3.5, None, None, "pending", label="HOM -10.0")


class AltTracker(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        socketserver.TCPServer.allow_reuse_address = True
        cls.srv = socketserver.TCPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=str(ROOT / "docs")))
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.port = cls.srv.server_address[1]
        cls.pw = sync_playwright().start(); cls.browser = cls.pw.chromium.launch()
        cls.picks = [c[0] for c in CASES] + [PENDING]

    @classmethod
    def tearDownClass(cls):
        cls.browser.close(); cls.pw.stop(); cls.srv.shutdown()

    def page(self, picks=None):
        pg = self.browser.new_page(viewport={"width": 1300, "height": 900})
        pg.goto(f"http://127.0.0.1:{self.port}/app.html?nosb=1")
        pg.wait_for_function("typeof _altRows==='function'&&typeof saveP==='function'"); pg.wait_for_timeout(800)
        pg.evaluate("(p)=>{saveP(p);window._altPeriod='all'}", self.picks if picks is None else picks)
        return pg

    def test_grading_matches_python_and_expectations(self):
        pg = self.page()
        rows = pg.evaluate("_altRows(getP().filter(p=>p.id.startsWith('alt'))).map(r=>[r.p.id,r.alt,r.posted,r.saved])")
        got = {r[0]: r for r in rows}
        for p, exp_alt, exp_post in CASES:
            r = got[p["id"]]
            self.assertEqual(r[1], exp_alt, p["id"]); self.assertEqual(r[2], exp_post, p["id"])
            # parity with the backend scorecard's grader (only where the scores reproduce the stored outcome)
            if exp_post is not None:
                self.assertEqual(sc.grade(p, float(p["altLine"]["posted"])), exp_post, "python grader: " + p["id"])
                self.assertEqual(sc.grade(p, float(p["altLine"]["line"])), exp_alt, "python grader (locked line): " + p["id"])
        self.assertEqual(sum(1 for r in rows if r[3]), 3)       # alt1, alt2, alt3 were saved by the shift
        pg.close()

    def test_totals(self):
        pg = self.page()
        s = pg.evaluate("_altStats(_altRows(getP().filter(p=>p.id.startsWith('alt'))))")
        self.assertEqual((s["n"], s["settled"], s["pending"]), (9, 8, 1))
        self.assertEqual((s["altW"], s["altL"], s["altP"]), (5, 2, 1))     # wins: 1,2,3,6,7  losses: 4,8  push: 5
        self.assertEqual(s["both"], 6)                                      # graded both ways: 1-6 (7 has no score, 8 fails the cross-check)
        self.assertEqual((s["bPostW"], s["bPostL"]), (1, 5)); self.assertEqual((s["bAltW"], s["bAltL"]), (4, 1))   # posted: only #6 wins; locked: 1,2,3,6 win, 4 loses (5 pushes)
        self.assertEqual((s["saved"], s["cost"]), (3, 0))
        pg.close()

    def test_rendering_everywhere(self):
        pg = self.page()
        pg.evaluate("renderHomePage()"); pg.wait_for_timeout(500)
        home = pg.evaluate("(document.getElementById('home-adj-tracker')||{}).innerText||''")
        for needle in ("ADJUSTED LINES", "ADJUSTED", "AT LOCKED LINE", "SAME PICKS AT POSTED", "SAVED BY SHIFT", "POSTED", "LOCKED", "NFL", "CFB", "NHL"):
            self.assertIn(needle, home)
        pg.evaluate("renderOverall()")
        self.assertIn("ADJUSTED LINES", pg.evaluate("(document.getElementById('ovr-adj-tracker')||{}).innerText||''"))
        pg.evaluate("setSub('analytics','adjlines')"); pg.wait_for_timeout(400)
        full = pg.evaluate("document.getElementById('adjlines-body').innerText")
        self.assertIn("EVERY ADJUSTED PICK", full); self.assertIn("NOT SHIFTED BY THE ENGINE YET", full)
        self.assertEqual(pg.evaluate("document.querySelectorAll('#analytics-adjlines .sb2, #sp-analytics .sb2').length>0"), True)
        # notation under a pick row: original -> shifted, plus the posted-line result once settled
        row = pg.evaluate("_pickRowHTML(getP().find(p=>p.id==='alt1'),{mode:'settled'})")
        self.assertIn("POSTED", row); self.assertIn("HOM -10.0", row); self.assertIn("LOCKED", row); self.assertIn("SAVED BY SHIFT", row); self.assertIn("6.5 PTS", row)
        pend = pg.evaluate("_pickRowHTML(getP().find(p=>p.id==='alt9'),{mode:'pending'})")
        self.assertIn("ADJUSTED 6.5 PTS", pend); self.assertNotIn("POSTED LINE", pend)
        plain = pg.evaluate("_pickRowHTML({id:'z',sport:'NFL',betType:'ML',betOn:'HOM',hA:'HOM',awA:'AWY',outcome:'win',ml:'-110'},{mode:'settled'})")
        self.assertNotIn("ADJUSTED", plain)                                  # picks without altLine are untouched
        pg.close()

    def test_empty_state_and_periods(self):
        pg = self.page([])
        pg.evaluate("renderHomePage()"); pg.wait_for_timeout(400)
        self.assertIn("NO ADJUSTED-LINE PICKS YET", pg.evaluate("document.getElementById('home-adj-tracker').innerText"))
        pg.evaluate("(p)=>{saveP(p);_altSetPeriod('today')}", self.picks)
        self.assertEqual(pg.evaluate("window._altPeriod"), "today")
        pg.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
