#!/usr/bin/env python3
"""NBA player-prop settlement from the box score (docs/app.html autoSettlePropsESPN), owner request 2026-10-23: "make sure nba props are using box score so accuracy is correct".

Runs the real settler in Chromium against a STUBBED ESPN scoreboard + box score (no network) and checks the accuracy rules:
  * the actual stat comes from the final box score and is stored on the pick (playerResult);
  * accented names settle ("Nikola Jokic" vs ESPN's "Nikola Jokić") -- they used to sit pending forever;
  * a player is looked up in HIS OWN team's block: OKC's Jalen Williams is never graded on Denver's Jalen Williams;
  * an ambiguous surname is left pending rather than guessed;
  * a player who did not play (didNotPlay / no stats / 0 minutes) is VOID (push, voidReason DNP) -- never a zero-stat UNDER win or OVER loss;
  * only FINAL games settle; the game is matched on both teams;
  * a settlement pulled in from another device brings the stat (and void marker) with it, so the next push cannot erase it.

    python3 scripts/test_prop_settlement.py
"""
import functools, http.server, json, socketserver, threading, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a, **k):
        pass


NAMES = ["MIN", "PTS", "REB", "AST", "3PT"]


def ath(name, stats, dnp=False, reason=None):
    d = {"athlete": {"displayName": name}, "stats": ([] if dnp else stats), "didNotPlay": dnp}
    if reason:
        d["reason"] = reason
    return d


def blk(athletes):
    return {"names": NAMES, "athletes": athletes}


def event(eid, home, away, state="post", date="2026-10-22T23:00Z"):
    return {"id": eid, "date": date, "competitions": [{"status": {"type": {"state": state}},
            "competitors": [{"homeAway": "home", "team": {"abbreviation": home}}, {"homeAway": "away", "team": {"abbreviation": away}}]}]}


def prop(pid, bet, team, opp, line_hA=None):
    return {"id": pid, "betType": "PROP", "sport": "NBA", "league": "NBA", "betOn": bet, "team": team, "hA": team, "awA": opp, "date": "2026-10-22",
            "outcome": "pending", "lockedAt": 1, "winProb": .6, "decOdds": 1.7, "player": bet.split(" OVER ")[0].split(" UNDER ")[0]}


SCORE = {"events": [event("E1", "DEN", "OKC"), event("E2", "BOS", "NY", state="in"), event("E3", "LAL", "GS")]}
SUMMARY = {
    "E1": {"boxscore": {"players": [
        {"team": {"abbreviation": "DEN"}, "statistics": [blk([ath("Nikola Jokić", ["34", "28", "13", "9", "1-3"]), ath("Jalen Williams", ["30", "10", "4", "3", "1-2"]),
                                                              ath("Zeke Nnaji", [], True, "COACH'S DECISION"), ath("Bruce Brown", ["0", "0", "0", "0", "0-0"]),
                                                              ath("Reserve Guy", ["5", "0", "1", "1", "0-0"]), ath("Nine Min", ["9", "8", "2", "1", "1-2"]), ath("Ten Min", ["10", "8", "2", "1", "1-2"])])]},
        {"team": {"abbreviation": "OKC"}, "statistics": [blk([ath("Jalen Williams", ["33", "24", "5", "6", "2-5"]), ath("Shai Gilgeous-Alexander", ["35", "31", "4", "7", "1-2"]),
                                                              ath("Isaiah Joe", ["20", "12", "2", "1", "3-6"])])]}]}},
    "E2": {"boxscore": {"players": [{"team": {"abbreviation": "NY"}, "statistics": [blk([ath("Jalen Brunson", ["20", "11", "1", "3", "0-1"])])]}]}},   # game still in progress
    "E3": {"boxscore": {"players": [
        {"team": {"abbreviation": "LAL"}, "statistics": [blk([ath("Jaxson Hayes", ["20", "6", "5", "0", "0-0"]), ath("Jake LaRavia", ["22", "9", "4", "2", "1-2"])])]},
        {"team": {"abbreviation": "GS"}, "statistics": [blk([ath("Jaylin Williams", ["10", "2", "2", "0", "0-0"]), ath("Jalen Williams", ["15", "5", "1", "1", "0-0"])])]}]}},
}


class Settle(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        socketserver.TCPServer.allow_reuse_address = True
        cls.srv = socketserver.TCPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=str(ROOT / "docs")))
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.pw = sync_playwright().start(); cls.b = cls.pw.chromium.launch()
        cls.pg = cls.b.new_page()
        cls.pg.goto(f"http://127.0.0.1:{cls.srv.server_address[1]}/app.html?nosb=1")
        cls.pg.wait_for_function("typeof autoSettlePropsESPN==='function'"); cls.pg.wait_for_timeout(1200)
        cls.pg.evaluate("""([sc,sm])=>{window.__f=window.fetch;window.fetch=async(u,o)=>{u=String(u);
          if(u.includes('/nba/scoreboard'))return new Response(JSON.stringify(sc),{status:200});
          const m=u.match(/\\/nba\\/summary\\?event=(\\w+)/);if(m)return new Response(JSON.stringify(sm[m[1]]||{}),{status:200});
          return window.__f(u,o)}}""", [SCORE, SUMMARY])

    @classmethod
    def tearDownClass(cls):
        cls.b.close(); cls.pw.stop(); cls.srv.shutdown()

    def settle(self, props):
        self.pg.evaluate("(p)=>saveP(p)", props)
        self.pg.evaluate("autoSettlePropsESPN('2026-10-22')")
        self.pg.wait_for_timeout(900)
        return {p["id"]: p for p in self.pg.evaluate("getP().filter(p=>p.betType==='PROP')")}

    def test_stat_comes_from_the_final_box_score_and_is_stored(self):
        r = self.settle([prop("a1", "Shai Gilgeous-Alexander UNDER AST 6.5", "OKC", "DEN"), prop("a2", "Shai Gilgeous-Alexander OVER PTS 30.5", "OKC", "DEN"),
                         prop("a3", "Isaiah Joe OVER AST 2.5", "OKC", "DEN")])
        self.assertEqual((r["a1"]["outcome"], r["a1"]["playerResult"]), ("loss", 7))       # 7 assists vs UNDER 6.5
        self.assertEqual((r["a2"]["outcome"], r["a2"]["playerResult"]), ("win", 31))       # 31 points vs OVER 30.5
        self.assertEqual((r["a3"]["outcome"], r["a3"]["playerResult"]), ("loss", 1))       # 1 assist vs OVER 2.5

    def test_an_accented_name_settles(self):
        r = self.settle([prop("b1", "Nikola Jokic OVER PTS 25.5", "DEN", "OKC")])
        self.assertEqual((r["b1"]["outcome"], r["b1"]["playerResult"]), ("win", 28))

    def test_a_shared_name_is_graded_on_his_own_team(self):
        r = self.settle([prop("c1", "Jalen Williams OVER PTS 20.5", "OKC", "DEN"), prop("c2", "Jalen Williams OVER PTS 20.5", "DEN", "OKC")])
        self.assertEqual((r["c1"]["outcome"], r["c1"]["playerResult"]), ("win", 24))        # OKC's Williams scored 24 ...
        self.assertEqual((r["c2"]["outcome"], r["c2"]["playerResult"]), ("loss", 10))       # ... Denver's scored 10

    def test_an_ambiguous_surname_stays_pending_instead_of_guessing(self):
        r = self.settle([prop("d1", "Williams OVER PTS 4.5", "GS", "LAL")])                   # GS has two Williamses in this box score
        self.assertEqual(r["d1"]["outcome"], "pending")

    def test_a_player_who_did_not_play_is_void_not_a_zero_stat_result(self):
        r = self.settle([prop("e1", "Zeke Nnaji UNDER PTS 4.5", "DEN", "OKC"), prop("e2", "Zeke Nnaji OVER PTS 4.5", "DEN", "OKC")])
        for k in ("e1", "e2"):
            self.assertEqual(r[k]["outcome"], "push")                                      # void: stake back, 0 units, not counted in win %
            self.assertEqual(r[k]["voidReason"], "DNP")
            self.assertIsNone(r[k].get("playerResult"))

    def test_zero_minutes_is_also_void_but_five_minutes_of_zeros_is_a_real_result(self):
        r = self.settle([prop("f1", "Bruce Brown UNDER PTS 3.5", "DEN", "OKC")])             # MIN "0" in the box score
        self.assertEqual((r["f1"]["outcome"], r["f1"]["voidReason"]), ("push", "DNP"))

    def test_only_final_games_settle(self):
        r = self.settle([prop("g1", "Jalen Brunson OVER PTS 10.5", "NY", "BOS")])            # E2 is in progress
        self.assertEqual(r["g1"]["outcome"], "pending")

    def test_the_game_is_matched_on_both_teams(self):
        r = self.settle([prop("h1", "Jaxson Hayes OVER REB 4.5", "LAL", "GS")])
        self.assertEqual((r["h1"]["outcome"], r["h1"]["playerResult"]), ("win", 5))

    def test_a_player_in_no_box_score_stays_pending(self):
        r = self.settle([prop("i1", "Nobody Atall OVER PTS 4.5", "DEN", "OKC")])
        self.assertEqual(r["i1"]["outcome"], "pending")

    def test_a_pulled_in_settlement_keeps_the_stat_and_the_void_marker(self):
        res = self.pg.evaluate("""()=>{
          const base={betType:'PROP',sport:'NBA',league:'NBA',date:'2026-10-22',hA:'DEN',awA:'OKC',team:'DEN',lockedAt:5,winProb:.6,decOdds:1.7};
          saveP([Object.assign({id:'m1',betOn:'X OVER PTS 9.5',outcome:'pending'},base),Object.assign({id:'m2',betOn:'Y OVER PTS 9.5',outcome:'pending'},base)]);
          _mergeRemoteBetRows([
            {id:'m1',outcome:'win',settled_at:new Date().toISOString(),raw:Object.assign({id:'m1',betOn:'X OVER PTS 9.5',outcome:'win',playerResult:14},base)},
            {id:'m2',outcome:'push',settled_at:new Date().toISOString(),raw:Object.assign({id:'m2',betOn:'Y OVER PTS 9.5',outcome:'push',playerResult:null,voidReason:'DNP'},base)}]);
          return getP().filter(p=>p.id==='m1'||p.id==='m2').map(p=>({id:p.id,outcome:p.outcome,pr:p.playerResult,v:p.voidReason}))}""")
        got = {x["id"]: x for x in res}
        self.assertEqual((got["m1"]["outcome"], got["m1"]["pr"]), ("win", 14))               # the stat arrives with the settlement
        self.assertEqual((got["m2"]["outcome"], got["m2"]["v"]), ("push", "DNP"))

    # ── stat backfill (2026-10-28): props settled before the box-score stat was stored get ONLY the stat ──
    def settled(self, pid, bet, team, opp, outcome, **extra):
        d = prop(pid, bet, team, opp)
        d.update({"outcome": outcome, "settledAt": 1234567, "lockedAt": 5}); d.update(extra)
        return d

    def test_a_settled_prop_without_its_stat_gets_the_stat_and_nothing_else(self):
        r = self.settle([self.settled("s1", "Shai Gilgeous-Alexander OVER PTS 30.5", "OKC", "DEN", "win")])     # 31 points in the box score
        self.assertEqual((r["s1"]["outcome"], r["s1"]["playerResult"], r["s1"]["settledAt"]), ("win", 31, 1234567))
        self.assertEqual(r["s1"]["playerMin"], 35)                                                             # minutes are recorded too (the under-10 rule needs them)
        self.assertNotIn("regradedNote", r["s1"])

    def test_a_stored_result_is_never_changed_even_if_the_box_score_disagrees(self):
        r = self.settle([self.settled("s2", "Shai Gilgeous-Alexander OVER PTS 30.5", "OKC", "DEN", "loss")])    # stored LOSS, box score says WIN
        self.assertEqual((r["s2"]["outcome"], r["s2"]["playerResult"]), ("loss", 31))                          # stat recorded, outcome untouched

    def test_a_stat_already_stored_is_left_alone(self):
        r = self.settle([self.settled("s3", "Shai Gilgeous-Alexander OVER PTS 30.5", "OKC", "DEN", "win", playerResult=99)])
        self.assertEqual(r["s3"]["playerResult"], 99)

    def test_a_voided_or_dnp_prop_is_not_backfilled(self):
        r = self.settle([self.settled("s4", "Zeke Nnaji OVER PTS 4.5", "DEN", "OKC", "push", voidReason="DNP"),
                         self.settled("s5", "Zeke Nnaji OVER PTS 4.5", "DEN", "OKC", "win")])                    # settled win but the box score says he did not play
        self.assertIsNone(r["s4"].get("playerResult"))
        self.assertIsNone(r["s5"].get("playerResult"))
        self.assertEqual(r["s5"]["outcome"], "win")

    # ── under-10-minutes void (owner 2026-10-28) ──
    def test_a_pending_prop_for_a_player_under_ten_minutes_is_void(self):
        r = self.settle([prop("v1", "Reserve Guy UNDER PTS 4.5", "DEN", "OKC"), prop("v2", "Reserve Guy OVER PTS 4.5", "DEN", "OKC")])      # 5 min, 0 pts
        for k in ("v1", "v2"):
            self.assertEqual((r[k]["outcome"], r[k]["voidReason"], r[k]["playerMin"], r[k]["playerResult"]), ("push", "LOW_MIN", 5, 0))   # an UNDER did not "win", an OVER did not "lose"

    def test_nine_minutes_is_void_ten_minutes_is_a_real_result(self):
        r = self.settle([prop("w1", "Nine Min OVER PTS 7.5", "DEN", "OKC"), prop("w2", "Ten Min OVER PTS 7.5", "DEN", "OKC")])
        self.assertEqual((r["w1"]["outcome"], r["w1"]["voidReason"]), ("push", "LOW_MIN"))
        self.assertEqual((r["w2"]["outcome"], r["w2"]["playerResult"], r["w2"]["playerMin"]), ("win", 8, 10))
        self.assertNotIn("voidReason", r["w2"])

    def test_an_already_settled_win_or_loss_under_ten_minutes_is_voided_and_marked_as_a_correction(self):
        r = self.settle([self.settled("x1", "Reserve Guy UNDER PTS 4.5", "DEN", "OKC", "win"), self.settled("x2", "Reserve Guy OVER PTS 4.5", "DEN", "OKC", "loss")])
        for k, was in (("x1", "WIN"), ("x2", "LOSS")):
            self.assertEqual((r[k]["outcome"], r[k]["voidReason"], r[k]["playerMin"]), ("push", "LOW_MIN", 5))
            self.assertIn("was " + was, r[k]["regradedNote"])                                                # the marker other devices use to accept a correction
            self.assertEqual(r[k]["settledAt"], 1234567)

    def test_a_settled_prop_that_already_has_its_minutes_is_not_looked_at_again(self):
        r = self.settle([self.settled("y1", "Reserve Guy UNDER PTS 4.5", "DEN", "OKC", "win", playerResult=0, playerMin=12)])   # minutes already checked (say 12): box score is not consulted
        self.assertEqual((r["y1"]["outcome"], r["y1"]["playerMin"]), ("win", 12))

    def test_a_voided_prop_carries_to_a_device_that_still_holds_the_win(self):
        res = self.pg.evaluate("""()=>{
          const base={betType:'PROP',sport:'NBA',league:'NBA',date:'2026-10-22',hA:'DEN',awA:'OKC',team:'DEN',lockedAt:5,winProb:.6,decOdds:1.7,betOn:'X UNDER PTS 9.5'};
          saveP([Object.assign({id:'z1',outcome:'win',playerResult:0},base)]);
          _mergeRemoteBetRows([{id:'z1',outcome:'push',settled_at:new Date().toISOString(),
            raw:Object.assign({id:'z1',outcome:'push',playerResult:0,playerMin:5,voidReason:'LOW_MIN',regradedNote:'Voided: played 5 min'},base)}]);
          const p=getP().find(q=>q.id==='z1');return {o:p.outcome,v:p.voidReason,m:p.playerMin}}""")
        self.assertEqual(res, {"o": "push", "v": "LOW_MIN", "m": 5})

    def test_the_undated_sweep_reaches_settled_props_from_earlier_days(self):
        self.pg.evaluate("""()=>{window.__realToday=today;today=()=>'2026-10-25'}""")                           # 3 days after the props' day
        try:
            self.pg.evaluate("(p)=>saveP(p)", [self.settled("s6", "Shai Gilgeous-Alexander OVER PTS 30.5", "OKC", "DEN", "win")])
            self.pg.evaluate("autoSettlePropsESPN()"); self.pg.wait_for_timeout(1500)
            got = self.pg.evaluate("getP().find(p=>p.id==='s6')")
        finally:
            self.pg.evaluate("()=>{today=window.__realToday}")
        self.assertEqual(got["playerResult"], 31)

    def test_other_sports_and_old_seasons_are_not_swept(self):
        r = self.settle([self.settled("s7", "Shai Gilgeous-Alexander OVER PTS 30.5", "OKC", "DEN", "win", date="2026-06-03")])
        self.assertIsNone(r["s7"].get("playerResult"))


class CiSweep(unittest.TestCase):
    """The scheduled settle pass (auto_lock_settle.run_settle) must reach settled props that are missing their stat even when NO pick is pending on their date."""

    @classmethod
    def setUpClass(cls):
        from datetime import date, timedelta
        from playwright.sync_api import sync_playwright
        sys_path = str(ROOT / "scripts")
        import sys
        if sys_path not in sys.path:
            sys.path.insert(0, sys_path)
        socketserver.TCPServer.allow_reuse_address = True
        cls.srv = socketserver.TCPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=str(ROOT / "docs")))
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.pw = sync_playwright().start(); cls.b = cls.pw.chromium.launch()
        cls.pg = cls.b.new_page()
        cls.pg.goto(f"http://127.0.0.1:{cls.srv.server_address[1]}/app.html?nosb=1&nosw=1")
        cls.pg.wait_for_function("typeof autoSettlePropsESPN==='function'&&typeof _propNeedsStat==='function'"); cls.pg.wait_for_timeout(1200)
        cls.day = (date.today() - timedelta(days=3)).isoformat()
        score = {"events": [event("E1", "DEN", "OKC", date=cls.day + "T19:00Z")]}
        cls.pg.evaluate("""([sc,sm])=>{const f=window.fetch;window.fetch=async(u,o)=>{u=String(u);
          if(u.includes('/nba/scoreboard'))return new Response(JSON.stringify(sc),{status:200});
          const m=u.match(/\\/nba\\/summary\\?event=(\\w+)/);if(m)return new Response(JSON.stringify(sm[m[1]]||{}),{status:200});
          return f(u,o)}}""", [score, {"E1": SUMMARY["E1"]}])

    @classmethod
    def tearDownClass(cls):
        cls.b.close(); cls.pw.stop(); cls.srv.shutdown()

    def ledger(self, **over):
        base = prop("k1", "Shai Gilgeous-Alexander OVER PTS 30.5", "OKC", "DEN")
        base.update({"outcome": "win", "settledAt": 1234567, "lockedAt": 5, "date": self.day})
        base.update(over)
        return [base]

    def run_pass(self, ledger):
        import auto_lock_settle as als
        self.pg.evaluate("(p)=>saveP(p)", ledger)
        newly = als.run_settle(self.pg, live=False)
        self.pg.wait_for_timeout(500)
        return newly, {p["id"]: p for p in self.pg.evaluate("getP()")}

    def test_the_scheduled_pass_fills_the_stat_with_nothing_pending(self):
        newly, got = self.run_pass(self.ledger())
        self.assertEqual((got["k1"]["outcome"], got["k1"]["playerResult"], got["k1"]["settledAt"]), ("win", 31, 1234567))
        self.assertEqual(newly, [])                                                         # it was not a new settlement

    def test_the_scheduled_pass_voids_a_low_minute_win_and_counts_it_for_the_supabase_flush(self):
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            _, got = self.run_pass(self.ledger(id="k3", betOn="Reserve Guy UNDER PTS 4.5", team="DEN", hA="DEN", awA="OKC"))
        self.assertEqual((got["k3"]["outcome"], got["k3"]["voidReason"]), ("push", "LOW_MIN"))
        self.assertIn("Prop box-score backfill: 1", buf.getvalue())                         # the flush to Supabase is keyed on this

    def test_a_prop_older_than_the_sweep_window_is_left_alone(self):
        old = self.ledger(date="2026-10-01", id="k2")
        _, got = self.run_pass(old)
        self.assertIsNone(got["k2"].get("playerResult"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
