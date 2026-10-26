#!/usr/bin/env python3
"""Soccer moneyline floor (owner 2026-10-26): a Premier League / La Liga / Serie A moneyline whose STATED win probability is below 65% is never auto-locked or emailed (Champions League is exempt;
O/U and goal spreads are untouched; a 75%+ moneyline is above the floor anyway).  The same floor grades the app's own card SKIP.  Python and JS constants must stay equal.

    python3 scripts/test_soccer_ml_floor.py
"""
from __future__ import annotations

import functools, http.server, os, re, socketserver, sys, threading, time, unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = Path(os.environ.get("CV_ROOT") or HERE.parent)
sys.path.insert(0, str(HERE))
import auto_lock_settle as A  # noqa: E402


def leg(sport, side, prob, label="Home Win"):
    return {"sport": sport, "hA": "HOM", "awA": "AWY", "startMs": time.time() * 1000 + 5 * 3600e3, "markets": [
        {"side": side, "label": label, "prob": prob, "tierN": 2, "tier": "OPTIMAL", "ml": "-110", "dec": 1.91, "priceSource": None}], "mcSummary": "x", "best": None, "socFactors": None}


def kept(sport, side, prob):
    return A.build_qualifying({"gameLegs": [leg(sport, side, prob)], "propLegs": []}, only_sports=None)


class Floor(unittest.TestCase):
    def test_domestic_leagues_drop_ml_below_the_floor(self):
        for sport in ("SOC_PL", "SOC_LIGA", "SOC_ITA"):
            self.assertEqual(kept(sport, "home", .61), [], sport)
            self.assertEqual(kept(sport, "away", .649), [], sport)
            self.assertEqual(len(kept(sport, "home", .65)), 1, sport)
            self.assertEqual(len(kept(sport, "home", .72)), 1, sport)

    def test_champions_league_is_exempt(self):
        self.assertEqual(len(kept("SOC_CL", "home", .60)), 1)

    def test_totals_and_goal_spreads_are_untouched(self):
        for side in ("over", "under", "ahFav", "ahDog"):
            self.assertEqual(len(kept("SOC_PL", side, .60)), 1, side)

    def test_python_and_app_constants_match(self):
        html = (ROOT / "docs" / "app.html").read_text()
        m = re.search(r"var SOC_ML_FLOOR=([0-9.]+),SOC_ML_FLOOR_LEAGUES=\{([^}]*)\}", html)
        self.assertTrue(m, "app constants not found")
        self.assertEqual(float(m.group(1)), A.SOCCER_ML_FLOOR)
        leagues = {"SOC_" + k.split(":")[0].strip().upper() for k in m.group(2).split(",")}
        self.assertEqual(leagues, set(A.SOCCER_ML_FLOOR_SPORTS))
        self.assertTrue(A.SOCCER_ML_FLOOR_SPORTS <= A.EURO_SOCCER_SPORTS and "SOC_CL" not in A.SOCCER_ML_FLOOR_SPORTS)


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a, **k):
        pass


class AppGrade(unittest.TestCase):
    """The app's own grade (card pill + the rows the lock pipeline reads) follows the same floor."""
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        socketserver.TCPServer.allow_reuse_address = True
        cls.srv = socketserver.TCPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=str(ROOT / "docs")))
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch()
        cls.pg = cls.browser.new_page()
        for pat in ("**/*espn*/**", "**/*workers.dev*", "**/*open-meteo*/**", "**/*nominatim*/**"):
            cls.pg.route(pat, lambda r: r.abort())
        cls.pg.goto(f"http://127.0.0.1:{cls.srv.server_address[1]}/app.html?nosb=1&nosw=1")
        cls.pg.wait_for_function("typeof _evalMkts==='function'&&typeof _socGC==='function'")

    @classmethod
    def tearDownClass(cls):
        cls.browser.close(); cls.pw.stop(); cls.srv.shutdown()

    def tiers(self, league, prob, side="home"):
        return self.pg.evaluate("""([league,p,side])=>{
          const r=_evalMkts([{label:'X Win',prob:p,evVal:ev(p,1.909),side:side}],league);
          return {tierN:r.mkts[0].tierN,pill:_socGC(p,league.replace('SOC_','').toLowerCase()).lb}}""", [league, prob, side])

    def test_card_grade_follows_the_floor(self):
        for lg in ("SOC_PL", "SOC_LIGA", "SOC_ITA"):
            low = self.tiers(lg, .63)
            self.assertEqual((low["tierN"], low["pill"]), (0, "SKIP"), lg)
            hi = self.tiers(lg, .68)
            self.assertGreaterEqual(hi["tierN"], 2, lg); self.assertNotEqual(hi["pill"], "SKIP", lg)

    def test_champions_league_and_other_markets_keep_their_grade(self):
        self.assertGreaterEqual(self.tiers("SOC_CL", .63)["tierN"], 2)
        self.assertGreaterEqual(self.tiers("SOC_PL", .63, "over")["tierN"], 1)


if __name__ == "__main__":
    unittest.main()
