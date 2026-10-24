#!/usr/bin/env python3
"""A game card whose game is final sinks to the bottom of its card list (any sport or league), docs/app.html.

    python3 scripts/test_finals_sink.py

The painter (_cvPaintCardScores) matches every card to the live layer (_LV.games + the European relay games) on both team labels and
flags a final one data-cv-final; _cvSinkFinals moves flagged cards after the live and upcoming ones inside their run of consecutive
cards.  Cards below are built with the real header structure (.gc > .gch > .gtm x2 + .np).
"""
import functools, http.server, os, socketserver, threading, unittest
from pathlib import Path

ROOT = Path(os.environ.get("CV_ROOT") or Path(__file__).resolve().parent.parent)


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a, **k):
        pass


def game(away, home, state, hs=None, as_=None, completed=None):
    return {"k": "nhl", "tag": "NHL", "id": f"{away}-{home}", "away": away, "home": home, "state": state,
            "awayScore": as_, "homeScore": hs, "note": "", "startMs": 1, "completed": completed if completed is not None else state == "post"}


GAMES = {
    "A": game("Alpha", "Bravo", "post", 3, 2),        # final
    "B": game("Charlie", "Delta", "in", 1, 1),        # live
    "C": game("Echo", "Foxtrot", "pre"),              # upcoming
    "D": game("Golf", "Hotel", "post", 4, 1),         # final
    "E": game("India", "Juliet", "post", 0, 0, completed=False),   # postponed: not a final
}


def card(away, home, grade="pn"):
    return (f'<div class="gc" data-t="{away}"><div class="gch"><div><span class="gtm">{away}</span> @ <span class="gtm">{home}</span></div>'
            f'<div class="np">7:00 PM</div></div><span class="chip">x</span><div class="bmk2 {grade}"></div></div>')


class Sink(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        socketserver.TCPServer.allow_reuse_address = True
        cls.srv = socketserver.TCPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=str(ROOT / "docs")))
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.pw = sync_playwright().start(); cls.b = cls.pw.chromium.launch(); cls.pg = cls.b.new_page()
        cls.pg.route("**/*espn.com/**", lambda r: r.abort())
        cls.pg.route("**/*workers.dev/**", lambda r: r.abort())
        cls.pg.goto(f"http://127.0.0.1:{cls.srv.server_address[1]}/app.html?nosb=1")
        cls.pg.wait_for_function("typeof _cvPaintCardScores==='function'&&typeof _LV!=='undefined'"); cls.pg.wait_for_timeout(1000)

    @classmethod
    def tearDownClass(cls):
        cls.b.close(); cls.pw.stop(); cls.srv.shutdown()

    def build(self, parts, games):
        """parts: list of ('card', away, home[, grade]) or ('h', text); games: list of _LV.games rows."""
        html = "".join(card(*x[1:]) if x[0] == "card" else f'<div class="hd">{x[1]}</div>' for x in parts)
        self.pg.evaluate("""([html,games])=>{document.getElementById('tst')?.remove();const d=document.createElement('div');d.id='tst';d.innerHTML=html;
          document.querySelector('.spane')?.appendChild(d);_LV.games=games;}""", [html, games])

    def paint(self):
        self.pg.evaluate("_cvPaintCardScores()")

    def order(self):
        return self.pg.evaluate("[...document.querySelectorAll('#tst > *')].map(c=>c.dataset.t||c.textContent)")

    def cards(self):
        return self.pg.evaluate("[...document.querySelectorAll('#tst > .gc')].map(c=>c.dataset.t)")

    def test_finals_go_below_live_and_upcoming_keeping_relative_order(self):
        self.build([("card", "Alpha", "Bravo"), ("card", "Charlie", "Delta"), ("card", "Echo", "Foxtrot"), ("card", "Golf", "Hotel")],
                   [GAMES[k] for k in "ABCD"])
        self.paint()
        self.assertEqual(self.order(), ["Charlie", "Echo", "Alpha", "Golf"])

    def test_a_postponed_game_is_not_a_final_and_keeps_its_place(self):
        self.build([("card", "India", "Juliet"), ("card", "Alpha", "Bravo"), ("card", "Echo", "Foxtrot")], [GAMES[k] for k in "AEC"])
        self.paint()
        self.assertEqual(self.order(), ["India", "Echo", "Alpha"])

    def test_cards_without_live_data_are_never_moved(self):
        self.build([("card", "Alpha", "Bravo"), ("card", "Zulu", "Yankee"), ("card", "Echo", "Foxtrot")], [GAMES["A"]])
        self.paint()
        self.assertEqual(self.order(), ["Zulu", "Echo", "Alpha"])

    def test_a_heading_between_runs_stays_put_and_finals_do_not_cross_it(self):
        self.build([("h", "TODAY"), ("card", "Alpha", "Bravo"), ("card", "Echo", "Foxtrot"), ("h", "TOMORROW"), ("card", "Golf", "Hotel"), ("card", "Charlie", "Delta")],
                   [GAMES[k] for k in "ACDB"])
        self.paint()
        self.assertEqual(self.order(), ["TODAY", "Echo", "Alpha", "TOMORROW", "Charlie", "Golf"])

    def test_painting_again_changes_nothing(self):
        self.build([("card", "Alpha", "Bravo"), ("card", "Charlie", "Delta"), ("card", "Golf", "Hotel"), ("card", "Echo", "Foxtrot")], [GAMES[k] for k in "ABCD"])
        self.paint(); first = self.order(); self.paint(); self.paint()
        self.assertEqual(self.order(), first)
        self.assertEqual(first, ["Charlie", "Echo", "Alpha", "Golf"])

    def test_a_game_that_ends_later_moves_down_on_the_next_paint(self):
        self.build([("card", "Charlie", "Delta"), ("card", "Echo", "Foxtrot"), ("card", "Golf", "Hotel")], [GAMES["B"], GAMES["C"], GAMES["D"]])
        self.paint()
        self.assertEqual(self.order(), ["Charlie", "Echo", "Golf"])
        self.pg.evaluate("_LV.games=_LV.games.map(g=>g.away==='Charlie'?{...g,state:'post',completed:true}:g)")
        self.paint()
        self.assertEqual(self.order(), ["Echo", "Charlie", "Golf"])

    def test_a_final_that_is_no_longer_final_comes_back_up_only_as_far_as_its_flag(self):
        self.build([("card", "Alpha", "Bravo"), ("card", "Echo", "Foxtrot")], [GAMES["A"], GAMES["C"]])
        self.paint()
        self.assertTrue(self.pg.evaluate("document.querySelector('#tst .gc[data-t=Alpha]').hasAttribute('data-cv-final')"))
        self.pg.evaluate("_LV.games=[]"); self.paint()
        self.assertFalse(self.pg.evaluate("document.querySelector('#tst .gc[data-t=Alpha]').hasAttribute('data-cv-final')"))

    def test_the_slate_sort_buttons_keep_finals_at_the_bottom(self):
        # the final card has the best grade: a plain Grade sort would put it first
        self.build([("card", "Echo", "Foxtrot", "pn"), ("card", "Alpha", "Bravo", "pg"), ("card", "Charlie", "Delta", "pp")], [GAMES[k] for k in "CAB"])
        self.paint()
        self.pg.wait_for_selector("#tst .slate2", timeout=5000)
        self.pg.click("#tst .slate2 [data-sort=grade]"); self.pg.wait_for_timeout(500)
        self.assertEqual(self.cards(), ["Charlie", "Echo", "Alpha"])
        self.pg.click("#tst .slate2 [data-sort=time]"); self.pg.wait_for_timeout(500)
        self.assertEqual(self.cards()[-1], "Alpha")


if __name__ == "__main__":
    unittest.main(verbosity=2)
