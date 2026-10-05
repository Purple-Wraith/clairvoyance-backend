#!/usr/bin/env python3
"""'Why this pick' reasoning (docs/app.html _attachReasoning / _mcLineMargin / _altReasoning / _whyThisPickHTML and scripts/auto_lock_settle.py _alt_finish):
adjusted-line picks describe the SHIFTED line (never the posted line's tier/EV), JS and Python write identical text, the projected margin is signed for the FAVORITE
(it printed the home-signed value next to an away favorite), assumed prices are labelled, and the adjusted-lines tracker shows the WHY row.

    python3 scripts/test_why_reasoning.py
"""
import functools, http.server, socketserver, sys, threading, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import auto_lock_settle as A  # noqa: E402

POSTED_REASONING = ("PICK: BAL -11.5 — OPTIMAL (63.5% win prob, EV +21.2%)\n"
                    "MODEL: Projected margin +14.1 (BAL), total 44.2 — BAL covers the 11.5-pt line in 63.5% of simulated outcomes, from a 25k-sim Monte Carlo.\n"
                    "WHY: BAL is the stronger side.")


def nfl_leg():
    return {"kind": "GAME", "sport": "NFL", "hA": "BAL", "awA": "CLE", "side": "sprdFav", "label": "BAL -11.5", "prob": .635, "ml": "-110", "dec": 1.909, "tierN": 2,
            "evVal": .212, "reasoning": POSTED_REASONING}


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a, **k):
        pass


class PythonSide(unittest.TestCase):
    def test_adjusted_pick_describes_the_shifted_line_not_the_posted_one(self):
        r = A._alt_shift_leg(nfl_leg())
        txt = r["reasoning"]
        self.assertTrue(txt.startswith("PICK: BAL -5.5 — ADJUSTED LINE (67.3% estimated"))
        for gone in ("OPTIMAL", "EV +21.2", "11.5-pt line in", "63.5% win prob"):
            self.assertNotIn(gone, txt)
        self.assertIn("estimate", txt)
        self.assertIn("MODEL: Projected margin +14.1 (BAL), total 44.2, from a 25k-sim Monte Carlo.", txt)    # the projection survives, the posted cover claim does not
        self.assertIn("rated this side 63.5%", txt)

    def test_hockey_uses_goals_and_a_missing_model_line_is_fine(self):
        leg = {"kind": "GAME", "sport": "NHL", "hA": "A", "awA": "B", "side": "over", "label": "OVER 6.5", "prob": .62, "ml": "-110", "dec": 1.909, "tierN": 2, "evVal": .03}
        r = A._alt_shift_leg(leg)
        if r:                                                      # (a pick that already clears the floor keeps its posted line)
            self.assertIn("goal", r["reasoning"])
            self.assertNotIn("MODEL:", r["reasoning"])


class JsSide(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        socketserver.TCPServer.allow_reuse_address = True
        cls.srv = socketserver.TCPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=str(ROOT / "docs")))
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch()
        cls.pg = cls.browser.new_page(viewport={"width": 1100, "height": 900})
        cls.errors = []
        cls.pg.on("pageerror", lambda e: cls.errors.append(str(e)))
        cls.pg.goto(f"http://127.0.0.1:{cls.srv.server_address[1]}/app.html?nosb=1")
        cls.pg.wait_for_function("typeof _altReasoning==='function'&&typeof _mcLineMargin==='function'&&typeof saveP==='function'")
        cls.pg.wait_for_timeout(800)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close(); cls.pw.stop(); cls.srv.shutdown()

    def test_alt_reasoning_is_identical_in_js_and_python(self):
        model = "MODEL: Projected margin +14.1 (BAL), total 44.2, from a 25k-sim Monte Carlo."
        for label, p, posted_label, posted_p, k, unit, ml in (("BAL -5.5", .673, "BAL -11.5", .635, 6, "pt", model), ("UNDER 233.5", .67, "UNDER 225.5", .6, 8, "pt", None),
                                                              ("MIA +13.5", .682, "MIA +7.5", None, 6.5, "pt", None), ("OVER 5.5", .61, "OVER 6.5", .58, 1, "goal", None),
                                                              ("OVER 4.5", .64, "OVER 6.5", .5, 2, "goal", None)):
            py = A._alt_reasoning(label, p, posted_label, posted_p, k, unit, ml)
            js = self.pg.evaluate("([a,b,c,d,e,f,g])=>_altReasoning(a,b,c,d,e,f,g)", [label, p, posted_label, posted_p, k, unit, ml])
            self.assertEqual(js, py, label)

    def test_projected_margin_is_signed_for_the_favorite(self):
        mc = {"avgMargin": 3.0, "avgTotal": 41.0}                                             # HOME-signed: the home team is +3
        away_fav = self.pg.evaluate("(mc)=>_mcLineMargin(mc,'sprdFav','DET','GB',2.5,.55,.5,41.5,-3.0)", mc)   # the favorite is the AWAY team: favorite-signed margin is -3
        self.assertIn("Projected margin -3.0 (DET)", away_fav)
        home_fav = self.pg.evaluate("(mc)=>_mcLineMargin(mc,'sprdFav','GB','DET',2.5,.55,.5,41.5,3.0)", mc)
        self.assertIn("Projected margin +3.0 (GB)", home_fav)
        legacy = self.pg.evaluate("(mc)=>_mcLineMargin(mc,'sprdFav','GB','DET',2.5,.55,.5,41.5)", mc)            # no favorite-signed value passed: old behaviour
        self.assertIn("Projected margin +3.0 (GB)", legacy)

    def test_assumed_price_is_labelled_but_moneylines_and_market_prices_are_not(self):
        nm = self.pg.evaluate("""()=>{
          const nm={mkts:[{label:'BAL -5.5',side:'sprdFav',tierN:2,prob:.62,evVal:.07},{label:'BAL ML',side:'mlFav',tierN:2,prob:.7,evVal:.04},
                          {label:'OVER 5.5',side:'over',tierN:2,prob:.6,evVal:.05,priceSource:'market',mktEv:.02,mktMl:-120}]};
          _attachReasoning(nm,null,null);return nm.mkts.map(m=>m.reasoning)}""")
        self.assertIn("EV assumes a standard -110 price, not a market quote", nm[0])
        self.assertNotIn("assumes a standard", nm[1])
        self.assertNotIn("assumes a standard", nm[2])

    def test_tracker_list_shows_the_why_row_and_no_stale_nba_spread_claim(self):
        picks = [{"id": "w1", "sport": "NFL", "betType": "SPREAD", "betOn": "BAL -5.5", "hA": "BAL", "awA": "CLE", "date": "2026-10-04", "lockedAt": 1790000000000,
                  "outcome": "pending", "winProb": .67, "decOdds": 1.39, "ml": "-257", "priceSource": "estimated", "lockOrigin": "auto",
                  "altLine": {"posted": -11.5, "line": -5.5, "shift": 6, "postedLabel": "BAL -11.5"}, "reasoning": "PICK: BAL -5.5 — ADJUSTED LINE <b>x</b>"},
                 {"id": "w2", "sport": "NFL", "betType": "OU", "betOn": "UNDER 50.5", "hA": "A", "awA": "B", "date": "2026-10-03", "lockedAt": 1789990000000,
                  "outcome": "pending", "winProb": .67, "decOdds": 1.39, "ml": "-257", "priceSource": "estimated", "lockOrigin": "auto",
                  "altLine": {"posted": 44.5, "line": 50.5, "shift": 6, "postedLabel": "UNDER 44.5"}}]
        html = self.pg.evaluate("(p)=>{saveP(p);window._altPeriod='all';return _altTrackerHTML('full')}", picks)
        self.assertGreaterEqual(html.count("whypick_"), 2)                                   # both rows get the toggle (the second says no reasoning captured gracefully)
        self.assertIn("No detailed reasoning captured", html)
        self.assertNotIn("<b>x</b>", html)                                                   # stored text is escaped, never rendered as HTML
        self.assertNotIn("NBA SPREADS", html)
        self.assertEqual(self.errors, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
