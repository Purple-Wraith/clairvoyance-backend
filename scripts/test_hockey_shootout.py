#!/usr/bin/env python3
"""European hockey totals are graded the way books grade them: regulation + overtime, WITHOUT the shootout-winning goal.

Flashscore lists a game decided in a shootout with the stage tag "Pen" and a final score that INCLUDES the shootout goal (1-1 after
overtime, won in the shootout, is listed 2-1).  The four scrapers (fetch_{shl,liiga,nla,extraliga}.py) now write `ouTotal` on such a
row and docs/app.html's _autoSettleFlashscoreHockey grades OVER/UNDER against it.

    python3 scripts/test_hockey_shootout.py

Row HTML below is the real Flashscore results-row structure (captured 2026-10-23 from /hockey/finland/liiga/results/).
"""
from __future__ import annotations

import functools
import http.server
import importlib
import os
import socketserver
import sys
import threading
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = Path(os.environ.get("CV_ROOT") or HERE.parent)
sys.path.insert(0, str(HERE))
import _schedule_carry as C  # noqa: E402

LEAGUES = ("shl", "liiga", "nla", "extraliga")


def row(mid, home, away, hs, as_, stage=""):
    hslug, aslug = home.lower(), away.lower()
    tag = f'<span class="wcl-simple-text-01_EaZ9- wcl-stageContent_fuKCx">{stage}</span>' if stage else ""
    return f"""<div class="event__match event__match--withRowLink" data-event-row="true">
      <a href="https://www.flashscore.com/match/hockey/{hslug}-AAAAAA1/{aslug}-BBBBBB2/?mid={mid}" class="eventRowLink"></a>
      <span class="event__stageTime event__stageTime--date"><span class="wcl-dateContent_eEChT">20.10. 09:30</span>{tag}</span>
      <div class="event__homeParticipant"><span class="wcl-name_jjfMf">{home}</span></div>
      <div class="event__awayParticipant"><span class="wcl-name_jjfMf">{away}</span></div>
      <div class="event__score event__score--home">{hs}</div><div class="event__score event__score--away">{as_}</div></div>"""


HTML = "<body>" + "".join([
    row("so1", "Kookoo", "Tappara", 2, 1, "Pen"),        # 1-1 after overtime, home won the shootout
    row("ot1", "Lukko", "Ilves", 3, 2, "AOT"),           # won in overtime: the OT goal counts
    row("rg1", "Karpat", "Sport", 4, 1),                 # regulation
    row("so2", "Jukurit", "Hpk", 1, 2, "Pen"),           # away won the shootout
]) + "</body>"


class Helper(unittest.TestCase):
    def test_shootout_removes_the_winning_goal(self):
        self.assertEqual(C.shootout_fields("Pen", 2, 1), {"ouTotal": 2})
        self.assertEqual(C.shootout_fields("Pen", 1, 2), {"ouTotal": 2})
        self.assertEqual(C.shootout_fields(" pen ", 4, 3), {"ouTotal": 6})

    def test_overtime_and_regulation_rows_get_nothing(self):
        for stage in ("AOT", "", None, "Finished", "Awarded"):
            self.assertEqual(C.shootout_fields(stage, 3, 2), {})

    def test_a_shootout_row_that_cannot_be_one_is_left_alone(self):
        self.assertEqual(C.shootout_fields("Pen", 3, 3), {})          # a shootout always leaves a one-goal margin
        self.assertEqual(C.shootout_fields("Pen", 5, 1), {})
        self.assertEqual(C.shootout_fields("Pen", None, 1), {})
        self.assertEqual(C.shootout_fields("Pen", "2", "1"), {})


class Scrapers(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        cls.pw = sync_playwright().start(); cls.b = cls.pw.chromium.launch(); cls.pg = cls.b.new_page()
        cls.pg.set_content(HTML)

        class Stub:                       # fetch_results navigates and clicks "show more"; the fixture page is already loaded
            def goto(self, *a, **k): pass
            def wait_for_timeout(self, *a, **k): pass
            def query_selector(self, sel): return None if "event__more" in sel else cls.pg.query_selector(sel)
            def query_selector_all(self, sel): return cls.pg.query_selector_all(sel)
        cls.stub = Stub()

    @classmethod
    def tearDownClass(cls):
        cls.b.close(); cls.pw.stop()

    def results(self, lg):
        m = importlib.import_module(f"fetch_{lg}")
        return {g["id"]: g for g in m.fetch_results(self.stub, 2026, {})}

    def test_every_scraper_flags_only_the_shootout_games(self):
        for lg in LEAGUES:
            with self.subTest(league=lg):
                r = self.results(lg)
                self.assertEqual(set(r), {"so1", "ot1", "rg1", "so2"})
                self.assertEqual(r["so1"]["ouTotal"], 2)
                self.assertEqual(r["so2"]["ouTotal"], 2)
                self.assertNotIn("ouTotal", r["ot1"])
                self.assertNotIn("ouTotal", r["rg1"])

    def test_the_final_score_itself_is_untouched(self):
        for lg in LEAGUES:
            with self.subTest(league=lg):
                r = self.results(lg)
                self.assertEqual((r["so1"]["homeScore"], r["so1"]["awayScore"]), (2, 1))
                self.assertEqual((r["so2"]["homeScore"], r["so2"]["awayScore"]), (1, 2))

    def test_a_refreshed_result_adds_ouTotal_to_the_game_already_in_the_file(self):
        from datetime import datetime, timezone
        prev = {"games": [{"id": "so1", "date": "2026-10-20T09:30Z", "state": "post", "homeScore": 2, "awayScore": 1, "ou": 5.5}]}
        doc, stats = C.merge_results_only(prev, [self.results("liiga")["so1"]], datetime(2026, 10, 23, tzinfo=timezone.utc))
        g = doc["games"][0]
        self.assertEqual((g["ouTotal"], g["ou"], g["homeScore"]), (2, 5.5, 2))
        self.assertEqual(stats["updated"], ["so1"])


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a, **k): pass


class Settler(unittest.TestCase):
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
        cls.pg.wait_for_function("typeof _autoSettleFlashscoreHockey==='function'&&typeof saveP==='function'"); cls.pg.wait_for_timeout(1200)

    @classmethod
    def tearDownClass(cls):
        cls.b.close(); cls.pw.stop(); cls.srv.shutdown()

    def grade(self, bet, **game):
        g = {"id": "g1", "date": "2026-10-20T15:30:00Z", "state": "post", "homeName": "Kookoo", "awayName": "Tappara", "homeScore": 3, "awayScore": 2, **game}
        pick = {"id": "t1", "betType": "OU", "sport": "HOCKEY", "league": "LIIGA", "betOn": bet, "hA": "Kookoo", "awA": "Tappara", "date": "2026-10-20",
                "outcome": "pending", "lockedAt": 1, "winProb": .6, "decOdds": 1.9}
        self.pg.evaluate("(p)=>saveP([p])", pick)
        self.pg.evaluate("([g])=>_autoSettleFlashscoreHockey('2026-10-20',async()=>({games:[g]}),'LIIGA')", [g])
        self.pg.wait_for_timeout(300)
        return self.pg.evaluate("getP().find(p=>p.id==='t1').outcome")

    def test_over_loses_when_the_only_goal_past_the_line_was_the_shootout_winner(self):
        # listed 3-2 (5 total) but it was 2-2 after overtime: books grade 4 goals
        self.assertEqual(self.grade("OVER 4.5", ouTotal=4), "loss")
        self.assertEqual(self.grade("UNDER 4.5", ouTotal=4), "win")

    def test_a_shootout_total_that_lands_on_the_line_is_a_push(self):
        self.assertEqual(self.grade("OVER 4", ouTotal=4), "push")

    def test_without_the_flag_the_listed_score_is_the_total(self):
        self.assertEqual(self.grade("OVER 4.5"), "win")
        self.assertEqual(self.grade("UNDER 4.5"), "loss")

    def test_an_overtime_or_regulation_game_is_not_adjusted(self):
        self.assertEqual(self.grade("OVER 4.5", homeScore=4, awayScore=1), "win")      # 5 goals in regulation


if __name__ == "__main__":
    unittest.main(verbosity=2)
