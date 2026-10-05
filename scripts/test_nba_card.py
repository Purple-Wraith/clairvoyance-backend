#!/usr/bin/env python3
"""_nbaGameCard() regression tests. It threw `ReferenceError: espnSprdDetails is not defined` for EVERY NBA game (a refactor deleted the declaration but not its reader;
the throw was swallowed by gather_legs' try/catch so the NBA tab said "ESPN UNAVAILABLE" and the lock pipeline saw zero NBA legs, silently). Also: moneyline markets carry the
price the card shows (they used to lock at a placeholder -110), and NBA_TONIGHT no longer starts as a hardcoded June 2026 Finals game.

    python3 scripts/test_nba_card.py
"""
import functools, http.server, json, socketserver, sys, threading, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def event(h, a, details=None, ou=None, hml=None, aml=None, state="pre", season_type=2):
    odds = {}
    if details: odds["details"] = details
    if ou: odds["overUnder"] = ou
    if hml is not None: odds["homeTeamOdds"] = {"moneyLine": hml}
    if aml is not None: odds["awayTeamOdds"] = {"moneyLine": aml}
    return {"id": "t1", "date": "2026-10-23T01:30Z", "season": {"type": season_type},
            "competitions": [{"competitors": [{"homeAway": "home", "team": {"abbreviation": h, "displayName": h}, "score": "0"},
                                              {"homeAway": "away", "team": {"abbreviation": a, "displayName": a}, "score": "0"}],
                              "status": {"type": {"state": state}}, "odds": [odds] if odds else []}]}


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a, **k):
        pass


class NbaCard(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        socketserver.TCPServer.allow_reuse_address = True
        cls.srv = socketserver.TCPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=str(ROOT / "docs")))
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch()
        cls.pg = cls.browser.new_page(viewport={"width": 1300, "height": 900})
        cls.errors = []
        cls.pg.on("pageerror", lambda e: cls.errors.append(str(e)))
        cls.pg.goto(f"http://127.0.0.1:{cls.srv.server_address[1]}/app.html?nosb=1")
        cls.pg.wait_for_function("typeof _nbaGameCard==='function'&&typeof NBA_TONIGHT!=='undefined'")
        cls.pg.wait_for_timeout(1500)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close(); cls.pw.stop(); cls.srv.shutdown()

    def card(self, ev):
        return self.pg.evaluate("(e)=>{window._autoLockLegs=[];const h=_nbaGameCard(e);return {html:h,legs:window._autoLockLegs}}", ev)

    def test_card_renders_for_a_market_spread_and_captures_legs(self):
        out = self.card(event("NY", "BOS", "NY -7.5", 224.5, -320, 260))
        self.assertGreater(len(out["html"]), 500)
        self.assertEqual(self.errors, [])
        self.assertEqual(len(out["legs"]), 1)

    def test_card_renders_with_no_odds_at_all(self):
        out = self.card(event("LAL", "DEN"))
        self.assertGreater(len(out["html"]), 300)
        self.assertEqual(self.errors, [])

    def test_card_renders_for_a_pickem(self):
        out = self.card(event("LAL", "DEN", "EVEN", 228.5, -110, -110))
        self.assertGreater(len(out["html"]), 300)
        self.assertEqual(self.errors, [])

    def test_moneyline_markets_carry_the_displayed_price(self):
        out = self.card(event("MIA", "CHA", "MIA -4.5", 226.5, -190, 160))
        mk = {m["side"]: m for m in out["legs"][0]["markets"]}
        ml = [m for s, m in mk.items() if s in ("mlFav", "mlDog")]
        self.assertEqual(len(ml), 2)
        for m in ml:
            self.assertIn(m["ml"], ("-190", "+160"), m)
            self.assertGreater(m["dec"], 1.0)

    def test_nba_tonight_is_empty_until_real_data_arrives(self):
        src = (ROOT / "docs" / "app.html").read_text()
        self.assertIn("var NBA_TONIGHT=[];", src)
        self.assertNotIn("NBA FINALS G5 · June 13 2026", src)
        self.pg.evaluate("patchNBATonight([])")
        self.assertEqual(self.pg.evaluate("NBA_TONIGHT.length"), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
