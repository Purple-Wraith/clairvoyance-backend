#!/usr/bin/env python3
"""Settled rows in ALL BETS (docs/app.html _pickRowHTML) show where the result finished against the line, win / loss / push alike:
a player prop -> "ACTUAL: 31 PTS (line 25.5)", an over/under -> "FINAL TOTAL: 5 (over 4.5)", a spread -> "FINAL SPREAD: Los Angeles Lakers -9 / Denver Nuggets +9 (pick Los Angeles Lakers -3.5)".

    python3 scripts/test_pick_result_lines.py
"""
import functools, http.server, os, re, socketserver, threading, unittest
from pathlib import Path

ROOT = Path(os.environ.get("CV_ROOT") or Path(__file__).resolve().parent.parent)


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a, **k):
        pass


def pick(bet, outcome="win", **kw):
    return {"id": "x1", "sport": "NBA", "league": "NBA", "betType": "ML", "betOn": bet, "hA": "LAL", "awA": "DEN", "date": "2026-10-23",
            "outcome": outcome, "lockedAt": 1, "winProb": .6, "decOdds": 1.9, **kw}


class Lines(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        socketserver.TCPServer.allow_reuse_address = True
        cls.srv = socketserver.TCPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=str(ROOT / "docs")))
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.pw = sync_playwright().start(); cls.b = cls.pw.chromium.launch(); cls.pg = cls.b.new_page()
        cls.pg.route("**/*espn.com/**", lambda r: r.abort()); cls.pg.route("**/*workers.dev/**", lambda r: r.abort())
        cls.pg.goto(f"http://127.0.0.1:{cls.srv.server_address[1]}/app.html?nosb=1")
        cls.pg.wait_for_function("typeof _pickRowHTML==='function'&&typeof _propActual==='function'"); cls.pg.wait_for_timeout(800)

    @classmethod
    def tearDownClass(cls):
        cls.b.close(); cls.pw.stop(); cls.srv.shutdown()

    def side(self, p, mode="settled"):
        """text of the right-hand column of the row"""
        return self.pg.evaluate("""([p,m])=>{const d=document.createElement('div');d.innerHTML=_pickRowHTML(p,{mode:m});return d.querySelector('.prow2-side').innerText.replace(/\\s+/g,' ')}""", [p, mode])

    # ── props ──
    def test_a_prop_shows_the_players_final_whether_it_won_or_lost(self):
        for oc in ("win", "loss"):
            t = self.side(pick("Anthony Edwards OVER PTS 25.5", oc, betType="PROP", playerResult=31 if oc == "win" else 19))
            self.assertIn("ACTUAL: " + ("31" if oc == "win" else "19") + " PTS (line 25.5)", t)
            self.assertNotIn("FINAL: —", t)

    def test_a_push_prop_shows_it_too(self):
        self.assertIn("ACTUAL: 28 PTS (line 28)", self.side(pick("Nikola Jokic OVER PTS 28", "push", betType="PROP", playerResult=28)))

    def test_multiword_and_combo_stats_keep_their_label(self):
        self.assertIn("ACTUAL: 12 FACEOFFS WON", self.side(pick("Jordan Staal OVER Faceoffs Won 11.5", betType="PROP", playerResult=12, sport="NHL")))
        self.assertIn("ACTUAL: 17 PTS+REB", self.side(pick("Skylar Diggins OVER PTS+REB 14.5", betType="PROP", playerResult=17)))

    def test_hand_entered_text_results_use_the_number_when_there_is_one(self):
        self.assertIn("ACTUAL: 30 PTS", self.side(pick("Jalen Brunson OVER PTS 21.5", betType="PROP", playerResult="had 30 pts")))
        t = self.side(pick("Mikal Bridges OVER PTS 14.5", "loss", betType="PROP", playerResult="under 14.5"))
        self.assertNotIn("ACTUAL", t)

    def test_a_prop_with_no_stored_result_and_a_pending_prop_show_nothing_extra(self):
        self.assertNotIn("ACTUAL", self.side(pick("A B OVER PTS 10.5", betType="PROP")))
        self.assertNotIn("ACTUAL", self.side(pick("A B OVER PTS 10.5", "pending", betType="PROP", playerResult=12), mode="pending"))

    # ── game lines ──
    def test_over_under_shows_the_final_total_against_the_line(self):
        self.assertIn("FINAL TOTAL: 227 (over 224.5)", self.side(pick("OVER 224.5", "win", hScore=118, aScore=109)))
        self.assertIn("FINAL TOTAL: 227 (under 230.5)", self.side(pick("UNDER 230.5", "win", hScore=118, aScore=109)))
        self.assertIn("FINAL TOTAL: 227 (over 230.5)", self.side(pick("OVER 230.5", "loss", hScore=118, aScore=109)))

    def test_a_shootout_total_uses_the_graded_regulation_plus_overtime_total(self):
        self.assertIn("FINAL TOTAL: 4 (over 4.5)", self.side(pick("OVER 4.5", "loss", sport="LIIGA", hScore=3, aScore=2, ouTotal=4)))

    def test_spread_shows_the_final_margin_for_the_picked_team(self):
        self.assertIn("FINAL SPREAD: Los Angeles Lakers -9 / Denver Nuggets +9 (pick Los Angeles Lakers -3.5)", self.side(pick("LAL -3.5", "win", hScore=118, aScore=109)))
        self.assertIn("FINAL SPREAD: Los Angeles Lakers -9 / Denver Nuggets +9 (pick Denver Nuggets +3.5)", self.side(pick("DEN +3.5", "loss", hScore=118, aScore=109)))
        self.assertIn("FINAL SPREAD: Los Angeles Lakers -3 / Denver Nuggets +3 (pick Los Angeles Lakers -3)", self.side(pick("LAL -3", "push", hScore=118, aScore=115)))

    def test_moneylines_show_the_winning_or_losing_margin(self):
        self.assertIn("FINAL SPREAD: Los Angeles Lakers -9 / Denver Nuggets +9 (pick Los Angeles Lakers ML)", self.side(pick("LAL ML", "win", hScore=118, aScore=109)))
        self.assertIn("FINAL SPREAD: Los Angeles Lakers -9 / Denver Nuggets +9 (pick Denver Nuggets ML)", self.side(pick("DEN", "loss", hScore=118, aScore=109)))      # a bare team label is a moneyline too
        self.assertNotIn("FINAL TOTAL", self.side(pick("LAL ML", "win", hScore=118, aScore=109)))

    def test_the_loser_of_the_game_carries_the_plus_and_a_tie_reads_even(self):
        self.assertIn("FINAL SPREAD: Denver Nuggets -4 / Los Angeles Lakers +4 (pick Los Angeles Lakers +6.5)", self.side(pick("LAL +6.5", "win", hScore=100, aScore=104)))
        self.assertIn("FINAL SPREAD: LAL / DEN even (pick LAL ML)", self.side(pick("LAL ML", "push", sport="PL", hScore=1, aScore=1)))

    def test_a_prop_whose_player_did_not_play_says_so(self):
        t = self.side(pick("A B OVER PTS 10.5", "push", betType="PROP", playerResult=None, voidReason="DNP"))
        self.assertIn("ACTUAL: DID NOT PLAY (void)", t)

    def test_unscored_rows_are_unchanged(self):
        t = self.side(pick("OVER 224.5", "win"))
        self.assertIn("FINAL: —", t); self.assertNotIn("FINAL TOTAL", t)

    def test_the_team_score_line_is_still_there(self):
        self.assertIn("FINAL: DEN 109 – 118 LAL", re.sub(r"\s+", " ", self.side(pick("OVER 224.5", "win", hScore=118, aScore=109))).replace("Denver Nuggets", "DEN").replace("Los Angeles Lakers", "LAL"))


class GoingForward(unittest.TestCase):
    """Every automatic settler stores what the result lines above are built from, so new settlements always show them."""

    def test_every_settler_stores_the_final_score_or_the_players_stat(self):
        src = (ROOT / "docs" / "app.html").read_text()
        hits = [m.start() for m in re.finditer(r"\w+\.outcome=outcome;\w+\.settledAt=Date\.now\(\);", src)]
        self.assertGreaterEqual(len(hits), 10)                                           # the ESPN / schedule-file settlers
        for i in hits:
            self.assertTrue(re.search(r"hScore=|playerResult=", src[i:i + 140]), "a settler records an outcome without the final score: " + src[i:i + 140])

    def test_the_prop_settler_saves_the_players_stat_with_the_outcome(self):
        src = (ROOT / "docs" / "app.html").read_text()
        i = src.index("const upd={};pending.forEach")
        self.assertIn("playerResult:p.playerResult", src[i:i + 300])


if __name__ == "__main__":
    unittest.main(verbosity=2)
