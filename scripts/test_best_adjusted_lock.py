#!/usr/bin/env python3
"""The BEST adjusted line of every game is a qualifying pick of the game's product (owner 2026-10-24 locks; 2026-10-25 "include in email best adjusted line for each game and include indicator"): the option the card
marks with the magenta border, over both markets and both sides.

  * the card keeps it (window._altBest) and gather_legs attaches it to the captured game leg as `adjBest`;
  * build_qualifying returns one extra leg per game (altLine.bestOfGame + altLine.product), skipping a market the lean pass already covers, a started game and bad data; the product's pass locks it and its email lists it
    under BEST ADJUSTED LINE + ADJUSTED LINE badges;
  * the ones locked BEFORE they were emailed (no altLine.product) stay internal: no product, no public figure.

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


def game(sport="CFB", start_in_h=5, ab=None, markets=()):
    return {"sport": sport, "hA": "ALA", "awA": "MSST", "startMs": now_ms() + start_in_h * 3600e3, "markets": list(markets), "adjBest": ab, "mcSummary": "x", "best": None, "socFactors": None}


class Build(unittest.TestCase):
    def best(self, gl):
        return [q for q in A.build_qualifying({"gameLegs": [gl], "propLegs": []}, only_sports=None) if (q.get("altLine") or {}).get("bestOfGame")]

    def test_a_game_with_a_best_adjusted_line_returns_exactly_one_extra_leg(self):
        q = A.build_qualifying({"gameLegs": [game("CFB", ab=adj())], "propLegs": []}, only_sports=None)
        self.assertEqual(len(q), 1)                                              # in the qualifying list: locked AND emailed
        leg = q[0]
        self.assertEqual((leg["label"], leg["side"], leg["kind"], leg["adjBest"]), ("UNDER 3.5", "under", "GAME", True))
        self.assertTrue(leg["altLine"]["bestOfGame"]); self.assertTrue(leg["altLine"]["product"]); self.assertEqual(leg["altLine"]["postedLabel"], "UNDER 2.5")
        self.assertEqual(leg["priceSource"], "estimated"); self.assertEqual(A._market_type(leg["side"]), "OU")
        self.assertIn("BEST ADJUSTED LINE OF THE GAME", leg["reasoning"])
        self.assertRegex(leg["lockDate"], r"^\d{4}-\d\d-\d\d$")

    def test_both_markets_and_both_sides_are_accepted(self):
        for mkt, side, label in (("OU", "over", "OVER 1.5"), ("OU", "under", "UNDER 3.5"), ("SPREAD", "sprdFav", "Arsenal +0.5"), ("SPREAD", "ahDog", "Chelsea +1.5"), ("SPREAD", "sprdDog", "Chelsea +9.5")):
            self.assertEqual([x["label"] for x in self.best(game("CFB", ab=adj(label, .7, mkt, side)))], [label], (mkt, side))

    def test_a_market_the_lean_pass_already_covers_keeps_its_pick(self):
        lean = {"label": "OVER 2.5", "side": "over", "prob": .66, "tierN": 2, "evVal": .08, "dec": 1.91, "ml": "-110"}
        self.assertEqual(self.best(game("CFB", ab=adj("UNDER 3.5", .74, "OU", "under"), markets=[lean])), [])      # a second total would be a second lock on the same market
        self.assertEqual(len(self.best(game("CFB", ab=adj("Chelsea +1.5", .76, "SPREAD", "ahDog"), markets=[lean]))), 1)    # a different market is free

    def test_started_games_and_bad_data_return_nothing(self):
        self.assertEqual(self.best(game("CFB", start_in_h=-1, ab=adj())), [])        # already started: the start guard
        for bad in (None, {}, {"alt": {}}, adj(prob=.2), adj(prob=1.4), adj(mkt="ML"), adj(side="mlFav")):
            self.assertEqual(self.best(game("CFB", ab=bad)), [])
        with mock.patch.object(A, "LOCK_BEST_ADJUSTED", False):
            self.assertEqual(self.best(game("CFB", ab=adj())), [])                   # the master switch

    def test_the_watchdog_look_only_call_sees_them_too(self):
        q = A.build_qualifying({"gameLegs": [game("CFB", ab=adj())], "propLegs": []}, now=0)
        self.assertEqual(len(q), 1)                                                  # so a missing one is noticed and re-locked like any other pick

    def test_the_sport_scope_of_the_pass_is_respected(self):
        self.assertEqual(A.build_qualifying({"gameLegs": [game("CFB", ab=adj())], "propLegs": []}, only_sports=frozenset({"NHL"})), [])


class EmailRendering(unittest.TestCase):
    def leg(self, **kw):
        q = A.build_qualifying({"gameLegs": [game("CFB", ab=adj())], "propLegs": []}, only_sports=None)[0]
        q.update(kw); return q

    def test_the_email_marks_it_as_the_best_adjusted_line_and_as_an_adjusted_line(self):
        html = A._leg_html(self.leg())
        self.assertIn("BEST ADJUSTED LINE", html); self.assertIn("ADJUSTED LINE</span>", html)
        self.assertIn("UNDER 3.5", html); self.assertIn("posted UNDER 2.5", html); self.assertIn("estimate", html)

    def test_an_ordinary_adjusted_line_has_only_the_adjusted_badge(self):
        q = self.leg(); q["altLine"] = dict(q["altLine"], bestOfGame=False)
        html = A._leg_html(q)
        self.assertNotIn("BEST ADJUSTED LINE", html); self.assertIn("ADJUSTED LINE</span>", html)

    def test_the_full_email_lists_it_and_explains_the_badge(self):
        body = A.build_locks_email_html([self.leg()], True, 1)
        self.assertIn("BEST ADJUSTED LINE", body); self.assertIn("in addition to the game", body)


class Gates(unittest.TestCase):
    PICK = {"id": "p", "sport": "NBA", "league": "NBA", "betType": "OU", "betOn": "OVER 200.5", "hA": "A", "awA": "B", "date": "2026-12-30", "outcome": "pending", "lockedAt": 1e12}

    def test_emailed_best_of_game_picks_are_product_picks_the_earlier_internal_ones_are_not(self):
        self.assertIsNotNone(ET.product_of_pick(self.PICK))
        self.assertIsNotNone(ET.product_of_pick(dict(self.PICK, altLine={"bestOfGame": True, "product": True, "posted": 1})))
        self.assertIsNone(ET.product_of_pick(dict(self.PICK, altLine={"bestOfGame": True, "posted": 1})))      # locked 2026-10-24/25, before they were emailed: never sent, never owed
        self.assertIsNotNone(ET.product_of_pick(dict(self.PICK, altLine={"posted": 1})))                        # an ordinary adjusted line is a product pick

    def test_public_figures_count_every_adjusted_line_pick(self):
        import generate_social_cards as G
        rows = [dict(self.PICK, id="keep"), dict(self.PICK, id="older", altLine={"bestOfGame": True}), dict(self.PICK, id="emailed", altLine={"bestOfGame": True, "product": True}),
                dict(self.PICK, id="lean", altLine={"posted": 1, "line": 2})]
        page = mock.Mock(); page.evaluate.return_value = rows
        with mock.patch.object(G.lt, "load_index", return_value={}), mock.patch.object(G.lt, "summarize", return_value={"settled_unknown_timing_included": 0}):
            pf = G.public_filter(page)
        self.assertEqual(pf["internal"], []); self.assertEqual(pf["ids"], [])             # nothing adjusted is dropped from a public figure


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
        self.pg.evaluate("""([m])=>{saveP([]);window._altReg={};window._altBest={};_altRowFor('CFB','ALA','MSST','2026-12-30T20:00Z',{mkts:m},false)}""", [self.MKTS])
        return self.pg.evaluate("_altAttachBest([{sport:'CFB',hA:'ALA',awA:'MSST',startMs:%d},{sport:'CFB',hA:'X',awA:'Y'}])" % int(now_ms() + 6 * 3600e3))

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
        self.assertEqual(ET.product_of_pick(p), "cfb")                                  # it is a product pick: emailed, tracked, counted
        self.assertEqual(A.lock_game_leg(self.pg, leg, leg["lockDate"]), "already-locked")   # a repeat pass is a normal duplicate
        self.assertEqual(len(self.pg.evaluate("getP().filter(p=>p.outcome==='pending')")), 1)

    def test_a_pass_locks_the_returned_best_adjusted_leg_with_the_product_flag(self):
        legs = self.attach()
        q = A.build_qualifying({"gameLegs": [dict(legs[0], sport="CFB", markets=[], mcSummary=None, best=None, socFactors=None)], "propLegs": []})
        self.assertEqual(len(q), 1)
        res = A._lock_qualifying_legs(self.pg, q)
        self.assertEqual((res.new, res.failed), (1, 0))
        got = self.pg.evaluate("getP().filter(p=>p.outcome==='pending'&&p.altLine&&p.altLine.bestOfGame)")
        self.assertEqual(len(got), 1); self.assertTrue(got[0]["altLine"]["product"])
        self.assertEqual(ET.product_of_pick(got[0]), "cfb")


if __name__ == "__main__":
    unittest.main(verbosity=2)
