#!/usr/bin/env python3
"""NBA games show the ADJUSTED line for both sides of the total AND of the spread (owner 2026-10-23): UNDER moved up / OVER moved down, and the favourite's and the underdog's spread each moved toward
safety, any of them lockable.  The model's lean comes first; the other side carries its OWN posted-line grade plus an OTHER SIDE tag.  College football and the NFL do the same since 2026-10-24
(the OVER at an adjusted line was rarely on offer because the model usually leans UNDER); the other side uses the SAME sigma with the lean side's ledger offset removed, so it needs the larger cushion
it really needs.  Hockey keeps its single total chip.

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

CHIP_JS = """([sp,m])=>{window._altReg={};const h=_altRowFor(sp,'PHI','CLE','2026-10-23T01:00Z',{mkts:m},false);
  const chips=[...h.matchAll(/class="altp">([^<]+)<\\/span>(?:<span class="pill[^"]*">([^<]+)<\\/span>)?(?:<span class="altpg"[^>]*>POSTED ([A-Z]+)<\\/span>)?(<span class="altos"[^>]*>OTHER SIDE<\\/span>)?/g)].map(x=>({label:x[1],grade:x[2]||null,posted:x[3]||null,other:!!x[4]}));
  return {chips,keys:Object.keys(window._altReg),other:(h.match(/OTHER SIDE/g)||[]).length}}"""


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
        return self.pg.evaluate(CHIP_JS, [sport, mkts])

    def test_nba_total_shows_both_sides_lean_first(self):
        chips = self.row("NBA", MKTS)["chips"]
        totals = [c for c in chips if re.match(r"(OVER|UNDER) ", c["label"])]
        self.assertEqual(len(totals), 2)
        self.assertTrue(totals[0]["label"].startswith("UNDER"))                # the model's lean first
        under = float(totals[0]["label"].split()[1]); over = float(totals[1]["label"].split()[1])
        self.assertGreater(under, 236.5)                                      # UNDER moves UP ...
        self.assertLess(over, 236.5)                                          # ... OVER moves DOWN
        self.assertAlmostEqual(under - 236.5, 236.5 - over, places=6)          # side-neutral table: the same cushion either way

    def test_nba_spread_shows_both_teams_lean_first(self):
        r = self.row("NBA", MKTS)
        spreads = [c for c in r["chips"] if re.match(r"(CLE|PHI) [+-]", c["label"])]
        self.assertEqual(len(spreads), 2)
        self.assertTrue(spreads[0]["label"].startswith("CLE"))                  # the model's lean (the 63% side) first
        cle = float(spreads[0]["label"].split()[1]); phi = float(spreads[1]["label"].split()[1])
        self.assertGreater(cle, 2.5)                                          # both teams' numbers move toward safety (up) by the same cushion
        self.assertGreater(phi, -2.5)
        self.assertAlmostEqual(cle - 2.5, phi - (-2.5), places=6)
        self.assertEqual(len(r["keys"]), 4)                                    # lean total, other total, lean spread, other spread
        self.assertEqual(len(set(r["keys"])), 4)

    def test_every_chip_shows_its_own_grade_and_the_other_side_is_tagged(self):
        r = self.row("NBA", MKTS)
        grades = [c["grade"] for c in r["chips"]]
        self.assertEqual(grades, ["PREMIUM", "PREMIUM", "PREMIUM", "PREMIUM"])  # graded on the ADJUSTED line's win probability (NBA cushions land at 67-68%), never on the posted line
        self.assertEqual([c["posted"] for c in r["chips"]], ["LEAN", "SKIP", "PREMIUM", "SKIP"])   # the posted line's own grade, kept small: lean total 56%, other total 44%, lean spread 63%, other spread 37%
        self.assertEqual([c["other"] for c in r["chips"]], [False, True, False, True])
        self.assertEqual(r["other"], 2)
        self.assertNotIn(None, grades)

    def test_the_other_side_is_the_lean_when_the_model_likes_the_over(self):
        mk = [dict(m) for m in MKTS]
        mk[0]["prob"], mk[1]["prob"] = .44, .56
        totals = [c for c in self.row("NBA", mk)["chips"] if re.match(r"(OVER|UNDER) ", c["label"])]
        self.assertTrue(totals[0]["label"].startswith("OVER"))
        self.assertFalse(totals[0]["other"])

    def test_hockey_adjusted_totals_are_graded_on_the_adjusted_line_not_skip(self):
        mk = [{"side": "under", "label": "UNDER 6.5", "prob": .52, "tierN": 0, "dec": 1.91, "ml": "-110"}, {"side": "over", "label": "OVER 6.5", "prob": .48, "tierN": 0, "dec": 1.91, "ml": "-110"}]
        r = self.pg.evaluate(CHIP_JS.replace("'PHI','CLE'", "'BOS','NSH'"), ["NHL", mk])
        c = [x for x in r["chips"] if x["label"].startswith("UNDER")][0]
        self.assertEqual(c["posted"], "SKIP")                                     # the posted line really is SKIP ...
        self.assertIn(c["grade"], ("LEAN", "OPTIMAL", "PREMIUM"))                  # ... but the adjusted line (about 60-65% to win) is graded for what it is
        self.assertEqual(r["other"], 0)                                           # hockey still shows one chip, no other side

    def football(self, sport):
        return [dict(m, label=m["label"].replace("236.5", "45.5")) for m in MKTS] if sport == "CFB" else [dict(m, label=m["label"].replace("236.5", "47.5")) for m in MKTS]

    def test_cfb_and_nfl_show_both_sides_of_the_total_and_of_the_spread(self):
        for sport in ("CFB", "NFL"):
            r = self.row(sport, self.football(sport))
            totals = [c for c in r["chips"] if re.match(r"(OVER|UNDER) ", c["label"])]
            spreads = [c for c in r["chips"] if re.match(r"(CLE|PHI) [+-]", c["label"])]
            self.assertEqual((len(totals), len(spreads)), (2, 2), sport)
            self.assertTrue(totals[0]["label"].startswith("UNDER") and spreads[0]["label"].startswith("CLE"), sport)        # the model's lean first
            self.assertEqual([c["other"] for c in r["chips"]], [False, True, False, True], sport)
            self.assertEqual(len(set(r["keys"])), 4, sport)

    def test_every_football_option_carries_a_pick_grade(self):
        for sport in ("CFB", "NFL"):
            r = self.row(sport, self.football(sport))
            self.assertEqual(len(r["chips"]), 4, sport)
            for c in r["chips"]:
                self.assertIn(c["grade"], ("PREMIUM", "OPTIMAL", "LEAN", "SKIP"), (sport, c))                 # the other side's and the spreads' included, never blank

    def test_the_football_other_side_needs_at_least_the_lean_sides_cushion(self):
        under = lambda r: next(c for c in r["chips"] if c["label"].startswith("UNDER"))
        over = lambda r: next(c for c in r["chips"] if c["label"].startswith("OVER"))
        for sport, base in (("CFB", 45.5), ("NFL", 47.5)):
            r = self.row(sport, self.football(sport))
            cu, co = float(under(r)["label"].split()[1]) - base, base - float(over(r)["label"].split()[1])
            self.assertGreater(cu, 0); self.assertGreater(co, 0)                                               # UNDER up, OVER down
            if sport == "CFB":
                self.assertGreater(co, cu)                                                                      # CFB's lean curve has a +1.9 offset the other side does not get
            else:
                self.assertAlmostEqual(co, cu, places=6)                                                        # NFL's curve has no offset: both sides alike

    def test_hockey_still_shows_one_total_chip(self):
        mk = [{"side": "under", "label": "UNDER 6.5", "prob": .52, "tierN": 0, "dec": 1.91, "ml": "-110"}, {"side": "over", "label": "OVER 6.5", "prob": .48, "tierN": 0, "dec": 1.91, "ml": "-110"}]
        r = self.pg.evaluate(CHIP_JS.replace("'PHI','CLE'", "'BOS','NSH'"), ["NHL", mk])
        self.assertEqual(len([c for c in r["chips"] if re.match(r"(OVER|UNDER) ", c["label"])]), 1); self.assertEqual(r["other"], 0)

    def test_the_football_other_side_is_lockable_and_listed_in_top_adjusted_lines(self):
        self.row("CFB", self.football("CFB"))
        keys = self.pg.evaluate("Object.keys(window._altReg)")
        opp = [k for k in keys if k.endswith("|opp")]
        self.assertEqual(len(opp), 2)
        over = next(self.pg.evaluate("(k)=>window._altReg[k].alt", k) for k in opp if self.pg.evaluate("(k)=>window._altReg[k].alt.label", k).startswith("OVER"))
        self.assertGreaterEqual(over["prob"], 0.62); self.assertEqual(over["posted"], 45.5)
        self.assertTrue(self.pg.evaluate("(k)=>typeof _lockAltKey==='function'&&!!window._altReg[k]", opp[0]))

    def test_locking_the_other_side_records_an_adjusted_line(self):
        self.row("NBA", MKTS)
        keys = [k for k in self.pg.evaluate("Object.keys(window._altReg)") if k.endswith("|opp")]
        self.assertEqual(len(keys), 2)
        alts = [self.pg.evaluate("(k)=>window._altReg[k].alt", k) for k in keys]
        over = [a for a in alts if a["label"].startswith("OVER")][0]
        self.assertGreaterEqual(over["prob"], 0.62)
        self.assertEqual(over["posted"], 236.5)
        spr = [a for a in alts if a["label"].startswith("PHI")][0]
        self.assertEqual(spr["posted"], -2.5)
        self.assertGreaterEqual(spr["prob"], 0.62)


if __name__ == "__main__":
    unittest.main(verbosity=2)
