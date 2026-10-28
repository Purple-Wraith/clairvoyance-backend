#!/usr/bin/env python3
"""ONE PROP PER PLAYER PER GAME (owner 2026-10-28).  Two layers, both tested:
  * auto_lock_settle.cap_props_per_player: when several props of one player qualify in the same pass, only his best is locked (highest probability; ties -> the model's probability at the posted
    line; then order);
  * docs/app.html lockProp: a player who already has a prop locked today (any earlier pass, or a manual tap) gets no second one -- and a removed prop does not block.
Also checks the props report's cap-violation count.

    python3 scripts/test_prop_player_cap.py
"""
import functools, http.server, socketserver, sys, threading, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import auto_lock_settle as als  # noqa: E402


def leg(player, stat, prob, hA="DEN", awA="OKC", posted_prob=None, sport="NBA", line=10.5):
    d = {"player": player, "statAbbr": stat, "prob": prob, "hA": hA, "awA": awA, "line": line, "over": True}
    if posted_prob is not None:
        d["altLine"] = {"posted": line + 2, "line": line, "postedProb": posted_prob}
    return {"kind": "PROP", "sport": sport, "leg": d}


class Selection(unittest.TestCase):
    def test_keeps_only_the_highest_probability_prop_of_a_player(self):
        a, b, c = leg("Star", "PTS", .64), leg("Star", "REB", .68), leg("Star", "AST", .66)
        kept, dropped = als.cap_props_per_player([a, b, c])
        self.assertEqual(kept, [b])
        self.assertEqual(dropped, [a, c])

    def test_a_tie_goes_to_the_higher_probability_at_the_posted_line(self):
        a, b = leg("Star", "PTS", .68, posted_prob=.61), leg("Star", "REB", .68, posted_prob=.64)   # adjusted lines all sit at the ceiling
        kept, _ = als.cap_props_per_player([a, b])
        self.assertEqual(kept, [b])

    def test_a_full_tie_keeps_the_first(self):
        a, b = leg("Star", "PTS", .68), leg("Star", "REB", .68)
        self.assertEqual(als.cap_props_per_player([a, b])[0], [a])

    def test_different_players_and_different_games_are_independent(self):
        legs = [leg("A", "PTS", .66), leg("B", "PTS", .66), leg("A", "PTS", .66, hA="BOS", awA="NY"), leg("C", "PTS", .66, sport="NHL")]
        kept, dropped = als.cap_props_per_player(legs)
        self.assertEqual((len(kept), len(dropped)), (4, 0))

    def test_name_case_and_spacing_do_not_defeat_the_cap(self):
        kept, dropped = als.cap_props_per_player([leg("Nikola Vucevic", "PTS", .64), leg(" nikola vucevic ", "REB", .67)])
        self.assertEqual((len(kept), len(dropped)), (1, 1))

    def test_input_order_is_preserved_and_a_leg_without_a_player_is_kept(self):
        a, b, c, d = leg("A", "PTS", .66), leg("B", "PTS", .70), leg("A", "REB", .70), {"kind": "PROP", "sport": "NBA", "leg": {"line": 5}}
        kept, dropped = als.cap_props_per_player([a, b, c, d])
        self.assertEqual(kept, [b, c, d])
        self.assertEqual(dropped, [a])

    def test_the_switch_turns_it_off(self):
        als.ONE_PROP_PER_PLAYER = False
        try:
            kept, dropped = als.cap_props_per_player([leg("A", "PTS", .66), leg("A", "REB", .67)])
        finally:
            als.ONE_PROP_PER_PLAYER = True
        self.assertEqual((len(kept), len(dropped)), (2, 0))

    def test_build_qualifying_applies_it_end_to_end(self):
        def raw(player, stat, prob, line=10.5):
            return {"grade": "OPTIMAL", "sportTag": "NBA", "hA": "DEN", "awA": "OKC", "player": player, "statAbbr": stat, "line": line, "over": True, "prob": prob, "team": "DEN"}
        result = {"gameLegs": [], "propLegs": [raw("Star", "PTS", .64), raw("Star", "REB", .67), raw("Star", "AST", .66), raw("Other", "PTS", .65)]}
        q = als.build_qualifying(result)
        got = sorted((x["leg"]["player"], x["leg"]["statAbbr"]) for x in q if x["kind"] == "PROP")
        self.assertEqual(got, [("Other", "PTS"), ("Star", "REB")])


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a, **k):
        pass


class LockGuard(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        socketserver.TCPServer.allow_reuse_address = True
        cls.srv = socketserver.TCPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=str(ROOT / "docs")))
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.pw = sync_playwright().start(); cls.b = cls.pw.chromium.launch()
        cls.pg = cls.b.new_page()
        cls.pg.goto(f"http://127.0.0.1:{cls.srv.server_address[1]}/app.html?nosb=1&nosw=1")
        cls.pg.wait_for_function("typeof lockProp==='function'"); cls.pg.wait_for_timeout(1200)

    @classmethod
    def tearDownClass(cls):
        cls.b.close(); cls.pw.stop(); cls.srv.shutdown()

    def lock(self, player, stat, line, over=True, team="DEN", opp="OKC"):
        return self.pg.evaluate("""([team,player,line,over,stat,opp])=>{const n=getP().length;lockProp(team,player,line,over,.66,'-150','NBA',opp,stat);return getP().length>n}""",
                                [team, player, line, over, stat, opp])

    def props(self, player):
        return [p["betOn"] for p in self.pg.evaluate("getP()") if p.get("betType") == "PROP" and p.get("player") == player and p.get("outcome") != "_removed"]

    def setUp(self):
        self.pg.evaluate("saveP([])")
        self.pg.evaluate("LOCKED_PROPS.length=0")

    def test_a_second_prop_for_the_same_player_the_same_day_is_refused(self):
        self.assertTrue(self.lock("Test Star", "PTS", 24.5))
        self.assertFalse(self.lock("Test Star", "REB", 9.5))                       # another stat
        self.assertFalse(self.lock("Test Star", "PTS", 22.5, over=False))          # the other side / line
        self.assertEqual(self.props("Test Star"), ["Test Star OVER PTS 24.5"])

    def test_other_players_are_unaffected(self):
        self.assertTrue(self.lock("Test Star", "PTS", 24.5))
        self.assertTrue(self.lock("Other Man", "PTS", 24.5))

    def test_a_removed_prop_does_not_block_a_new_one(self):
        self.assertTrue(self.lock("Test Star", "PTS", 24.5))
        self.pg.evaluate("()=>{const a=getP();a.forEach(p=>{if(p.player==='Test Star')p.outcome='_removed'});saveP(a)}")
        self.assertTrue(self.lock("Test Star", "REB", 9.5))

    def test_an_earlier_days_prop_does_not_block_today(self):
        self.pg.evaluate("""()=>saveP([{id:'old1',betType:'PROP',sport:'NBA',player:'Test Star',betOn:'Test Star OVER PTS 24.5',date:'2026-10-20',outcome:'win',lockedAt:1}])""")
        self.assertTrue(self.lock("Test Star", "PTS", 24.5))


class ReportViolations(unittest.TestCase):
    def test_report_counts_only_post_cap_violations(self):
        import nba_props_report as R
        def prop(i, player, date, stat):
            return {"id": i, "sport": "NBA", "betType": "PROP", "betOn": f"{player} OVER {stat} 9.5", "player": player, "outcome": "win", "date": date, "hA": "DEN", "awA": "OKC", "winProb": .66, "decOdds": 1.7}
        led = [prop("a", "Star", "2026-10-21", "PTS"), prop("b", "Star", "2026-10-21", "REB"),     # before the cap: history, tolerated
               prop("c", "Star", "2026-10-30", "PTS"), prop("d", "Star", "2026-10-30", "REB")]     # after the cap: a violation
        c = R.summarize(led)["concentration"]
        self.assertEqual(c["same_player_same_day"], 2)
        self.assertEqual(c["cap_violations"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
