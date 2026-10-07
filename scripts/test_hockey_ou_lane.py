#!/usr/bin/env python3
"""Hockey over/unders in the HIGH PROB lane (owner decision 2026-10-06, floor 56% (first 62%)): NHL + SHL/Liiga/NLA/Extraliga totals with a real posted price now qualify even when the value tier
is LEAN/SKIP; the adjusted-line shift then moves them toward the 60-65% band.  docs/app.html (_hkLaneRow, HOCKEY_LANE_OU_P) and scripts/auto_lock_settle.py (build_qualifying) must agree.

    python3 scripts/test_hockey_ou_lane.py
"""
import functools, http.server, socketserver, sys, threading, unittest
from unittest import mock
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
        self.assertEqual(A.HOCKEY_LANE_OU_P, 0.56)
        self.assertIn("56%", A.build_locks_email_html([{"kind": "GAME", "sport": "NHL", "hA": "H", "awA": "A", "side": "over", "label": "OVER 5.5", "prob": .63, "ml": "-110", "dec": 1.91,
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
        q = A.build_qualifying(result("NHL", [market("over", "OVER 6.5", .58, tier=1, lane=True)]), now=1)
        shifted = A._apply_alt_lines(q)
        self.assertEqual(len(shifted), 1)
        self.assertTrue(shifted[0]["lane"])
        self.assertEqual(shifted[0]["label"], "OVER 6.5")                           # at the lane floor already: posted line and real price kept
        self.assertNotEqual(shifted[0].get("priceSource"), "estimated")
        self.assertFalse(shifted[0].get("altLine"))


class Flip(unittest.TestCase):
    def _flip_on(self):
        pt = mock.patch.object(A, "HOCKEY_ALT_FLIP", True)
        pt.start()
        self.addCleanup(pt.stop)

    """A side-flipped hockey total (e.g. posted OVER 5.5 -> locked UNDER 6.5): grading of the POSTED pick must use the original side, and the email must say the side switched."""
    def flip_pick(self, h, a):
        return {"id": "f1", "sport": "NHL", "betType": "OU", "betOn": "UNDER 6.5", "hA": "H", "awA": "A", "hScore": h, "aScore": a, "outcome": "pending",
                "altLine": {"posted": 5.5, "line": 6.5, "shift": 1.0, "postedLabel": "OVER 5.5", "flip": True}}

    def test_scorecard_grades_the_posted_pick_on_its_original_side(self):
        import alt_line_scorecard as S
        p = self.flip_pick(4, 3)                       # 7 goals: UNDER 6.5 loses, the posted OVER 5.5 would have won
        self.assertEqual(S.grade(p, 6.5), "loss")
        self.assertEqual(S.grade(p, 5.5, "OVER 5.5"), "win")
        self.assertEqual(S.grade(p, 5.5), "loss")      # without the override it would have graded the wrong side
        q = self.flip_pick(3, 3)                       # exactly 6: both win (the overlap)
        self.assertEqual((S.grade(q, 6.5), S.grade(q, 5.5, "OVER 5.5")), ("win", "win"))

    def test_email_says_the_side_switched(self):
        q = {"kind": "GAME", "sport": "NHL", "hA": "H", "awA": "A", "side": "under", "label": "UNDER 6.5", "prob": .611, "ml": "-157", "dec": 1.57, "tierN": 2, "evVal": -.05,
             "priceSource": "estimated", "altLine": {"posted": 5.5, "line": 6.5, "shift": 1.0, "postedLabel": "OVER 5.5", "flip": True}}
        html = A._leg_html(q)
        self.assertIn("Switched to the opposite side from the posted OVER 5.5", html)
        self.assertNotIn("Moved from", html)
        q["altLine"]["flip"] = False
        self.assertIn("Moved from the posted OVER 5.5", A._leg_html(q))

    def test_the_locked_leg_is_on_the_new_side(self):
        self._flip_on()                        # flips are OFF by default (2026-10-06)
        out = A._alt_shift_leg({"kind": "GAME", "sport": "NHL", "hA": "H", "awA": "A", "side": "over", "label": "OVER 5.5", "prob": .55, "ml": "-110", "dec": 1.91, "tierN": 2, "evVal": .02})
        self.assertEqual((out["label"], out["side"], out["altLine"]["flip"]), ("UNDER 6.5", "under", True))


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
        self.assertTrue(self.lane("over", 0.56))
        self.assertTrue(self.lane("under", 0.70))
        self.assertFalse(self.lane("over", 0.555))
        self.assertFalse(self.lane("under", 0.50))

    def test_price_and_ev_guards_still_apply_to_totals(self):
        self.assertFalse(self.lane("over", 0.64, real=False))                  # no real price
        self.assertFalse(self.lane("over", 0.60, dec=1.30))                    # EV at a 1.30 price is far worse than -7%

    def test_ml_and_puck_line_rules_are_unchanged(self):
        self.assertTrue(self.lane("mlFav", 0.65))
        self.assertFalse(self.lane("mlFav", 0.64))
        self.assertTrue(self.lane("plDog", 0.65))
        self.assertFalse(self.lane("plFav", 0.90))                             # a -1.5 favourite never uses the lane

    def test_tracker_grades_the_posted_pick_of_a_flip_on_its_original_side(self):
        r = self.pg.evaluate("""()=>{const p={id:'f1',sport:'NHL',betType:'OU',betOn:'UNDER 6.5',hA:'H',awA:'A',hScore:4,aScore:3,outcome:'loss',date:'2026-10-08',lockedAt:Date.now(),
          altLine:{posted:5.5,line:6.5,shift:1,postedLabel:'OVER 5.5',flip:true}};const row=_altRows([p])[0];return {posted:row.posted,cost:row.cost,saved:row.saved}}""")
        self.assertEqual(r["posted"], "win")
        self.assertTrue(r["cost"])                                  # lost at the locked line, would have won at the posted one

    def test_js_and_python_floors_agree(self):
        self.assertEqual(self.pg.evaluate("HOCKEY_LANE_OU_P"), A.HOCKEY_LANE_OU_P)


if __name__ == "__main__":
    unittest.main(verbosity=2)
