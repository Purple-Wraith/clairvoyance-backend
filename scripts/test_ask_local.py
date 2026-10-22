#!/usr/bin/env python3
"""Ask Clairvoyance (local assistant, 2026-10-22): rule-based answers from the page's own data -- no API, no network.  Seeds a small known ledger and checks the answers' numbers.

    python3 scripts/test_ask_local.py
"""
import functools, http.server, json, socketserver, threading, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a, **k):
        pass


def pick(i, sport, bt, on, outcome, dec=2.0, prob=0.6, date=None, **kw):
    d = {"id": f"t{i}", "date": date or "2026-10-01", "sport": sport, "league": sport, "betType": bt, "betOn": on, "outcome": outcome, "decOdds": dec, "winProb": prob,
         "hA": "HHH", "awA": "AAA", "ml": "+100", "lockedAt": 1790000000000 + i}
    d.update(kw)
    return d


LEDGER = [
    pick(1, "NHL", "OU", "UNDER 6.5", "win", 2.0), pick(2, "NHL", "OU", "UNDER 5.5", "loss"), pick(3, "NHL", "OU", "OVER 6.5", "win", 1.5),
    pick(4, "NHL", "ML", "HHH ML", "win", 1.8), pick(5, "NBA", "SPREAD", "AAA +7.5", "loss"), pick(6, "NBA", "ML", "HHH ML", "win", 1.5),
    pick(7, "NBA", "PROP", "Nikola Jokic UNDER REB 13.5", "pending", 1.5, 0.652, date="__TODAY__", player="Nikola Jokic", reasoning="Because the numbers say so."),
    pick(8, "NBA", "PROP", "Test Guy OVER PTS 19.5", "win", 1.4, date="__TODAY__", altLine={"posted": 24.5, "line": 19.5, "shift": 5, "postedLabel": "Test Guy OVER PTS 24.5"}),
]


class Ask(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        socketserver.TCPServer.allow_reuse_address = True
        cls.srv = socketserver.TCPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=str(ROOT / "docs")))
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.pw = sync_playwright().start()
        cls.b = cls.pw.chromium.launch()
        cls.pg = cls.b.new_page(viewport={"width": 1300, "height": 900})
        cls.errors, cls.requests = [], []
        cls.pg.on("pageerror", lambda e: cls.errors.append(str(e)))
        cls.pg.goto(f"http://127.0.0.1:{cls.srv.server_address[1]}/app.html?nosb=1&chat=1")
        cls.pg.wait_for_function("typeof _chatAnswer==='function'&&typeof getP==='function'")
        cls.pg.wait_for_timeout(1500)
        cls.pg.evaluate("(r)=>{const t=today();saveP(r.map(p=>Object.assign(p,{date:p.date==='__TODAY__'?t:p.date})))}", LEDGER)
        cls.pg.on("request", lambda r: cls.requests.append(r.url))

    @classmethod
    def tearDownClass(cls):
        cls.b.close(); cls.pw.stop(); cls.srv.shutdown()

    def ask(self, q):
        return self.pg.evaluate("(q)=>_chatAnswer(q)", q)["text"]

    def test_record_for_a_sport_and_side(self):
        a = self.ask("How are NHL UNDERs doing?")
        self.assertIn("1-1 (50%)", a)                       # two NHL unders, one win one loss
        self.assertIn("+0u", a)
        self.assertIn("NHL UNDERS", a)

    def test_units_follow_the_flat_unit_rule(self):
        a = self.ask("NHL record")                           # wins at 2.0, 1.5, 1.8 and one loss -> 3-1, +1 + .5 + .8 - 1
        self.assertIn("3-1 (75%)", a)
        self.assertIn("+1.3u", a)

    def test_group_by_sport_is_a_table_sorted_best_first(self):
        a = self.ask("record by sport")
        self.assertIn("| SPORT |", a)
        self.assertLess(a.index("| NHL |"), a.index("| NBA |"))

    def test_props_record_uses_the_bet_type(self):
        a = self.ask("NBA props record")
        self.assertIn("1-0", a)

    def test_locked_today_lists_todays_picks(self):
        a = self.ask("What is locked today?")
        self.assertIn("2 picks locked today", a)
        self.assertIn("Nikola Jokic UNDER REB 13.5", a)

    def test_pending_bets(self):
        a = self.ask("pending bets")
        self.assertIn("1 pending pick", a)

    def test_why_a_pick_shows_the_stored_reasoning(self):
        a = self.ask("why Jokic UNDER?")
        self.assertIn("Nikola Jokic UNDER REB 13.5", a)
        self.assertIn("Because the numbers say so.", a)
        self.assertIn("65.2%", a)

    def test_why_an_adjusted_pick_names_the_posted_line(self):
        a = self.ask("why Test Guy")
        self.assertIn("Adjusted line", a)
        self.assertIn("24.5", a)

    def test_glossary(self):
        self.assertIn("ADJ. LINE", self.ask("what is an adjusted line?"))
        self.assertIn("value grade", self.ask("what does SKIP mean?"))
        self.assertIn("1 unit per pick", self.ask("what are units?"))

    def test_unanswerable_says_so_and_does_not_guess(self):
        a = self.ask("tell me a joke")
        self.assertIn("can't answer", a)
        self.assertNotIn("Jokerit", a)

    def test_a_team_name_in_two_leagues_answers_for_both(self):
        a = self.ask("How is Toronto doing?")
        self.assertIn("(NBA)", a)
        self.assertIn("(NHL)", a)

    def test_follow_up_word_is_not_a_team(self):
        a = self.ask("best sport")
        self.assertIn("| SPORT |", a)

    def test_ui_opens_answers_and_never_calls_out(self):
        pg = self.pg
        self.assertTrue(pg.evaluate("document.getElementById('cv-ask-fab').classList.contains('on')"))
        n = len(self.requests)
        pg.click("#cv-ask-fab")
        pg.fill("#ask-text", "How are NHL UNDERs doing?")
        pg.press("#ask-text", "Enter")
        pg.wait_for_function("document.getElementById('ask-log').innerText.includes('NHL UNDERS')")
        self.assertIn("NHL UNDERS", pg.inner_text("#ask-log"))
        self.assertEqual([u for u in self.requests[n:] if "anthropic" in u or "/chat" in u], [])
        self.assertEqual(self.errors, [])

    def test_no_secret_or_api_code_is_left_in_the_page(self):
        src = (ROOT / "docs" / "app.html").read_text()
        self.assertNotIn("api.anthropic.com", src)
        self.assertNotIn("x-owner-key'||''", src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
