#!/usr/bin/env python3
"""Hockey over/unders in the HIGH PROB lane (owner decision 2026-10-06, floor 62%): NHL + SHL/Liiga/NLA/Extraliga totals with a real posted price now qualify even when the value tier
is LEAN/SKIP; the adjusted-line shift then moves them toward the 60-65% band.  docs/app.html (_hkLaneRow, HOCKEY_LANE_OU_P) and scripts/auto_lock_settle.py (build_qualifying) must agree.

    python3 scripts/test_hockey_ou_lane.py
"""
import functools, http.server, socketserver, sys, threading, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import auto_lock_settle as A  # noqa: E402


def market(side, label, prob, tier=1, lane=False, real=True):
    return {"side": side, "label": label, "prob": prob, "tierN": tier, "hkLane": lane, "evVal": -0.04, "ml": "-110", "dec": 1.91, "priceSource": "market" if real else "assumed"}


def result(sport, markets):
    return {"gameLegs": [{"sport": sport, "hA": "H", "awA": "A", "markets": markets, "startMs": 4_000_000_000_000}], "propLegs": []}


class Python(unittest.TestCase):
    def test_constant_and_legend(self):
        self.assertEqual(A.HOCKEY_LANE_OU_P, 0.62)
        self.assertIn("62%", A.build_locks_email_html([{"kind": "GAME", "sport": "NHL", "hA": "H", "awA": "A", "side": "over", "label": "OVER 5.5", "prob": .63, "ml": "-110", "dec": 1.91,
                                                         "tierN": 1, "evVal": -.04, "lane": True, "mcSummary": None, "best": None, "startMs": None}], True, 1))

    def test_a_lane_flagged_total_qualifies_through_the_lane_even_at_low_tier(self):
        q = A.build_qualifying(result("NHL", [market("over", "OVER 5.5", .64, tier=1, lane=True)]), now=1)
        self.assertEqual(len(q), 1)
        self.assertTrue(q[0]["lane"])

    def test_a_total_without_the_lane_and_low_tier_does_not_qualify(self):
        self.assertEqual(A.build_qualifying(result("NHL", [market("over", "OVER 5.5", .58, tier=1, lane=False)]), now=1), [])

    def test_lane_needs_a_real_price(self):
        self.assertEqual(A.build_qualifying(result("SHL", [market("under", "UNDER 5.5", .70, tier=1, lane=True, real=False)]), now=1), [])

    def test_the_adjusted_line_shift_still_runs_on_a_lane_total_and_keeps_the_lane_flag(self):
        q = A.build_qualifying(result("NHL", [market("over", "OVER 6.5", .62, tier=1, lane=True)]), now=1)
        shifted = A._apply_alt_lines(q)
        self.assertEqual(len(shifted), 1)
        self.assertTrue(shifted[0]["lane"])
        self.assertIn(shifted[0]["label"], ("OVER 5.5", "OVER 6.5"))                # shifted toward safety, or left when it already clears the floor
        if shifted[0]["label"] != "OVER 6.5":
            self.assertEqual(shifted[0]["priceSource"], "estimated")


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a, **k):
        pass


class Js(unittest.TestCase):
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
        cls.pg.wait_for_function("typeof _hkLaneRow==='function'")
        cls.pg.wait_for_timeout(800)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close(); cls.pw.stop(); cls.srv.shutdown()

    def lane(self, side, prob, dec=1.91, real=True):
        return self.pg.evaluate("([s,p,d,r])=>_hkLaneRow({hk:true,priceReal:r,mktDec:d,side:s,prob:p})", [side, prob, dec, real])

    def test_totals_qualify_at_62_and_not_below(self):
        self.assertTrue(self.lane("over", 0.62))
        self.assertTrue(self.lane("under", 0.70))
        self.assertFalse(self.lane("over", 0.61))
        self.assertFalse(self.lane("under", 0.55))

    def test_price_and_ev_guards_still_apply_to_totals(self):
        self.assertFalse(self.lane("over", 0.64, real=False))                  # no real price
        self.assertFalse(self.lane("over", 0.62, dec=1.30))                    # EV at a 1.30 price is far worse than -7%

    def test_ml_and_puck_line_rules_are_unchanged(self):
        self.assertTrue(self.lane("mlFav", 0.65))
        self.assertFalse(self.lane("mlFav", 0.64))
        self.assertTrue(self.lane("plDog", 0.65))
        self.assertFalse(self.lane("plFav", 0.90))                             # a -1.5 favourite never uses the lane

    def test_js_and_python_floors_agree(self):
        self.assertEqual(self.pg.evaluate("HOCKEY_LANE_OU_P"), A.HOCKEY_LANE_OU_P)


if __name__ == "__main__":
    unittest.main(verbosity=2)
