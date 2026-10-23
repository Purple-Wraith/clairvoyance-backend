#!/usr/bin/env python3
"""NBA totals show the ADJUSTED line for both sides (owner 2026-10-23): UNDER moved up and OVER moved down, either lockable; the model's lean first, the other side flagged OTHER SIDE.
Other sports and spreads are unchanged (one total chip, one spread chip).

    python3 scripts/test_nba_alt_both_sides.py
"""
import functools, http.server, json, re, socketserver, threading, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a, **k):
        pass


MKTS = [{"side": "under", "label": "UNDER 236.5", "prob": .56, "tierN": 1, "dec": 1.91, "ml": "-110"}, {"side": "over", "label": "OVER 236.5", "prob": .44, "tierN": 0, "dec": 1.91, "ml": "-110"},
        {"side": "sprdDog", "label": "CLE +2.5", "prob": .63, "tierN": 3, "dec": 1.91, "ml": "-110"}, {"side": "sprdFav", "label": "PHI -2.5", "prob": .37, "tierN": 0, "dec": 1.91, "ml": "-110"}]


class BothSides(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        socketserver.TCPServer.allow_reuse_address = True
        cls.srv = socketserver.TCPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=str(ROOT / "docs")))
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.pw = sync_playwright().start(); cls.b = cls.pw.chromium.launch(); cls.pg = cls.b.new_page()
        cls.pg.goto(f"http://127.0.0.1:{cls.srv.server_address[1]}/app.html?nosb=1")
        cls.pg.wait_for_function("typeof _altRowFor==='function'"); cls.pg.wait_for_timeout(1000)

    @classmethod
    def tearDownClass(cls):
        cls.b.close(); cls.pw.stop(); cls.srv.shutdown()

    def row(self, sport, mkts):
        return self.pg.evaluate("""([sp,m])=>{window._altReg={};const h=_altRowFor(sp,'PHI','CLE','2026-10-23T01:00Z',{mkts:m},false);
          return {labels:[...h.matchAll(/class="altp">([^<]+)</g)].map(x=>x[1]),other:(h.match(/OTHER SIDE/g)||[]).length,keys:Object.keys(window._altReg)}}""", [sport, mkts])

    def test_nba_total_shows_both_sides_lean_first(self):
        r = self.row("NBA", MKTS)
        totals = [l for l in r["labels"] if re.match(r"(OVER|UNDER) ", l)]
        self.assertEqual(len(totals), 2)
        self.assertTrue(totals[0].startswith("UNDER"))                         # the model's lean first
        under = float(totals[0].split()[1]); over = float(totals[1].split()[1])
        self.assertGreater(under, 236.5)                                      # UNDER moves UP ...
        self.assertLess(over, 236.5)                                          # ... OVER moves DOWN
        self.assertAlmostEqual(under - 236.5, 236.5 - over, places=6)          # side-neutral table: the same cushion either way
        self.assertEqual(r["other"], 1)
        self.assertEqual(len(r["keys"]), len(set(r["keys"])))
        self.assertEqual(len(r["keys"]), 3)                                    # lean total, other-side total, spread

    def test_the_other_side_is_the_lean_when_the_model_likes_the_over(self):
        mk = [dict(m) for m in MKTS]
        mk[0]["prob"], mk[1]["prob"] = .44, .56
        r = self.row("NBA", mk)
        totals = [l for l in r["labels"] if re.match(r"(OVER|UNDER) ", l)]
        self.assertTrue(totals[0].startswith("OVER"))

    def test_other_sports_are_unchanged(self):
        r = self.row("NFL", [dict(m, label=m["label"].replace("236.5", "50.5")) for m in MKTS])
        self.assertEqual(len([l for l in r["labels"] if re.match(r"(OVER|UNDER) ", l)]), 1)
        self.assertEqual(r["other"], 0)

    def test_locking_the_other_side_records_an_adjusted_over(self):
        self.row("NBA", MKTS)
        key = [k for k in self.pg.evaluate("Object.keys(window._altReg)") if k.endswith("|opp")][0]
        a = self.pg.evaluate("(k)=>window._altReg[k].alt", key)
        self.assertTrue(a["label"].startswith("OVER"))
        self.assertGreaterEqual(a["prob"], 0.62)
        self.assertEqual(a["posted"], 236.5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
