#!/usr/bin/env python3
"""Over/Under records from Flashscore's standings tab (NHL + the four European hockey leagues): scripts/_flashscore_ou.py, fetch_nhl_ou.py, the euro fetch scripts,
the committed docs/nhl_ou.json and the OVER RATE row on the cards (docs/app.html).  No network.

    python3 scripts/test_ou_records.py
"""
from __future__ import annotations

import functools, http.server, json, os, socketserver, sys, threading, unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
ROOT = Path(os.environ.get("CV_ROOT") or HERE.parent)
sys.path.insert(0, str(HERE))
import _flashscore_ou as OU  # noqa: E402

LINES = ["4.5", "5.5", "6.5", "7.5"]


def rec(gp, over):
    """record for one scope from a list of over counts at 4.5/5.5/6.5/7.5"""
    return {"gp": gp, "ou": {l: [o, gp - o] for l, o in zip(LINES, over)}}


class Pure(unittest.TestCase):
    def test_ou_base_strips_the_scope_and_line(self):
        want = "https://www.flashscore.com/hockey/finland/liiga/standings/C8KZXayI/over_under"
        for u in (want + "/overall/6.5/", want + "/overall/", want + "/", want, want + "/home/4.5"):
            self.assertEqual(OU.ou_base(u), want)

    def test_clean_scope_accepts_a_real_record(self):
        self.assertTrue(OU.clean_scope({4.5: dict(gp=12, over=11, under=1), 5.5: dict(gp=12, over=10, under=2), 6.5: dict(gp=12, over=8, under=4), 7.5: dict(gp=12, over=5, under=7)}))

    def test_clean_scope_rejects_rows_that_do_not_add_up(self):
        base = {4.5: dict(gp=12, over=11, under=1), 5.5: dict(gp=12, over=10, under=2)}
        self.assertFalse(OU.clean_scope({**base, 6.5: dict(gp=12, over=8, under=3)}))                 # over + under != games
        self.assertFalse(OU.clean_scope({**base, 6.5: dict(gp=12, over=11, under=1)}))                # over count rises with the line
        self.assertFalse(OU.clean_scope({**base, 6.5: dict(gp=13, over=8, under=5)}))                 # games played changes between lines
        self.assertFalse(OU.clean_scope({}))

    def test_the_lines_are_the_four_the_owner_asked_for(self):
        self.assertEqual(OU.LINES, (4.5, 5.5, 6.5, 7.5)); self.assertEqual(OU.SCOPES, ("overall", "home", "away"))


class Rows(unittest.TestCase):
    """parse_rows on the real Flashscore row markup (captured 2026-10-24 from the NHL Over/Under tab)"""
    ROW = """<div class="ui-table__row"><div class="table__cell table__cell--rank"><div class="tableCellRank">1.</div></div>
      <div class="table__cell table__cell--participant"><div class="tableCellParticipant"><div class="tableCellParticipant__block">
      <a class="tableCellParticipant__image" href="/team/vegas-golden-knights/fTT0TsUg/"></a><a href="/team/vegas-golden-knights/fTT0TsUg/" class="tableCellParticipant__name">Vegas Golden Knights</a></div></div></div>
      <span class="table__cell table__cell--value">12</span><span class="table__cell table__cell--value table__cell--over">10</span><span class="table__cell table__cell--value table__cell--under">2</span>
      <span class="table__cell table__cell--value">44:36</span><span class="table__cell table__cell--value">6.7</span></div>"""

    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        cls.pw = sync_playwright().start(); cls.b = cls.pw.chromium.launch(); cls.pg = cls.b.new_page()
        bad = '<div class="ui-table__row"><span class="table__cell table__cell--value">x</span></div>'          # header-ish / malformed row
        cls.pg.set_content("<body>" + cls.ROW + bad + "</body>")

    @classmethod
    def tearDownClass(cls):
        cls.b.close(); cls.pw.stop()

    def test_a_row_is_read_into_numbers(self):
        r = OU.parse_rows(self.pg.query_selector_all("[class*='ui-table__row']"))
        self.assertEqual(list(r), ["fTT0TsUg"])
        self.assertEqual(r["fTT0TsUg"], {"name": "Vegas Golden Knights", "gp": 12, "over": 10, "under": 2, "gf": 44, "ga": 36, "gm": 6.7})


class FakePage:
    pass


class EuroPair(unittest.TestCase):
    """fetch_ou_pair: current + prior records, previous-file carry-over, fail-open, and no re-scrape of the (static) prior season"""

    def run_pair(self, scrape, prev_doc):
        with mock.patch.object(OU, "scrape_teams", scrape):
            return OU.fetch_ou_pair(object(), "https://x/liiga/standings/AAA/over_under/overall/6.5/", "https://x/liiga-2025-2026/standings/BBB/over_under/overall/5.5/", prev_doc)

    def test_scrapes_current_and_prior(self):
        calls = []

        def scrape(ctx, base, scopes, lines, log):
            calls.append((base, scopes))
            return {"t1": {"overall": rec(10, [9, 8, 5, 3]), "home": rec(5, [5, 4, 3, 2]), "away": rec(5, [4, 4, 2, 1])}} if "BBB" not in base else {"t1": {"overall": rec(60, [50, 40, 25, 12])}}
        cur, prior = self.run_pair(scrape, {})
        self.assertEqual(set(cur["t1"]), {"overall", "home", "away"})
        self.assertEqual(prior["t1"]["gp"], 60)
        self.assertEqual([c[0].endswith("over_under") for c in calls], [True, True]); self.assertEqual(calls[1][1], ("overall",))

    def test_prior_season_is_not_scraped_again_once_the_file_has_it(self):
        prev = {"teams": {f"t{i}": {"prevSeason": {"ou": rec(60, [50, 40, 25, 12])}} for i in range(6)}}
        calls = []

        def scrape(ctx, base, scopes, lines, log):
            calls.append(base)
            return {f"t{i}": {"overall": rec(10, [9, 8, 5, 3])} for i in range(6)}
        cur, prior = self.run_pair(scrape, prev)
        self.assertEqual(len(calls), 1); self.assertEqual(len(prior), 6)

    def test_a_failed_scrape_falls_back_to_the_previous_files_records_and_never_raises(self):
        prev = {"teams": {"t1": {"ou": {"overall": rec(10, [9, 8, 5, 3])}, "prevSeason": {"ou": rec(60, [50, 40, 25, 12])}}}}

        def boom(*a, **k):
            raise RuntimeError("flashscore down")
        cur, prior = self.run_pair(boom, prev)
        self.assertEqual(cur["t1"]["overall"]["gp"], 10); self.assertEqual(prior["t1"]["gp"], 60)

    def test_a_failed_scrape_with_no_previous_file_is_just_empty(self):
        def boom(*a, **k):
            raise RuntimeError("down")
        self.assertEqual(self.run_pair(boom, None), ({}, {}))


class Wiring(unittest.TestCase):
    def test_every_euro_scraper_collects_and_stores_the_records(self):
        for lg in ("liiga", "shl", "nla", "extraliga"):
            src = (HERE / f"fetch_{lg}.py").read_text()
            self.assertIn("from _flashscore_ou import fetch_ou_pair", src, lg)
            self.assertIn("fetch_ou_pair(context, CURRENT_OU_URL, PRIOR_OU_URL, load_previous(OUT), log)", src, lg)
            self.assertIn('teams[team_id]["ou"] = ou_cur[team_id]', src, lg)
            self.assertIn('prev = {**prev, "ou": ou_prior[team_id]}', src, lg)

    def test_the_refresh_workflow_runs_the_nhl_scraper(self):
        wf = (ROOT / ".github" / "workflows" / "hockey-euro-refresh.yml").read_text()
        self.assertIn("python3 scripts/fetch_nhl_ou.py --push", wf)


class NhlFile(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.doc = json.loads((ROOT / "docs" / "nhl_ou.json").read_text())

    def test_all_32_teams_with_clean_records_and_the_four_lines(self):
        self.assertEqual(self.doc["lines"], [4.5, 5.5, 6.5, 7.5]); self.assertEqual(len(self.doc["teams"]), 32)
        for code, t in self.doc["teams"].items():
            ov = t["overall"]
            self.assertEqual(sorted(ov["ou"]), LINES, code)
            for l, (o, u) in ov["ou"].items():
                self.assertEqual(o + u, ov["gp"], f"{code} {l}")
            overs = [ov["ou"][l][0] for l in LINES]
            self.assertEqual(overs, sorted(overs, reverse=True), code)                  # over counts only fall as the line rises
            if "home" in t and "away" in t:
                self.assertEqual(t["home"]["gp"] + t["away"]["gp"], ov["gp"], code)
                for l in LINES:
                    self.assertEqual(t["home"]["ou"][l][0] + t["away"]["ou"][l][0], ov["ou"][l][0], f"{code} {l} home+away")

    def test_codes_are_the_apps_nhl_codes(self):
        codes = set(self.doc["teams"])
        for need in ("TBL", "LAK", "NJD", "SJS", "UTA", "VGK", "CAR"):
            self.assertIn(need, codes)
        self.assertEqual(len(self.doc["prev"]["teams"]), 32)

    def test_prior_season_is_a_full_82_game_record(self):
        for code, t in self.doc["prev"]["teams"].items():
            self.assertEqual(t["gp"], 82, code)


class Basis(unittest.TestCase):
    """Flashscore counts REGULATION goals; books settle regulation + overtime (shootout goal excluded).  The two differ only at the even-total lines."""

    def test_total_bases(self):
        import backtest_hockey_models as H
        g = lambda hs, as_, ot: dict(hs=hs, as_=as_, ot=ot)
        self.assertEqual((H.reg_total(g(3, 2, "REG")), H.book_total(g(3, 2, "REG"))), (5, 5))
        self.assertEqual((H.reg_total(g(3, 2, "OT")), H.book_total(g(3, 2, "OT"))), (4, 5))      # 2-2 after 60, OT goal: books count it, Flashscore does not
        self.assertEqual((H.reg_total(g(3, 2, "SO")), H.book_total(g(3, 2, "SO"))), (4, 4))      # shootout goal: neither counts it

    def test_the_shift_only_touches_4_5_and_6_5(self):
        import backtest_hockey_models as H
        games = [dict(hs=3, as_=2, ot="OT"), dict(hs=4, as_=3, ot="OT"), dict(hs=3, as_=2, ot="SO"), dict(hs=2, as_=1, ot="REG")]
        sh = H.ot_tie_shift(games)
        self.assertEqual(sh[4.5], 0.25); self.assertEqual(sh[6.5], 0.25); self.assertEqual(sh[5.5], 0.0); self.assertEqual(sh[7.5], 0.0)


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a, **k):
        pass


class Card(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        socketserver.TCPServer.allow_reuse_address = True
        cls.srv = socketserver.TCPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=str(ROOT / "docs")))
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.pw = sync_playwright().start(); cls.b = cls.pw.chromium.launch(); cls.pg = cls.b.new_page()
        cls.pg.route("**/*espn.com/**", lambda r: r.abort()); cls.pg.route("**/*workers.dev/**", lambda r: r.abort())
        cls.pg.goto(f"http://127.0.0.1:{cls.srv.server_address[1]}/app.html?nosb=1")
        cls.pg.wait_for_function("typeof _hkOuRecordRowHTML==='function'&&typeof loadNhlOuData==='function'"); cls.pg.wait_for_timeout(800)

    @classmethod
    def tearDownClass(cls):
        cls.b.close(); cls.pw.stop(); cls.srv.shutdown()

    def row(self, lg, g):
        return self.pg.evaluate("([lg,g])=>{const d=document.createElement('div');d.innerHTML=_hkOuRecordRowHTML(lg,g);return d.innerText.replace(/\\s+/g,' ')}", [lg, g])

    def nhl(self, teams, prev=None):
        self.pg.evaluate("([t,p])=>{_NHL_OU={teams:t,prev:{teams:p||{}}}}", [teams, prev])

    def test_nhl_row_shows_home_team_at_home_and_road_team_on_the_road(self):
        self.nhl({"VGK": {"overall": rec(12, [11, 10, 8, 5]), "home": rec(7, [6, 6, 5, 3]), "away": rec(5, [5, 4, 3, 2])},
                  "TBL": {"overall": rec(10, [9, 6, 3, 3]), "home": rec(5, [4, 2, 0, 0]), "away": rec(5, [5, 4, 3, 3])}})
        t = self.row("nhl", {"home": "VGK", "away": "TBL"})
        self.assertIn("OVER RATE · 60 MIN", t)
        self.assertIn("TBL O4.5 100% · O5.5 80% · O6.5 60% · O7.5 60% 5 GP on the road", t)        # the visitor's ROAD games
        self.assertIn("VGK O4.5 86% · O5.5 86% · O6.5 71% · O7.5 43% 7 GP at home", t)             # the host's HOME games
        self.assertIn("does not change any pick", t)

    def test_a_split_with_fewer_than_5_games_uses_all_games_and_a_new_team_uses_last_season(self):
        self.nhl({"VGK": {"overall": rec(8, [7, 6, 4, 2]), "home": rec(3, [3, 3, 2, 1]), "away": rec(5, [4, 3, 2, 1])}, "TBL": {"overall": rec(3, [3, 2, 1, 1])}},
                 {"TBL": {"gp": 82, "ou": rec(82, [60, 45, 28, 15])["ou"]}})
        t = self.row("nhl", {"home": "VGK", "away": "TBL"})
        self.assertIn("VGK O4.5 88% · O5.5 75% · O6.5 50% · O7.5 25% 8 GP all games", t)
        self.assertIn("TBL O4.5 73% · O5.5 55% · O6.5 34% · O7.5 18% 82 GP last season", t)

    def test_no_data_or_unknown_teams_give_no_row(self):
        self.nhl({})
        self.assertEqual(self.row("nhl", {"home": "VGK", "away": "TBL"}), "")
        self.pg.evaluate("_NHL_OU=null"); self.assertEqual(self.row("nhl", {"home": "VGK", "away": "TBL"}), "")

    def test_a_euro_card_reads_the_leagues_own_team_records(self):
        self.pg.evaluate("""()=>{_LIIGA_DATA={teams:{h1:{name:'Tappara',ou:{overall:%s,home:%s}},a1:{name:'<b>Ilves</b>',ou:{overall:%s,away:%s}}},games:[]}}""" % tuple(json.dumps(x) for x in (
            rec(17, [13, 12, 8, 7]), rec(8, [6, 5, 5, 5]), rec(17, [12, 10, 7, 5]), rec(9, [7, 7, 3, 2]))))
        t = self.row("liiga", {"home": "h1", "away": "a1"})
        self.assertIn("Tappara O4.5 75% · O5.5 63% · O6.5 63% · O7.5 63% 8 GP at home", t)
        self.assertIn("<b>Ilves</b> O4.5 78% · O5.5 78% · O6.5 33% · O7.5 22% 9 GP on the road", t)         # shown as text, never executed
        html = self.pg.evaluate("_hkOuRecordRowHTML('liiga',{home:'h1',away:'a1'})")
        self.assertNotIn("<b>Ilves", html)

    def test_the_cards_carry_the_row(self):
        src = (ROOT / "docs" / "app.html").read_text()
        for lg in ("nhl", "liiga", "shl", "nla", "extraliga"):
            self.assertIn(f"_hkOuRecordRowHTML('{lg}',g)", src, lg)
        self.assertIn("await loadNhlOuData()", src)

    def test_no_pick_probability_reads_the_records(self):
        src = (ROOT / "docs" / "app.html").read_text()
        for name in ("_ouRecPick", "_ouRecItem", "_hkOuRecordRowHTML", "_NHL_OU"):
            uses = [i for i in range(len(src)) if src.startswith(name, i)]
            for i in uses:
                line = src[src.rfind("\n", 0, i) + 1: src.find("\n", i)]
                self.assertNotRegex(line, r"nhlEns|liigaEns|shlEns|nlaEns|extraligaEns|winProb|_hkBlendCore|lockNHL|lockHockey", line)


if __name__ == "__main__":
    unittest.main(verbosity=2)
