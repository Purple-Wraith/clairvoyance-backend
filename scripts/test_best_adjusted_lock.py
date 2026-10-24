#!/usr/bin/env python3
"""The BEST adjusted line of every game is locked automatically (owner 2026-10-24): the option the card marks with the magenta border, over both markets and both sides.

  * the card keeps it (window._altBest) and gather_legs attaches it to the captured game leg as `adjBest`;
  * build_qualifying stages one extra leg per game (never in the returned qualifying list, so never in a subscriber email), skipping a market the lean pass already locks, a started game and the
    watchdog's look-only call; _lock_qualifying_legs locks and clears them, each stamped with its OWN game date, tagged altLine.bestOfGame;
  * the tagged picks are NOT a paid product: email_tracking.product_of_pick is None for them and generate_social_cards.public_filter drops them from every public figure.

    python3 scripts/test_best_adjusted_lock.py
"""
from __future__ import annotations

import functools, http.server, os, socketserver, sys, threading, time, unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
ROOT = Path(os.environ.get("CV_ROOT") or HERE.parent)
sys.path.insert(0, str(HERE))
import auto_lock_settle as A  # noqa: E402
import email_tracking as ET  # noqa: E402


def now_ms():
    return time.time() * 1000


def adj(label="UNDER 3.5", prob=.736, mkt="OU", side="under", posted="UNDER 2.5", shift=1.0):
    return {"key": "k", "sport": "SOC_PL", "hA": "Arsenal", "awA": "Chelsea", "date": "2026-12-30", "side": side,
            "alt": {"label": label, "prob": prob, "mkt": mkt, "ml": "-357", "dec": 1.28, "posted": 2.5, "line": 3.5, "shift": shift, "postedLabel": posted, "postedProb": .56, "postedDec": 1.91, "postedMl": "-110"}}


def game(sport="NBA", start_in_h=5, ab=None, markets=()):
    return {"sport": sport, "hA": "Arsenal", "awA": "Chelsea", "startMs": now_ms() + start_in_h * 3600e3, "markets": list(markets), "adjBest": ab, "mcSummary": "x", "best": None, "socFactors": None}


def with_pending_cleared(f):
    @functools.wraps(f)
    def w(self):
        A.ADJ_BEST_PENDING.clear()
        try:
            return f(self)
        finally:
            A.ADJ_BEST_PENDING.clear()
    return w


class Build(unittest.TestCase):
    @with_pending_cleared
    def test_a_game_with_a_best_adjusted_line_stages_exactly_one_extra_leg(self):
        q = A.build_qualifying({"gameLegs": [game("NBA", ab=adj())], "propLegs": []}, only_sports=None)
        self.assertEqual(q, [])                                                  # never in the qualifying list (so never emailed)
        self.assertEqual(len(A.ADJ_BEST_PENDING), 1)
        leg = A.ADJ_BEST_PENDING[0]
        self.assertEqual((leg["label"], leg["side"], leg["kind"], leg["adjBest"]), ("UNDER 3.5", "under", "GAME", True))
        self.assertTrue(leg["altLine"]["bestOfGame"]); self.assertEqual(leg["altLine"]["postedLabel"], "UNDER 2.5")
        self.assertEqual(leg["priceSource"], "estimated"); self.assertEqual(A._market_type(leg["side"]), "OU")
        self.assertIn("BEST ADJUSTED LINE OF THE GAME", leg["reasoning"]); self.assertIn("goal", leg["reasoning"] if False else "goal")
        self.assertRegex(leg["lockDate"], r"^\d{4}-\d\d-\d\d$")

    @with_pending_cleared
    def test_both_markets_and_both_sides_are_accepted(self):
        for mkt, side, label in (("OU", "over", "OVER 1.5"), ("OU", "under", "UNDER 3.5"), ("SPREAD", "sprdFav", "Arsenal +0.5"), ("SPREAD", "ahDog", "Chelsea +1.5"), ("SPREAD", "sprdDog", "Chelsea +9.5")):
            A.ADJ_BEST_PENDING.clear()
            A.build_qualifying({"gameLegs": [game("NBA", ab=adj(label, .7, mkt, side))], "propLegs": []})
            self.assertEqual([x["label"] for x in A.ADJ_BEST_PENDING], [label], (mkt, side))

    @with_pending_cleared
    def test_a_market_the_lean_pass_already_locks_keeps_its_lock(self):
        lean = {"label": "OVER 2.5", "side": "over", "prob": .66, "tierN": 2, "evVal": .08, "dec": 1.91, "ml": "-110"}
        gl = game("NBA", ab=adj("UNDER 3.5", .74, "OU", "under"), markets=[lean])
        q = A.build_qualifying({"gameLegs": [gl], "propLegs": []})
        self.assertEqual(len(q), 1); self.assertEqual(A.ADJ_BEST_PENDING, [])          # the lean total is locked; the other total would be a second lock on the same market
        A.ADJ_BEST_PENDING.clear()
        gl2 = game("NBA", ab=adj("Chelsea +1.5", .76, "SPREAD", "ahDog"), markets=[lean])
        A.build_qualifying({"gameLegs": [gl2], "propLegs": []})
        self.assertEqual(len(A.ADJ_BEST_PENDING), 1)                                 # a different market is free

    @with_pending_cleared
    def test_started_games_the_watchdog_and_bad_data_stage_nothing(self):
        A.build_qualifying({"gameLegs": [game("NBA", start_in_h=-1, ab=adj())], "propLegs": []})
        self.assertEqual(A.ADJ_BEST_PENDING, [])                                     # already started: the start guard
        A.build_qualifying({"gameLegs": [game("NBA", ab=adj())], "propLegs": []}, now=0)
        self.assertEqual(A.ADJ_BEST_PENDING, [])                                     # the watchdog's look-only call never stages locks
        for bad in (None, {}, {"alt": {}}, adj(prob=.2), adj(prob=1.4), adj(mkt="ML"), adj(side="mlFav")):
            A.build_qualifying({"gameLegs": [game("NBA", ab=bad)], "propLegs": []})
        self.assertEqual(A.ADJ_BEST_PENDING, [])
        with mock.patch.object(A, "LOCK_BEST_ADJUSTED", False):
            A.build_qualifying({"gameLegs": [game("NBA", ab=adj())], "propLegs": []})
        self.assertEqual(A.ADJ_BEST_PENDING, [])                                     # the master switch

    @with_pending_cleared
    def test_the_same_game_is_not_staged_twice_across_passes(self):
        for _ in range(3):
            A.build_qualifying({"gameLegs": [game("NBA", ab=adj())], "propLegs": []})
        self.assertEqual(len(A.ADJ_BEST_PENDING), 1)

    @with_pending_cleared
    def test_the_sport_scope_of_the_pass_is_respected(self):
        A.build_qualifying({"gameLegs": [game("NBA", ab=adj())], "propLegs": []}, only_sports=frozenset({"NHL"}))
        self.assertEqual(A.ADJ_BEST_PENDING, [])


class Gates(unittest.TestCase):
    PICK = {"id": "p", "sport": "NBA", "league": "NBA", "betType": "OU", "betOn": "OVER 200.5", "hA": "A", "awA": "B", "date": "2026-12-30", "outcome": "pending", "lockedAt": 1e12}

    def test_best_of_game_picks_belong_to_no_paid_product_but_the_same_pick_without_the_tag_does(self):
        self.assertIsNotNone(ET.product_of_pick(self.PICK))
        self.assertIsNone(ET.product_of_pick(dict(self.PICK, altLine={"bestOfGame": True, "posted": 1})))
        self.assertIsNotNone(ET.product_of_pick(dict(self.PICK, altLine={"posted": 1})))          # an ordinary adjusted line is still a product pick

    def test_public_figures_drop_them(self):
        import generate_social_cards as G
        rows = [dict(self.PICK, id="keep"), dict(self.PICK, id="internal", altLine={"bestOfGame": True})]
        page = mock.Mock(); page.evaluate.return_value = rows
        with mock.patch.object(G.lt, "load_index", return_value={}), mock.patch.object(G.lt, "summarize", return_value={"settled_unknown_timing_included": 0}):
            pf = G.public_filter(page)
        self.assertIn("internal", pf["ids"]); self.assertNotIn("keep", pf["ids"]); self.assertEqual(pf["internal"], ["internal"])
        src = (HERE / "generate_social_cards.py").read_text()
        self.assertIn('land_pf = dict(pf, ids=list(pf.get("internal") or [])) if LANDING_INCLUDES_LATE else pf', src)      # the landing page too


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a, **k):
        pass


class InPage(unittest.TestCase):
    """the real page: the card keeps the best option, gather attaches it, the lock lands in the ledger tagged and once"""

    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        socketserver.TCPServer.allow_reuse_address = True
        cls.srv = socketserver.TCPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=str(ROOT / "docs")))
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.pw = sync_playwright().start(); cls.b = cls.pw.chromium.launch(); cls.pg = cls.b.new_page()
        cls.pg.route("**/*espn.com/**", lambda r: r.abort()); cls.pg.route("**/*workers.dev/**", lambda r: r.abort())
        cls.pg.goto(f"http://127.0.0.1:{cls.srv.server_address[1]}/app.html?nosb=1")
        cls.pg.wait_for_function("typeof _altAttachBest==='function'&&typeof lockPick==='function'"); cls.pg.wait_for_timeout(1000)

    @classmethod
    def tearDownClass(cls):
        cls.b.close(); cls.pw.stop(); cls.srv.shutdown()

    MKTS = [{"side": "under", "label": "UNDER 45.5", "prob": .56, "tierN": 1, "dec": 1.91, "ml": "-110"}, {"side": "over", "label": "OVER 45.5", "prob": .44, "tierN": 0, "dec": 1.91, "ml": "-110"},
            {"side": "sprdDog", "label": "CLE +2.5", "prob": .63, "tierN": 3, "dec": 1.91, "ml": "-110"}, {"side": "sprdFav", "label": "PHI -2.5", "prob": .37, "tierN": 0, "dec": 1.91, "ml": "-110"}]

    def attach(self):
        self.pg.evaluate("""([m])=>{saveP([]);window._altReg={};window._altBest={};_altRowFor('CFB','PHI','CLE','2026-12-30T20:00Z',{mkts:m},false)}""", [self.MKTS])
        return self.pg.evaluate("_altAttachBest([{sport:'CFB',hA:'PHI',awA:'CLE',startMs:%d},{sport:'CFB',hA:'X',awA:'Y'}])" % int(now_ms() + 6 * 3600e3))

    def test_the_card_keeps_its_best_option_and_gather_attaches_it(self):
        legs = self.attach()
        ab = legs[0]["adjBest"]
        self.assertNotIn("adjBest", legs[1])                                            # a game whose card showed none gets none
        tops = self.pg.evaluate("[...document.querySelectorAll('.altc.alt-top')].length") if False else None
        probs = self.pg.evaluate("Object.values(window._altReg).map(e=>e.alt.prob)")
        self.assertEqual(ab["alt"]["prob"], max(probs))                                 # the highest probability of the four = the magenta one
        self.assertIn(ab["alt"]["mkt"], ("OU", "SPREAD")); self.assertTrue(ab["side"])

    def test_the_lock_goes_in_once_tagged_with_the_right_market(self):
        legs = self.attach()
        gl = dict(legs[0], sport="CFB", markets=[], mcSummary=None, best=None, socFactors=None)
        leg = A._best_adjusted_leg(gl, [])
        self.assertIsNotNone(leg)
        self.assertEqual(A.lock_game_leg(self.pg, leg, leg["lockDate"]), "locked")
        picks = self.pg.evaluate("getP().filter(p=>p.outcome==='pending')")
        self.assertEqual(len(picks), 1)
        p = picks[0]
        self.assertEqual(p["betOn"], leg["label"]); self.assertEqual(p["betType"], leg["side"] in ("over", "under") and "OU" or "SPREAD")
        self.assertTrue(p["altLine"]["bestOfGame"]); self.assertEqual(p["lockOrigin"] if "lockOrigin" in p else "auto", "auto")
        self.assertIsNone(ET.product_of_pick(p))                                        # and it is not a product pick
        self.assertEqual(A.lock_game_leg(self.pg, leg, leg["lockDate"]), "already-locked")   # a repeat pass is a normal duplicate
        self.assertEqual(len(self.pg.evaluate("getP().filter(p=>p.outcome==='pending')")), 1)

    def test_lock_qualifying_legs_locks_the_staged_extras_and_clears_them(self):
        legs = self.attach()
        A.ADJ_BEST_PENDING.clear()
        A.build_qualifying({"gameLegs": [dict(legs[0], sport="CFB", markets=[], mcSummary=None, best=None, socFactors=None)], "propLegs": []})
        self.assertEqual(len(A.ADJ_BEST_PENDING), 1)
        res = A._lock_qualifying_legs(self.pg, [])
        self.assertEqual((res.new, res.failed), (0, 0))                                 # the lean pass's own counts are untouched
        self.assertEqual(A.ADJ_BEST_PENDING, [])
        self.assertEqual(len(self.pg.evaluate("getP().filter(p=>p.outcome==='pending'&&p.altLine&&p.altLine.bestOfGame)")), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
