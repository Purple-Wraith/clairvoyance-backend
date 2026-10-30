#!/usr/bin/env python3
"""BEST ADJUSTED TOTAL (owner 2026-10-27: "lock the best total when the best of game is a spread ... to give more option and to run analysis to see how they both perform").

  * the card keeps the game's best adjusted TOTAL (window._altBestOU) only when its best adjusted line (window._altBest) is a SPREAD; gather_legs attaches it as `adjBestOU`;
  * build_qualifying returns it as one more product leg tagged altLine.bestTotal (never bestOfGame), unless the lean pass already has a total for that game, the game started, or the best line is a total;
  * the email marks it BEST ADJUSTED TOTAL; scripts/alt_line_scorecard.py --best grades it as its own group.

    python3 scripts/test_best_adjusted_total.py
"""
from __future__ import annotations

import functools, http.server, os, socketserver, sys, threading, time, unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
ROOT = Path(os.environ.get("CV_ROOT") or HERE.parent)
sys.path.insert(0, str(HERE))
import auto_lock_settle as A  # noqa: E402
import email_tracking as ET  # noqa: E402


def now_ms():
    return time.time() * 1000


def opt(label, prob, mkt, side, posted_label):
    return {"key": "k", "sport": "NBA", "hA": "BOS", "awA": "NY", "date": "2026-12-30", "side": side,
            "alt": {"label": label, "prob": prob, "mkt": mkt, "ml": "-266", "dec": 1.376, "posted": 0, "line": 0, "shift": 6.0 if mkt == "SPREAD" else 8.0,
                    "postedLabel": posted_label, "postedProb": .52, "postedDec": 1.91, "postedMl": "-110"}}


SPREAD = opt("NY +9.5", .682, "SPREAD", "sprdDog", "NY +3.5")
TOTAL = opt("OVER 222.5", .670, "OU", "over", "OVER 230.5")


def game(ab=SPREAD, abou=TOTAL, start_in_h=5, markets=()):
    return {"sport": "NBA", "hA": "BOS", "awA": "NY", "startMs": now_ms() + start_in_h * 3600e3, "markets": list(markets), "adjBest": ab, "adjBestOU": abou,
            "mcSummary": "x", "best": None, "socFactors": None}


def build(gl):
    # _beyond_horizon off: the fake game starts 5h from now, which after ~7 PM MT is TOMORROW's MT date and would be dropped by the same-day rule (the horizon is not what this file tests)
    with mock.patch.object(A, "_nba_preseason_pairs", lambda: set()), mock.patch.object(A, "_nba_regular_season_start", lambda: "2000-01-01"), \
            mock.patch.object(A, "_beyond_horizon", lambda *a, **k: False):
        return A.build_qualifying({"gameLegs": [gl], "propLegs": []}, only_sports=frozenset({"NBA"}))


def kinds(q):
    return sorted("total" if (x.get("altLine") or {}).get("bestTotal") else "line" if (x.get("altLine") or {}).get("bestOfGame") else "lean" for x in q)


class Build(unittest.TestCase):
    def test_best_spread_game_also_locks_its_best_total(self):
        q = build(game())
        self.assertEqual(kinds(q), ["line", "total"])
        t = next(x for x in q if x["altLine"].get("bestTotal"))
        self.assertEqual((t["label"], t["side"], A._market_type(t["side"])), ("OVER 222.5", "over", "OU"))
        self.assertTrue(t["altLine"]["product"]); self.assertNotIn("bestOfGame", t["altLine"])
        self.assertIn("BEST ADJUSTED TOTAL OF THE GAME", t["reasoning"]); self.assertEqual(t["priceSource"], "estimated")

    def test_only_when_the_best_line_is_a_spread(self):
        self.assertEqual(kinds(build(game(ab=TOTAL, abou=TOTAL))), ["line"])
        self.assertEqual(kinds(build(game(ab=None))), [])

    def test_lean_total_keeps_its_pick_and_the_spread_lean_does_not_block_it(self):
        lean_ou = {"label": "UNDER 230.5", "side": "under", "prob": .63, "tierN": 2, "evVal": .2, "dec": 1.91, "ml": "-110"}
        self.assertEqual(kinds(build(game(markets=[lean_ou]))), ["lean", "line"])        # one lock per market per game
        lean_sp = {"label": "NY +3.5", "side": "sprdDog", "prob": .63, "tierN": 2, "evVal": .2, "dec": 1.91, "ml": "-110"}
        self.assertEqual(kinds(build(game(markets=[lean_sp]))), ["lean", "total"])       # the spread lean replaces the best line, the best total still goes in

    def test_started_bad_data_and_switch(self):
        self.assertEqual(build(game(start_in_h=-1)), [])
        for bad in (None, {}, {"alt": {}}, opt("OVER 222.5", .2, "OU", "over", "x"), opt("NY +9.5", .7, "SPREAD", "sprdDog", "x")):
            self.assertEqual(kinds(build(game(abou=bad))), ["line"], bad)
        with mock.patch.object(A, "LOCK_BEST_ADJUSTED_TOTAL", False):
            self.assertEqual(kinds(build(game())), ["line"])

    def test_email_badge_and_product(self):
        t = next(x for x in build(game()) if x["altLine"].get("bestTotal"))
        html = A._leg_html(t)
        self.assertIn("BEST ADJUSTED TOTAL", html); self.assertNotIn("BEST ADJUSTED LINE", html); self.assertIn("ADJUSTED LINE</span>", html)
        pick = {"id": "p", "sport": "NBA", "league": "NBA", "betType": "OU", "betOn": "OVER 222.5", "hA": "BOS", "awA": "NY", "date": "2026-12-30", "outcome": "pending",
                "lockedAt": 1e12, "altLine": t["altLine"]}
        self.assertEqual(ET.product_of_pick(pick), "nba")


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a, **k):
        pass


class InPage(unittest.TestCase):
    """the real card code: the best total is kept only beside a best-line spread, and gather attaches it"""

    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        socketserver.TCPServer.allow_reuse_address = True
        cls.srv = socketserver.TCPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=str(ROOT / "docs")))
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.pw = sync_playwright().start(); cls.b = cls.pw.chromium.launch(); cls.pg = cls.b.new_page()
        cls.pg.route("**/*espn.com/**", lambda r: r.abort()); cls.pg.route("**/*workers.dev/**", lambda r: r.abort())
        cls.pg.goto(f"http://127.0.0.1:{cls.srv.server_address[1]}/app.html?nosb=1")
        cls.pg.wait_for_function("typeof _altAttachBest==='function'&&typeof _altRowFor==='function'"); cls.pg.wait_for_timeout(1000)

    @classmethod
    def tearDownClass(cls):
        cls.b.close(); cls.pw.stop(); cls.srv.shutdown()

    def run_card(self, mkts):
        self.pg.evaluate("""([m])=>{window._altReg={};window._altBest={};window._altBestOU={};_altRowFor('NBA','BOS','NY','2026-12-30T00:00Z',{mkts:m},false)}""", [mkts])
        return self.pg.evaluate("_altAttachBest([{sport:'NBA',hA:'BOS',awA:'NY'}])")[0]

    def test_nba_game_best_spread_with_best_total(self):
        mk = [{"side": "under", "label": "UNDER 230.5", "prob": .56, "tierN": 1, "dec": 1.91, "ml": "-110"}, {"side": "over", "label": "OVER 230.5", "prob": .44, "tierN": 0, "dec": 1.91, "ml": "-110"},
              {"side": "sprdDog", "label": "NY +3.5", "prob": .55, "tierN": 1, "dec": 1.91, "ml": "-110"}, {"side": "sprdFav", "label": "BOS -3.5", "prob": .45, "tierN": 0, "dec": 1.91, "ml": "-110"}]
        gl = self.run_card(mk)
        self.assertEqual(gl["adjBest"]["alt"]["mkt"], "SPREAD")                          # NBA: the spread table (68.2%) beats the totals table (67.0%)
        self.assertEqual(gl["adjBestOU"]["alt"]["mkt"], "OU")
        ou_probs = self.pg.evaluate("Object.values(window._altReg).filter(e=>e.alt.mkt==='OU').map(e=>e.alt.prob)")
        self.assertEqual(gl["adjBestOU"]["alt"]["prob"], max(ou_probs))

    def test_no_best_total_when_the_game_has_no_total(self):
        mk = [{"side": "sprdDog", "label": "NY +3.5", "prob": .55, "tierN": 1, "dec": 1.91, "ml": "-110"}, {"side": "sprdFav", "label": "BOS -3.5", "prob": .45, "tierN": 0, "dec": 1.91, "ml": "-110"}]
        gl = self.run_card(mk)
        self.assertEqual(gl["adjBest"]["alt"]["mkt"], "SPREAD"); self.assertNotIn("adjBestOU", gl)

    def test_no_best_total_when_the_best_line_is_a_total(self):
        mk = [{"side": "under", "label": "UNDER 230.5", "prob": .56, "tierN": 1, "dec": 1.91, "ml": "-110"}, {"side": "over", "label": "OVER 230.5", "prob": .44, "tierN": 0, "dec": 1.91, "ml": "-110"}]
        gl = self.run_card(mk)
        self.assertEqual(gl["adjBest"]["alt"]["mkt"], "OU"); self.assertNotIn("adjBestOU", gl)


if __name__ == "__main__":
    unittest.main(verbosity=2)
