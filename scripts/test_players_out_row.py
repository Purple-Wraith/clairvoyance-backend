#!/usr/bin/env python3
"""PLAYERS OUT row on NFL and NHL game cards (owner 2026-11-01, same idea as the NBA card's PLAYERS OUT & REST).  Display only: it feeds no probability, price, grade or lock.

    python3 scripts/test_players_out_row.py
"""
from __future__ import annotations

import functools
import http.server
import json
import os
import socketserver
import threading
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = Path(os.environ.get("CV_ROOT") or HERE.parent)

NHL_FEED = {"injuries": [
    {"displayName": "Tampa Bay Lightning", "injuries": [
        {"status": "Day-To-Day", "athlete": {"displayName": "Zemgus Girgensons", "position": {"abbreviation": "C"}, "team": {"abbreviation": "TB"}}},
        {"status": "Out", "athlete": {"displayName": "Andrei <b>Vasilevskiy</b>", "position": {"abbreviation": "G"}, "team": {"abbreviation": "TB"}}},
        {"status": "Injured Reserve", "athlete": {"displayName": "Erik Cernak", "position": {"abbreviation": "D"}, "team": {"abbreviation": "TB"}}},
        {"status": "Injured Reserve", "athlete": {"displayName": "Nick Paul", "position": {"abbreviation": "LW"}, "team": {"abbreviation": "TB"}}},
        {"status": "Injured Reserve", "athlete": {"displayName": "Brayden Point", "position": {"abbreviation": "C"}, "team": {"abbreviation": "TB"}}},
        {"status": "Active", "athlete": {"displayName": "Healthy Guy", "position": {"abbreviation": "C"}, "team": {"abbreviation": "TB"}}},
    ]},
    {"displayName": "Anaheim Ducks", "injuries": [
        {"status": "Suspension", "athlete": {"displayName": "Suspended Sam", "position": {"abbreviation": "RW"}, "team": {"abbreviation": "ANA"}}},
        {"status": "Out", "athlete": {"displayName": "No Team Guy", "position": {"abbreviation": "C"}}},          # no team abbreviation: skipped
    ]},
]}


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a, **k):
        pass


class Row(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        socketserver.TCPServer.allow_reuse_address = True
        cls.srv = socketserver.TCPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=str(ROOT / "docs")))
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch()
        cls.pg = cls.browser.new_page()
        cls.errors = []
        cls.pg.on("pageerror", lambda e: cls.errors.append(str(e)))
        for pat in ("**/*workers.dev*", "**/*open-meteo*/**", "**/*nominatim*/**"):
            cls.pg.route(pat, lambda r: r.abort())
        cls.pg.route("**/*espn*/**", lambda r: r.abort())                    # Playwright runs the LAST-registered matching route first, so the fixture goes after the catch-all
        cls.pg.route("**/hockey/nhl/injuries*", lambda r: r.fulfill(status=200, content_type="application/json", body=json.dumps(NHL_FEED)))
        cls.pg.goto(f"http://127.0.0.1:{cls.srv.server_address[1]}/app.html?nosb=1&nosw=1")
        cls.pg.wait_for_function("typeof _nflOutRowHTML==='function'&&typeof _nhlOutRowHTML==='function'&&typeof _outRowItems==='function'&&typeof _nflGameCard2==='function'&&typeof _NFL_DATA!=='undefined'&&!!_NFL_DATA")
        cls.pg.wait_for_timeout(800)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()
        cls.srv.shutdown()

    def ev(self, js, arg=None):
        return self.pg.evaluate(js, arg) if arg is not None else self.pg.evaluate(js)

    # ── the shared row builder ────────────────────────────────────────────────────────────────────────────────────────
    def test_certain_absences_come_first_then_the_key_position_and_maybes_last(self):
        rows = [{"player": "Punter P", "position": "P", "status": "Out"}, {"player": "Quarterback Q", "position": "QB", "status": "Questionable"},
                {"player": "Guard G", "position": "G", "status": "Injured Reserve"}, {"player": "Runner R", "position": "RB", "status": "Out"},
                {"player": "Corner C", "position": "CB", "status": "Doubtful"}, {"player": "Fine F", "position": "WR", "status": "Active"}]
        out = self.ev("(r)=>_outRowItems(r,'KC','h',_OUT_POS_RANK,3)", rows)
        self.assertEqual([x["text"] for x in out[:3]], ["Runner R", "Guard G", "Corner C"])   # RB(out), G(IR), CB(doubtful); the QB is only questionable so he sorts after the certain absences
        self.assertEqual([x["tag"] for x in out[:3]], ["OUT", "IR", "DOUBTFUL"])
        self.assertEqual([x["sub"] for x in out[:3]], ["RB", "G", "CB"])
        self.assertEqual(out[3]["text"], "+2 more")                                              # 5 listed players (Active is ignored) - 3 shown
        self.assertTrue(all(x["team"] == "KC" and x["side"] == "h" for x in out))

    def test_a_questionable_qb_ranks_above_a_questionable_punter_and_unknown_statuses_are_ignored(self):
        rows = [{"player": "P", "position": "P", "status": "Questionable"}, {"player": "Q", "position": "QB", "status": "Questionable"}, {"player": "X", "position": "QB", "status": "Probable"}]
        out = self.ev("(r)=>_outRowItems(r,'A','a',_OUT_POS_RANK,3)", rows)
        self.assertEqual([x["text"] for x in out], ["Q", "P"])
        self.assertEqual(self.ev("()=>_outRowItems([],'A','a',_OUT_POS_RANK,3)"), [])
        self.assertEqual(self.ev("()=>_outRowItems(undefined,'A','a',_OUT_POS_RANK,3)"), [])

    def test_names_are_html_escaped(self):
        out = self.ev("()=>_outRowItems([{player:'<img src=x onerror=1>',position:'QB',status:'Out'}],'A','a',_OUT_POS_RANK,3)")
        self.assertNotIn("<img", out[0]["text"])
        self.assertIn("&lt;img", out[0]["text"])

    # ── NFL ───────────────────────────────────────────────────────────────────────────────────────────────────────────
    def nfl_row(self, game_js, gen="new Date().toISOString()", teams=None):
        return self.ev("""([g,gen,teams])=>{const D=_NFL_DATA,old={g:D.generatedAt,i:D.injuriesByTeam};
          D.generatedAt=Object.assign({},D.generatedAt,{injuries:eval(gen)});D.injuriesByTeam=teams||{AAA:[{player:'Alpha One',position:'QB',status:'Out'}],BBB:[{player:'Beta Two',position:'WR',status:'Injured Reserve'}]};
          const game=eval('('+g+')');const h=_nflOutRowHTML(game);D.generatedAt=old.g;D.injuriesByTeam=old.i;return h}""", [game_js, gen, teams])

    def test_nfl_row_lists_both_teams_with_positions_and_tags(self):
        h = self.nfl_row("{away:'AAA',home:'BBB',date:new Date(Date.now()+86400000).toISOString(),state:'pre'}")
        self.assertIn("PLAYERS OUT", h)
        self.assertIn("Alpha One", h)
        self.assertIn("Beta Two", h)
        self.assertIn(">QB<", h)
        self.assertIn("OUT", h)
        self.assertIn(">IR<", h)
        self.assertLess(h.index("Alpha One"), h.index("Beta Two"))                              # away team first, like the NBA row

    def test_nfl_row_is_hidden_when_stale_over_or_far_ahead_or_without_data(self):
        game = "{away:'AAA',home:'BBB',date:new Date(Date.now()+86400000).toISOString(),state:'pre'}"
        self.assertEqual(self.nfl_row(game, gen="new Date(Date.now()-70*3600e3).toISOString()"), "")             # injury file older than 60 h
        self.assertEqual(self.nfl_row("{away:'AAA',home:'BBB',date:new Date(Date.now()-3600e3).toISOString(),state:'post'}"), "")   # game is over
        self.assertEqual(self.nfl_row("{away:'AAA',home:'BBB',date:new Date(Date.now()+9*86400000).toISOString(),state:'pre'}"), "")  # more than 8 days away
        self.assertEqual(self.nfl_row("{away:'ZZZ',home:'YYY',date:new Date(Date.now()+86400000).toISOString(),state:'pre'}"), "")    # neither team has anyone listed
        self.assertEqual(self.ev("()=>_nflOutRowHTML(null)"), "")

    def test_nfl_card_shows_the_row_above_the_weather_row(self):
        html = self.ev("""()=>{const D=_NFL_DATA,old={g:D.generatedAt,i:D.injuriesByTeam};
          const g=Object.values(D.weeks).flat().find(x=>x.state!=='post');
          D.generatedAt=Object.assign({},D.generatedAt,{injuries:new Date().toISOString()});
          D.injuriesByTeam={[g.away]:[{player:'Zed Marker',position:'RB',status:'Out'}],[g.home]:[]};
          const gg=Object.assign({},g,{date:new Date(Date.now()+86400000).toISOString(),state:'pre'});
          const h=_nflGameCard2(gg);D.generatedAt=old.g;D.injuriesByTeam=old.i;return h}""")
        self.assertIn("PLAYERS OUT", html)
        self.assertIn("Zed Marker", html)
        if "WEATHER" in html:
            self.assertLess(html.index("PLAYERS OUT"), html.index("WEATHER"))

    # ── NHL ───────────────────────────────────────────────────────────────────────────────────────────────────────────
    def test_nhl_feed_is_keyed_by_the_athletes_team_abbreviation_and_aliases_work(self):
        self.ev("()=>{_NHL_INJ={ts:0,by:{}};return _nhlPrefetchInj()}")
        keys = self.ev("()=>Object.keys(_NHL_INJ.by).sort()")
        self.assertEqual(keys, ["ANA", "TB"])                                                    # the athlete with no team abbreviation is skipped
        h = self.ev("()=>_nhlOutRowHTML({home:'TBL',away:'ANA',state:'pre'})")                   # the app's key is TBL, ESPN's is TB
        self.assertIn("PLAYERS OUT", h)
        self.assertIn("Suspended Sam", h)
        self.assertIn("&lt;b&gt;Vasilevskiy&lt;/b&gt;", h)                                       # escaped
        self.assertNotIn("<b>Vasilevskiy", h)
        self.assertNotIn("No Team Guy", h)
        self.assertNotIn("Healthy Guy", h)                                                       # 'Active' is not an absence

    def test_nhl_goalie_first_then_other_certain_absences_then_day_to_day_and_capped_at_three(self):
        self.ev("()=>{_NHL_INJ={ts:0,by:{}};return _nhlPrefetchInj()}")
        items = self.ev("()=>_outRowItems(_nhlInjFor('TBL'),'TBL','h',{G:0,C:1,LW:2,RW:2,W:2,D:3},3)")
        names = [x["text"] for x in items]
        self.assertTrue(names[0].startswith("Andrei"))                                           # the goalie (Out) first
        self.assertEqual([x["tag"] for x in items[:3]], ["OUT", "IR", "IR"])
        self.assertNotIn("Zemgus Girgensons", " ".join(names[:3]))                               # day-to-day is behind every certain absence
        self.assertEqual(items[3]["text"], "+2 more")

    def test_nhl_row_hidden_for_final_games_unknown_teams_and_before_the_feed_loads(self):
        self.ev("()=>{_NHL_INJ={ts:0,by:{}};return _nhlPrefetchInj()}")
        self.assertEqual(self.ev("()=>_nhlOutRowHTML({home:'TBL',away:'ANA',state:'post'})"), "")
        self.assertEqual(self.ev("()=>_nhlOutRowHTML({home:'BOS',away:'NSH',state:'pre'})"), "")
        self.assertEqual(self.ev("()=>_nhlOutRowHTML(null)"), "")
        self.ev("()=>{_NHL_INJ={ts:0,by:{}}}")
        self.assertEqual(self.ev("()=>_nhlOutRowHTML({home:'TBL',away:'ANA',state:'pre'})"), "")  # nothing fetched yet: no row, no error

    def test_nhl_schedule_card_includes_the_row_and_both_renderers_wait_for_the_feed(self):
        html = self.ev("""async()=>{_NHL_INJ={ts:0,by:{}};await _nhlPrefetchInj();
          return _nhlUpcomingCardRaw({home:'TBL',away:'ANA',date:new Date(Date.now()+86400000).toISOString(),state:'pre',id:'t1',homeML:null,awayML:null},0)}""")
        self.assertIn("PLAYERS OUT", html)
        self.assertIn("Suspended Sam", html)
        self.assertLess(html.index("STARTING GOALIES") if "STARTING GOALIES" in html else 0, html.index("PLAYERS OUT"))   # right under the goalie row
        src = (ROOT / "docs" / "app.html").read_text()
        self.assertIn("${_nhlOutRowHTML({home:hn,away:an,state:state})}", src)                   # today's ESPN card (_nhlGameCard)
        self.assertEqual(src.count("try{await _nhlPrefetchInj();}catch(e){}"), 2)                # renderNHLWeek + the schedule-day renderer fetch the feed before they build cards

    # ── display only ──────────────────────────────────────────────────────────────────────────────────────────────────
    def test_rows_never_mutate_the_injury_data(self):
        before = self.ev("()=>JSON.stringify([_NFL_DATA.injuriesByTeam,_NHL_INJ.by])")
        self.ev("()=>{_nhlOutRowHTML({home:'TBL',away:'ANA',state:'pre'});_nflOutRowHTML({away:'KC',home:'BUF',state:'pre',date:new Date(Date.now()+86400000).toISOString()})}")
        self.assertEqual(self.ev("()=>JSON.stringify([_NFL_DATA.injuriesByTeam,_NHL_INJ.by])"), before)

    def test_no_page_errors(self):
        self.assertEqual(self.errors, [])


if __name__ == "__main__":
    unittest.main()
