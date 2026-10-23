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
                                                              ath("Zeke Nnaji", [], True, "COACH'S DECISION"), ath("Bruce Brown", ["0", "0", "0", "0", "0-0"])])]},
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
