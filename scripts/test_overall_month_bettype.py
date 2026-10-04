#!/usr/bin/env python3
"""Overall > Dashboard: neither BET TYPE list -- the month-filtered one under FILTER BY MONTH nor the all-time one below it -- may show PARLAY (owner request 2026-10-04); the other types still show."""
import functools, http.server, socketserver, threading, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOCKED = 1780000000000   # 2026-05-28 UTC -- a past month with data


def pk(i, bt, outcome="win", sport="NHL"):
    return {"id": f"p{i}", "sport": sport, "betType": bt, "betOn": "X", "hA": "AAA", "awA": "BBB", "date": "2026-05-28", "lockedAt": LOCKED + i, "settledAt": LOCKED + i + 1,
            "outcome": outcome, "decOdds": 1.9, "ml": "-110", "winProb": 0.6, "wager": 100}


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a, **k):
        pass


class MonthBetType(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        socketserver.TCPServer.allow_reuse_address = True
        cls.srv = socketserver.TCPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=str(ROOT / "docs")))
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.pw = sync_playwright().start(); cls.browser = cls.pw.chromium.launch()
        cls.pg = cls.browser.new_page(viewport={"width": 1300, "height": 900})
        cls.pg.goto(f"http://127.0.0.1:{cls.srv.server_address[1]}/app.html?nosb=1")
        cls.pg.wait_for_function("typeof renderOverall==='function'&&typeof saveP==='function'"); cls.pg.wait_for_timeout(1000)
        cls.pg.evaluate("(p)=>saveP(p)", [pk(1, "ML"), pk(2, "SPREAD", "loss"), pk(3, "OU"), pk(4, "PARLAY", "win"), pk(5, "PARLAY", "loss")])

    @classmethod
    def tearDownClass(cls):
        cls.browser.close(); cls.pw.stop(); cls.srv.shutdown()

    def cards(self):
        return self.pg.evaluate("""()=>{
          const month=[...document.querySelectorAll('.ovr-sport-breakdown')][0], all=[...document.querySelectorAll('.ovr-sport-breakdown')][1];
          const typeCard=r=>[...r.querySelectorAll('.card')].find(c=>/^\\s*BET TYPE/.test(c.innerText)).innerText;
          return {month:typeCard(month), all:typeCard(all)}}""")

    def test_no_parlay_in_either_bet_type_list(self):
        self.pg.evaluate("window._ovrSelectedMonth='2026-05';renderOverall()"); self.pg.wait_for_timeout(300)
        c = self.cards()
        self.assertNotIn("PARLAY", c["month"]); self.assertIn("ML", c["month"]); self.assertIn("SPREAD", c["month"]); self.assertIn("O/U", c["month"])
        self.assertNotIn("PARLAY", c["all"])                                # nor in the all-time list below it
        for t in ("ML", "SPREAD", "O/U"):
            self.assertIn(t, c["all"])

    def test_all_time_section_has_its_own_header(self):
        self.pg.evaluate("window._ovrSelectedMonth='2026-05';renderOverall()"); self.pg.wait_for_timeout(300)
        r = self.pg.evaluate("""()=>{const h=document.getElementById('ovr-alltime-breakdown-lbl');const grids=[...document.querySelectorAll('.ovr-sport-breakdown')];
          return {text:h&&h.innerText, headerIsRightBeforeAllTimeGrid:h&&h.nextElementSibling===grids[1], monthHeader:/MAY 2026 — BY SPORT \/ BET TYPE \/ LEAGUE/i.test(document.getElementById('ovr-dashboard').innerText)}}""")
        self.assertEqual(r["text"], "ALL TIME — BY SPORT / BET TYPE / LEAGUE")
        self.assertTrue(r["headerIsRightBeforeAllTimeGrid"]); self.assertTrue(r["monthHeader"])      # and the month section keeps its own header

    def test_selected_month_really_is_the_one_with_the_picks(self):
        self.pg.evaluate("window._ovrSelectedMonth='2026-05';renderOverall()"); self.pg.wait_for_timeout(300)
        self.assertIn("MAY 2026", self.pg.evaluate("document.getElementById('ovr-month-select').selectedOptions[0].text").upper())


if __name__ == "__main__":
    unittest.main(verbosity=2)
