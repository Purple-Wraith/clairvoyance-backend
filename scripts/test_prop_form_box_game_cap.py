#!/usr/bin/env python3
"""Owner 2026-10-28: (1) a player prop's box shows his season averages and last-5 form; (2) one game keeps at most 5 player props.

  * docs/app.html _propFormBoxHTML / _propStatsObj / the stats stored on a locked prop and drawn in the locked pick row;
  * auto_lock_settle.cap_props_per_game + build_qualifying, and the lockProp ledger backstop; the props report's game-cap check.

    python3 scripts/test_prop_form_box_game_cap.py
"""
import functools, http.server, socketserver, sys, threading, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import auto_lock_settle as als  # noqa: E402


def leg(player, prob, hA="DEN", awA="OKC", posted_prob=None, sport="NBA"):
    d = {"player": player, "statAbbr": "PTS", "prob": prob, "hA": hA, "awA": awA, "line": 20.5, "over": True}
    if posted_prob is not None:
        d["altLine"] = {"posted": 22.5, "line": 20.5, "postedProb": posted_prob}
    return {"kind": "PROP", "sport": sport, "leg": d}


class GameCapSelection(unittest.TestCase):
    def test_a_game_keeps_its_five_best(self):
        legs = [leg(f"P{i}", .60 + i * .01) for i in range(8)]          # P7 best ... P0 worst
        kept, dropped = als.cap_props_per_game(legs)
        self.assertEqual([q["leg"]["player"] for q in kept], ["P3", "P4", "P5", "P6", "P7"])
        self.assertEqual(sorted(q["leg"]["player"] for q in dropped), ["P0", "P1", "P2"])

    def test_five_or_fewer_are_untouched(self):
        legs = [leg(f"P{i}", .65) for i in range(5)]
        self.assertEqual(als.cap_props_per_game(legs), (legs, []))

    def test_games_and_sports_are_counted_separately(self):
        legs = [leg(f"A{i}", .65) for i in range(5)] + [leg(f"B{i}", .65, hA="BOS", awA="NY") for i in range(5)]
        kept, dropped = als.cap_props_per_game(legs)
        self.assertEqual((len(kept), len(dropped)), (10, 0))

    def test_a_tie_goes_to_the_posted_line_probability_then_order(self):
        legs = [leg(f"P{i}", .68, posted_prob=.60) for i in range(5)] + [leg("Better", .68, posted_prob=.66), leg("Last", .68, posted_prob=.60)]
        kept, dropped = als.cap_props_per_game(legs)
        names = [q["leg"]["player"] for q in kept]
        self.assertIn("Better", names)
        self.assertEqual(sorted(q["leg"]["player"] for q in dropped), ["Last", "P4"])

    def test_the_limit_can_be_changed_or_switched_off(self):
        legs = [leg(f"P{i}", .65) for i in range(7)]
        self.assertEqual(len(als.cap_props_per_game(legs, limit=3)[0]), 3)
        self.assertEqual(als.cap_props_per_game(legs, limit=0), (legs, []))

    def test_build_qualifying_applies_both_caps(self):
        def raw(player, stat, prob):
            return {"grade": "OPTIMAL", "sportTag": "NBA", "hA": "DEN", "awA": "OKC", "player": player, "statAbbr": stat, "line": 10.5, "over": True, "prob": prob, "team": "DEN"}
        props = [raw(f"P{i}", "PTS", .60 + i * .01) for i in range(7)] + [raw("P6", "REB", .59)]   # P6 has a second, worse prop
        q = [x for x in als.build_qualifying({"gameLegs": [], "propLegs": props}) if x["kind"] == "PROP"]
        self.assertEqual(sorted(x["leg"]["player"] for x in q), ["P2", "P3", "P4", "P5", "P6"])
        self.assertTrue(all(x["leg"]["statAbbr"] == "PTS" for x in q))


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a, **k):
        pass


PL = {"name": "Test Star", "team": "DEN", "gp": 8, "ppg": 24.14, "rpg": 5.2, "apg": 4.04, "mpg": 33.06,
      "last5": {"n": 5, "ppg": 28.6, "rpg": 4.8, "apg": 4.0, "thisSeason": 3}}


class InPage(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        socketserver.TCPServer.allow_reuse_address = True
        cls.srv = socketserver.TCPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=str(ROOT / "docs")))
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.pw = sync_playwright().start(); cls.b = cls.pw.chromium.launch()
        cls.pg = cls.b.new_page()
        cls.pg.goto(f"http://127.0.0.1:{cls.srv.server_address[1]}/app.html?nosb=1&nosw=1")
        cls.pg.wait_for_function("typeof lockProp==='function'&&typeof _propFormBoxHTML==='function'"); cls.pg.wait_for_timeout(1200)

    @classmethod
    def tearDownClass(cls):
        cls.b.close(); cls.pw.stop(); cls.srv.shutdown()

    def setUp(self):
        self.pg.evaluate("saveP([])"); self.pg.evaluate("LOCKED_PROPS.length=0")

    def box(self, ps, stat):
        return self.pg.evaluate("([ps,s])=>_propFormBoxHTML(ps,s)", [ps, stat])

    def test_stats_object_is_compact_and_rounded(self):
        o = self.pg.evaluate("pl=>_propStatsObj(pl)", PL)
        self.assertEqual(o["s"], {"PTS": 24.1, "REB": 5.2, "AST": 4.0}); self.assertEqual(o["mpg"], 33.1)
        self.assertEqual(o["l5"], {"n": 5, "cur": 3, "PTS": 28.6, "REB": 4.8, "AST": 4.0}); self.assertEqual(o["gp"], 8)

    def test_box_shows_season_and_last_five_with_the_prop_stat_marked(self):
        html = self.box(self.pg.evaluate("pl=>_propStatsObj(pl)", PL), "PTS")
        for txt in ("SEASON", "LAST 5", "24.1", "28.6", "33.1", "8 GP this season", "last 5 includes 2 games from last season"):
            self.assertIn(txt, html)
        self.assertIn('<span class="on">24.1</span>', html)       # the prop's own stat is emphasised
        self.assertNotIn('<span class="on">5.2</span>', html)
        self.assertIn("▲", html)                              # PTS form 28.6 vs 24.1: up

    def test_form_arrow_points_down_and_small_gaps_have_none(self):
        ps = self.pg.evaluate("pl=>_propStatsObj(pl)", PL)
        ps["l5"]["PTS"] = 19.0
        self.assertIn("▼", self.box(ps, "PTS"))
        ps["l5"]["PTS"] = 24.3
        h = self.box(ps, "PTS"); self.assertNotIn("▲", h); self.assertNotIn("▼", h)

    def test_missing_form_and_missing_stats_degrade_quietly(self):
        ps = self.pg.evaluate("pl=>_propStatsObj(pl)", {**PL, "last5": None})
        self.assertIn("no recent games", self.box(ps, "REB"))
        self.assertEqual(self.box(None, "PTS"), "")
        self.assertEqual(self.box({"s": {"PTS": None, "REB": None, "AST": None}, "mpg": None}, "PTS").count("—") >= 4, True)

    def test_a_generated_prop_carries_stats_and_a_lock_stores_them(self):
        r = self.pg.evaluate("""(pl)=>{
          const p=_withPropReasoning({player:'Test Star',statAbbr:'PTS',stat:'ppg',line:20.5,over:true,grade:'OPTIMAL',prob:.66,pstats:_propStatsObj(pl),mu:24,sigma:6,base:24.1});
          lockProp('DEN','Test Star','20.5',true,.66,'-150','NBA','OKC','PTS');
          const row=getP().find(x=>x.player==='Test Star');
          return {stored:row&&row.pstats, html:_pickRowHTML(row,{mode:'pending'})};}""", PL)
        self.assertEqual(r["stored"]["s"]["PTS"], 24.1)
        self.assertIn('class="pfbox"', r["html"]); self.assertIn("LAST 5", r["html"])

    def test_a_row_without_stats_just_has_no_box(self):
        html = self.pg.evaluate("""()=>{lockProp('DEN','Old Guy','20.5',true,.66,'-150','NBA','OKC','PTS');return _pickRowHTML(getP().find(x=>x.player==='Old Guy'),{mode:'pending'})}""")
        self.assertNotIn("pfbox", html)

    def lock(self, player, team="DEN", opp="OKC"):
        return self.pg.evaluate("""([t,pl,o])=>{const n=getP().length;lockProp(t,pl,'10.5',true,.66,'-150','NBA',o,'PTS');return getP().length>n}""", [team, player, opp])

    def test_a_sixth_prop_in_a_game_is_refused_from_either_side(self):
        for i in range(5):
            self.assertTrue(self.lock(f"Player {i}", team="DEN" if i % 2 else "OKC", opp="OKC" if i % 2 else "DEN"))
        self.assertFalse(self.lock("Player 5"))
        self.assertFalse(self.lock("Player 6", team="OKC", opp="DEN"))
        self.assertTrue(self.lock("Other Game", team="BOS", opp="NY"))     # another game is unaffected

    def test_removed_props_and_earlier_days_do_not_count(self):
        for i in range(5):
            self.lock(f"Player {i}")
        self.pg.evaluate("()=>{const a=getP();a[0].outcome='_removed';saveP(a)}")
        self.assertTrue(self.lock("Player 9"))
        self.pg.evaluate("()=>saveP(getP().map(p=>({...p,date:'2026-10-01'})))")
        self.assertTrue(self.lock("Player 10"))


class GameHeader(unittest.TestCase):
    def test_props_game_header_is_a_big_neon_magenta_class_with_a_phone_size(self):
        import re
        html = (ROOT / "docs" / "app.html").read_text()
        rule = re.search(r"^\.pgh\{([^}]*)\}", html, re.M).group(1)
        self.assertIn("color:var(--pc)", rule)                                   # neon magenta
        self.assertGreaterEqual(int(re.search(r"font-size:(\d+)px", rule).group(1)), 28)
        self.assertRegex(html, r"html \.pgh\{font-size:1[6-9]px")                 # phone rule: the inline-size caps cannot reach a class
        self.assertIn("""'<span class="pgh">'+ps[0].awA+' @ '+ps[0].hA""", html)


class ReportGameCap(unittest.TestCase):
    def test_flags_a_game_over_five_after_the_cap_date_only(self):
        import nba_props_report as R
        def prop(i, date):
            return {"id": f"{date}{i}", "sport": "NBA", "betType": "PROP", "betOn": f"P{i} OVER PTS 9.5", "player": f"P{i}", "outcome": "win", "date": date, "hA": "DEN", "awA": "OKC", "winProb": .66, "decOdds": 1.7}
        led = [prop(i, "2026-10-21") for i in range(6)] + [prop(i, "2026-10-30") for i in range(6)]
        c = R.summarize(led)["concentration"]
        self.assertEqual(c["game_cap_violations"], 1)
        self.assertTrue(any("per-game cap" in l for l in R.verdict(R.summarize(led))))


if __name__ == "__main__":
    unittest.main(verbosity=2)
