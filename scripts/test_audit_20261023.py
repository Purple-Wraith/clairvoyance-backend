#!/usr/bin/env python3
"""Regression tests for the 2026-10-23 full audit (docs/app.html).  Each test pins one verified bug the audit found:

  - a prop is graded from ITS OWN day's box score (never from another day's game for the same team), per pending date
  - a postponed / abandoned game (ESPN 'post' with completed:false and a 0-0 score) never settles anything
  - lockProp dedupes against the whole ledger (LOCKED_PROPS is a localStorage list that is empty in every CI run, so repeat lock passes re-locked settled props)
  - the props settler writes its results onto a fresh read of the ledger (a pick locked while ESPN was being fetched must survive)
  - TOP ADJUSTED LINES forgets a line the model no longer adjusts
  - external text is escaped (pick rows, quotes inside attributes, news links)
  - yesterday() is the calendar day before the Denver date (DST-safe)

    python3 scripts/test_audit_20261023.py
"""
import functools, http.server, json, socketserver, threading, unittest
from pathlib import Path

import os
ROOT = Path(os.environ.get("CV_ROOT") or Path(__file__).resolve().parent.parent)


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a, **k):
        pass


NAMES = ["MIN", "PTS", "REB", "AST", "3PT"]


def ath(name, stats):
    return {"athlete": {"displayName": name}, "stats": stats, "didNotPlay": False}


def event(eid, home, away, date, completed=True):
    return {"id": eid, "date": date, "competitions": [{"status": {"type": {"state": "post", "completed": completed}},
            "competitors": [{"homeAway": "home", "team": {"abbreviation": home}}, {"homeAway": "away", "team": {"abbreviation": away}}]}]}


def box(team_players):
    return {"boxscore": {"players": [{"team": {"abbreviation": t}, "statistics": [{"names": NAMES, "athletes": [ath(n, s) for n, s in pl]}]} for t, pl in team_players]}}


# scoreboards by ESPN date param (YYYYMMDD); the summary by event id
BOARDS = {
    "20261022": [event("E1", "DEN", "OKC", "2026-10-22T23:00Z")],
    "20261023": [event("E4", "PHX", "LAL", "2026-10-23T23:00Z")],
    "20261021": [event("E5", "BOS", "NY", "2026-10-21T23:00Z", completed=False)],
}
SUMMARY = {
    "E1": box([("DEN", [("Nikola Jokic", ["34", "28", "13", "9", "1-3"])]), ("OKC", [("Shai Gilgeous-Alexander", ["35", "31", "4", "7", "1-2"])])]),
    "E4": box([("LAL", [("LeBron James", ["34", "12", "6", "7", "1-3"])]), ("PHX", [("Devin Booker", ["35", "30", "4", "7", "1-2"])])]),
    "E5": box([("NY", [("Jalen Brunson", ["0", "0", "0", "0", "0-0"])]), ("BOS", [("Jayson Tatum", ["0", "0", "0", "0", "0-0"])])]),
}


def prop(pid, bet, team, opp, date):
    return {"id": pid, "betType": "PROP", "sport": "NBA", "league": "NBA", "betOn": bet, "team": team, "hA": team, "awA": opp, "date": date,
            "outcome": "pending", "lockedAt": 1, "winProb": .6, "decOdds": 1.7, "player": bet.split(" OVER ")[0].split(" UNDER ")[0]}


class Audit(unittest.TestCase):
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
        cls.pg.wait_for_function("typeof autoSettlePropsESPN==='function'&&typeof lockProp==='function'&&typeof _altRowFor==='function'"); cls.pg.wait_for_timeout(1200)
        cls.pg.evaluate("""([boards,sm])=>{window.today=()=>'2026-10-23';window.__f=window.fetch;
          window.fetch=async(u,o)=>{u=String(u);
            if(u.includes('/nba/scoreboard')){const m=u.match(/dates=(\\d{8})/);return new Response(JSON.stringify({events:boards[m?m[1]:'20261023']||[]}),{status:200});}
            const s=u.match(/\\/nba\\/summary\\?event=(\\w+)/);if(s)return new Response(JSON.stringify(sm[s[1]]||{}),{status:200});
            return window.__f(u,o)}}""", [BOARDS, SUMMARY])

    @classmethod
    def tearDownClass(cls):
        cls.b.close(); cls.pw.stop(); cls.srv.shutdown()

    def settle(self, props, arg=None):
        self.pg.evaluate("(p)=>saveP(p)", props)
        self.pg.evaluate("(a)=>autoSettlePropsESPN(a)", arg)
        self.pg.wait_for_timeout(1200)
        return {p["id"]: p for p in self.pg.evaluate("getP().filter(p=>p.betType==='PROP')")}

    # ── props ──
    def test_a_prop_is_never_graded_from_another_days_game_of_the_same_team(self):
        # LAL played on the 23rd (LeBron 12 pts) but this prop is for the 22nd's LAL-DEN game, which has no box score: it stays pending
        r = self.settle([prop("p1", "LeBron James OVER PTS 25.5", "LAL", "DEN", "2026-10-22")])
        self.assertEqual(r["p1"]["outcome"], "pending")

    def test_with_no_date_each_pending_day_is_settled_from_its_own_scoreboard(self):
        r = self.settle([prop("p2", "Shai Gilgeous-Alexander OVER PTS 30.5", "OKC", "DEN", "2026-10-22"), prop("p3", "LeBron James OVER PTS 25.5", "LAL", "PHX", "2026-10-23")])
        self.assertEqual((r["p2"]["outcome"], r["p2"]["playerResult"]), ("win", 31))
        self.assertEqual((r["p3"]["outcome"], r["p3"]["playerResult"]), ("loss", 12))

    def test_a_postponed_game_never_settles_a_prop(self):
        r = self.settle([prop("p4", "Jalen Brunson UNDER PTS 20.5", "NY", "BOS", "2026-10-21")], "2026-10-21")                    # ESPN: 'post', completed:false, all zeros -- an UNDER would "win"
        self.assertEqual(r["p4"]["outcome"], "pending")

    def test_results_are_written_onto_a_fresh_read_of_the_ledger(self):
        self.pg.evaluate("(p)=>saveP(p)", [prop("p5", "Shai Gilgeous-Alexander OVER PTS 30.5", "OKC", "DEN", "2026-10-22")])
        # a pick locked while the ESPN requests are in flight must survive the save
        self.pg.evaluate("""()=>{const f=window.fetch;window.fetch=async(u,o)=>{const r=await f(u,o);if(String(u).includes('/nba/summary')&&!window.__added){window.__added=1;
          const cur=getP();cur.push({id:'late_lock',betType:'ML',sport:'NBA',league:'NBA',betOn:'X ML',hA:'X',awA:'Y',date:'2026-10-23',outcome:'pending',lockedAt:2,winProb:.6,decOdds:1.7,wager:0});saveP(cur);}return r};}""")
        self.pg.evaluate("autoSettlePropsESPN('2026-10-22')"); self.pg.wait_for_timeout(1500)
        ids = {p["id"]: p["outcome"] for p in self.pg.evaluate("getP()")}
        self.assertEqual(ids.get("p5"), "win")
        self.assertIn("late_lock", ids)

    def test_a_prop_that_lands_exactly_on_a_whole_number_line_is_a_push(self):
        r = self.settle([prop("p6", "Nikola Jokic OVER PTS 28", "DEN", "OKC", "2026-10-22")], "2026-10-22")
        self.assertEqual((r["p6"]["outcome"], r["p6"]["playerResult"]), ("push", 28))

    def test_lock_prop_does_not_relock_a_prop_the_ledger_already_has(self):
        pid = "prop_2026-10-23_NBA_LeBronJames_PTS_25.5_O"
        self.pg.evaluate("""(pid)=>{LOCKED_PROPS.length=0;saveP([{id:pid,betType:'PROP',sport:'NBA',league:'NBA',player:'LeBron James',over:true,betOn:'LeBron James OVER PTS 25.5',team:'LAL',hA:'LAL',awA:'DEN',date:'2026-10-23',outcome:'win',lockedAt:1,settledAt:5,playerResult:30,winProb:.6,decOdds:1.7,wager:0}]);
          lockProp('LAL','LeBron James',25.5,true,.62,-150,'NBA','DEN','PTS');lockProp('LAL','LeBron James',25.5,true,.62,-150,'NBA','DEN','PTS');}""", pid)
        rows = [p for p in self.pg.evaluate("getP()") if p["id"] == pid]
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["outcome"], rows[0]["lockedAt"]), ("win", 1))                                   # not reopened, not re-stamped

    # ── adjusted lines registry ──
    def test_an_adjusted_line_the_model_no_longer_adjusts_is_forgotten(self):
        mk = lambda p: {"mkts": [{"side": "over", "label": "OVER 5.5", "prob": p, "tierN": 1}, {"side": "under", "label": "UNDER 5.5", "prob": 1 - p, "tierN": 1}]}
        self.pg.evaluate("window._altReg={}")
        self.pg.evaluate("(nm)=>_altRowFor('NHL','HOMEZ','AWAYZ','2026-10-23',nm,false)", mk(.52))
        self.assertTrue(self.pg.evaluate("Object.keys(_altReg).some(k=>k.includes('HOMEZ'))"))
        self.pg.evaluate("(nm)=>_altRowFor('NHL','HOMEZ','AWAYZ','2026-10-23',nm,false)", mk(.60))                # now at the lane floor on the posted line: no adjusted line
        self.assertFalse(self.pg.evaluate("Object.keys(_altReg).some(k=>k.includes('HOMEZ'))"))

    # ── escaping ──
    def test_pick_rows_escape_external_text(self):
        html = self.pg.evaluate("""()=>_pickRowHTML({id:'x1',sport:'NHL',league:'NHL',betType:'ML',betOn:'<img src=x onerror=window.__xss=1>',hA:'<b>H</b>',awA:'<i>A</i>',ml:'<u>-110</u>',outcome:'win',hScore:'<s>1</s>',aScore:2,date:today(),lockedAt:Date.now()},{mode:'settled'})""")
        for bad in ("<img", "<b>H", "<i>A", "<u>", "<s>1"):
            self.assertNotIn(bad, html)
        self.assertIn("&lt;img", html)

    def test_escHtml_escapes_quotes_for_attributes(self):
        self.assertEqual(self.pg.evaluate("""_escHtml('a"b\\'c<d>&')"""), "a&quot;b&#39;c&lt;d&gt;&amp;")

    def test_news_links_must_be_http_or_https(self):
        self.assertEqual(self.pg.evaluate("[_safeUrl('javascript:alert(1)'),_safeUrl(' https://espn.com/x '),_safeUrl('http://a.b'),_safeUrl(null),_safeUrl('data:text/html,x')]"), ["", "https://espn.com/x", "http://a.b", "", ""])

    # ── dates ──
    def test_yesterday_is_the_calendar_day_before_the_denver_date(self):
        r = self.pg.evaluate("""()=>{const t=window.today,out=[];for(const d of ['2027-03-15','2026-11-02','2026-03-01','2027-01-01']){window.today=()=>d;out.push(yesterday());}window.today=t;return out}""")
        self.assertEqual(r, ["2027-03-14", "2026-11-01", "2026-02-28", "2026-12-31"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
