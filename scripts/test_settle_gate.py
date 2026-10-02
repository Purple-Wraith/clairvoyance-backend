#!/usr/bin/env python3
"""Tests for scripts/settle_gate.py (the cheap settle gate), _schedule_carry.merge_results_only and
refresh_hockey_results.apply_results (no network, no browser, no Supabase).

    python3 scripts/test_settle_gate.py

Fixtures: scripts/fixtures/schedule_carry/*.json (real committed schedule snapshots of 2026-10-02) and
scripts/fixtures/settle_gate/pending_picks_20261002.json (the real pending European-hockey picks of that day, unmodified).
"""
from __future__ import annotations

import contextlib
import copy
import io
import json
import re
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _schedule_carry as C  # noqa: E402
import refresh_hockey_results as R  # noqa: E402
import settle_gate as G  # noqa: E402

HERE = Path(__file__).resolve().parent
FIX = HERE / "fixtures" / "schedule_carry"
PICKS = json.loads((HERE / "fixtures" / "settle_gate" / "pending_picks_20261002.json").read_text())["picks"]


def snap(league, tag):
    return json.loads((FIX / f"{league}_{tag}.json").read_text())


def utc(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=timezone.utc)


def schedules(tag_by_league):
    return {lg.upper(): snap(lg, tag) for lg, tag in tag_by_league.items()}


EVENING = {"liiga": "1604mt", "nla": "1604mt", "extraliga": "1604mt"}  # all final (4:04 PM MT scrape)
NOON = {"liiga": "1153mt", "nla": "1155mt", "extraliga": "1156mt"}      # games in progress: absent
MORNING = {"liiga": "0757mt", "nla": "0756mt", "extraliga": "0756mt"}   # all 'pre'


def pick(tag="LIIGA", home="KalPa", away="SaiPa", bet="SaiPa", date="2026-10-02", outcome="pending", **kw):
    p = {"id": f"{date}_{home}_{away}_{tag}_{bet}", "sport": tag, "league": tag, "hA": home, "awA": away, "betOn": bet,
         "date": date, "outcome": outcome}
    p.update(kw)
    return p


def game(home="KalPa", away="SaiPa", start="2026-10-02T15:30Z", state="post", hs=4, as_=3, **kw):
    g = {"id": "g1", "date": start, "homeName": home, "awayName": away, "state": state, "homeScore": hs, "awayScore": as_}
    g.update(kw)
    return g


def sched(*games, tag="LIIGA"):
    return {tag: {"games": list(games)}}


class Helpers(unittest.TestCase):
    def test_sport_tag(self):
        self.assertEqual(G.sport_tag({"sport": "LIIGA"}), "LIIGA")
        self.assertEqual(G.sport_tag({"sport": "hockey", "league": "shl"}), "SHL")  # broad bucket -> league wins
        self.assertEqual(G.sport_tag({"league": "NLA"}), "NLA")
        self.assertIsNone(G.sport_tag({"sport": "NHL", "league": "NHL"}))
        self.assertIsNone(G.sport_tag({"sport": "CFB"}))

    def test_classify_matches_the_app(self):
        self.assertEqual(G.classify_bet_on("OVER 4.5"), {"type": "OU", "over": True, "line": 4.5})
        self.assertEqual(G.classify_bet_on("under 6.5")["over"], False)
        self.assertEqual(G.classify_bet_on("Karpat -1.5"), {"type": "SPREAD", "team": "Karpat", "line": -1.5})
        self.assertEqual(G.classify_bet_on("Kometa Brno +1.5")["team"], "Kometa Brno")
        self.assertEqual(G.classify_bet_on("Lugano ML"), {"type": "ML"})
        self.assertEqual(G.classify_bet_on("Over"), {"type": "ML"})  # no number: falls through exactly like the JS

    def test_app_can_grade(self):
        g = game()
        for ok in ("SaiPa", "KalPa", "SaiPa ML", "KalPa ML", "OVER 5.5", "SaiPa -1.5", "KalPa +1.5"):
            self.assertTrue(G.app_can_grade(pick(bet=ok), g), ok)
        for bad in ("Someone Else", "Jokerit -1.5", "SaiPa Win", "Over"):
            self.assertFalse(G.app_can_grade(pick(bet=bad), g), bad)


class JsParity(unittest.TestCase):
    """The Python ports against the REAL JS in docs/app.html (node), over every betOn string in the ledger backup."""

    @classmethod
    def setUpClass(cls):
        try:
            subprocess.run(["node", "--version"], capture_output=True, check=True)
        except Exception:
            raise unittest.SkipTest("node not available")

    def test_classify_bet_on_parity(self):
        html = (HERE.parent / "docs" / "app.html").read_text()
        m = re.search(r"function _classifyBetOn\(betOn\)\{.*?\n\}\n", html, re.S)
        self.assertIsNotNone(m)
        picks = json.loads((HERE.parent / "docs" / "picks_backup.json").read_text())
        bets = sorted({p.get("betOn") for p in picks if p.get("betOn")} | {"", "OVER 4.5", "Over", "X +1.5", "Team -0.5"})
        js = m.group(0) + "\nconst bets=" + json.dumps(bets) + ";\nconsole.log(JSON.stringify(bets.map(b=>_classifyBetOn(b))));"
        out = subprocess.run(["node", "-e", js], capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        js_res = json.loads(out.stdout)
        for b, jr in zip(bets, js_res):
            self.assertEqual(G.classify_bet_on(b), jr, b)


class Stages(unittest.TestCase):
    NOW = utc(2026, 10, 2, 22, 20)  # 4:20 PM MT

    def run_gate(self, picks, scheds, stage, now=None):
        return G.evaluate(picks, scheds, now or self.NOW, stage)

    # ---- stage 1 ----
    def test_candidates_noop_with_nothing_pending(self):
        r = self.run_gate([pick(outcome="win"), pick(tag="NHL")], sched(game()), "candidates")
        self.assertFalse(r["go"])
        self.assertEqual(r["pending"], 0)

    def test_candidates_noop_while_the_game_cannot_be_over(self):
        g = game(state="pre", hs=None, as_=None, start="2026-10-02T21:00Z")  # started 80 min ago
        self.assertFalse(self.run_gate([pick()], sched(g), "candidates")["go"])

    def test_candidates_go_once_the_game_could_be_over(self):
        g = game(state="pre", hs=None, as_=None, start="2026-10-02T20:00Z")  # started 140 min ago
        self.assertTrue(self.run_gate([pick()], sched(g), "candidates")["go"])

    def test_candidates_go_when_already_final_or_game_missing(self):
        self.assertTrue(self.run_gate([pick()], sched(game()), "candidates")["go"])
        self.assertTrue(self.run_gate([pick()], sched(), "candidates")["go"])

    def test_candidates_ignore_future_and_stale_pick_dates(self):
        self.assertFalse(self.run_gate([pick(date="2026-10-03")], sched(), "candidates")["go"])
        self.assertFalse(self.run_gate([pick(date="2026-09-20")], sched(), "candidates")["go"])
        self.assertTrue(self.run_gate([pick(date="2026-09-26")], sched(), "candidates")["go"])  # 6 days back: still in the window

    # ---- stage 2 ----
    def test_final_go_when_game_final_and_gradable(self):
        r = self.run_gate([pick()], sched(game()), "final")
        self.assertTrue(r["go"])
        self.assertIn("FINAL", r["reasons"][0])

    def test_final_noop_when_not_final(self):
        pre = game(state="pre", hs=None, as_=None)
        self.assertFalse(self.run_gate([pick()], sched(pre), "final")["go"])
        carried_pre = game(state="pre", hs=None, as_=None, carried=True, lastSeen="2026-10-02T13:55Z")
        self.assertFalse(self.run_gate([pick()], sched(carried_pre), "final")["go"])
        scoreless_post = game(state="post", hs=None, as_=None)
        self.assertFalse(self.run_gate([pick()], sched(scoreless_post), "final")["go"])

    def test_final_noop_when_the_app_could_not_grade_it(self):
        self.assertFalse(self.run_gate([pick(bet="Some other text")], sched(game()), "final")["go"])

    def test_final_requires_the_pick_date_to_equal_the_games_mountain_date(self):
        # 03:30Z on 10-03 is 9:30 PM MT on 10-02: a 10-02 pick matches it; a 10-03 pick must not
        late = game(start="2026-10-03T03:30Z")
        self.assertTrue(self.run_gate([pick(date="2026-10-02")], sched(late), "final")["go"])
        self.assertFalse(self.run_gate([pick(date="2026-10-03")], sched(late), "final", now=utc(2026, 10, 3, 20))["go"])

    def test_final_matches_either_orientation_and_wrong_league_never(self):
        self.assertTrue(self.run_gate([pick(home="SaiPa", away="KalPa")], sched(game()), "final")["go"])
        self.assertFalse(self.run_gate([pick(tag="SHL")], sched(game(), tag="LIIGA"), "final")["go"])

    # ---- real data ----
    def test_real_morning_and_noon_and_evening_snapshots(self):
        # the real pending picks of 10-02 against the real schedule snapshots, clock at each scrape time
        morning = self.run_gate(PICKS, schedules(MORNING), "final", now=utc(2026, 10, 2, 14, 0))
        self.assertFalse(morning["go"])  # everything still 'pre'
        noon = self.run_gate(PICKS, schedules(NOON), "final", now=utc(2026, 10, 2, 17, 52))
        # the incident: the noon file holds only Extraliga's 2 early finals -> nothing for Liiga/NLA, and Extraliga's two games'
        # picks only; without the carry fix the other picks could never be graded here
        self.assertTrue(noon["go"])
        evening = self.run_gate(PICKS, schedules(EVENING), "final", now=utc(2026, 10, 2, 22, 20))
        self.assertTrue(evening["go"])
        self.assertGreater(len(evening["reasons"]), len(noon["reasons"]))
        # only the picks the app genuinely cannot grade are left out (none of the real 10-02 ones: they use bare team names,
        # "<Team> ML" or spreads/totals)
        self.assertEqual(len(evening["reasons"]), len(PICKS))

    def test_real_noon_with_carry_does_not_pretend_unfinished_games_are_final(self):
        noon, morning = snap("liiga", "1153mt"), snap("liiga", "0757mt")
        games = copy.deepcopy(noon["games"])
        C.carry_over_missing(games, morning, C.parse_generated_at(noon["generated_at"]))
        r = self.run_gate([p for p in PICKS if p["sport"] == "LIIGA"], {"LIIGA": {"games": games}}, "final",
                          now=utc(2026, 10, 2, 17, 52))
        self.assertFalse(r["go"])  # carried rows are 'pre' -> no pick can be graded off them
        # ... but stage 1 correctly says a re-scrape is worthwhile once the games could be over (started 15:30Z; 140 min later)
        r1 = self.run_gate([p for p in PICKS if p["sport"] == "LIIGA"], {"LIIGA": {"games": games}}, "candidates",
                           now=utc(2026, 10, 2, 18, 0))
        self.assertTrue(r1["go"])
        r1_early = self.run_gate([p for p in PICKS if p["sport"] == "LIIGA"], {"LIIGA": {"games": games}}, "candidates",
                                 now=utc(2026, 10, 2, 16, 30))
        self.assertFalse(r1_early["go"])  # an hour after puck drop: not worth a scrape


class Cli(unittest.TestCase):
    def docs(self, td, picks, scheds):
        d = Path(td)
        (d / "picks_backup.json").write_text(json.dumps(picks))
        for tag, fn in G.SCHEDULE_FILES.items():
            if tag in scheds:
                (d / fn).write_text(json.dumps(scheds[tag]))
        return d

    def call(self, d, stage, now):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = G.main(["--stage", stage, "--docs", str(d), "--now", now])
        return rc, buf.getvalue()

    def test_exit_codes_and_output(self):
        with tempfile.TemporaryDirectory() as td:
            d = self.docs(td, PICKS, schedules(EVENING))
            rc, out = self.call(d, "final", "2026-10-02T22:20:00Z")
            self.assertEqual(rc, 0)
            self.assertIn("GO", out)
            d2 = self.docs(td, [], schedules(EVENING))
            rc, out = self.call(d2, "candidates", "2026-10-02T22:20:00Z")
            self.assertEqual(rc, 1)
            self.assertIn("NO-OP", out)

    def test_fails_open_on_unreadable_inputs(self):
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "picks_backup.json").write_text("{broken")
            rc, out = self.call(Path(td), "final", "2026-10-02T22:20:00Z")
            self.assertEqual(rc, 0)
            self.assertIn("failing open", out)
        with tempfile.TemporaryDirectory() as td:  # missing picks file entirely
            rc, out = self.call(Path(td), "candidates", "2026-10-02T22:20:00Z")
            self.assertEqual(rc, 0)


class MergeResultsOnly(unittest.TestCase):
    def test_real_noon_then_results_scrape(self):
        morning, noon, evening = snap("liiga", "0757mt"), snap("liiga", "1153mt"), snap("liiga", "1604mt")
        games = copy.deepcopy(noon["games"])
        C.carry_over_missing(games, morning, C.parse_generated_at(noon["generated_at"]))
        prev = {"generated_at": noon["generated_at"], "teams": {"x": 1}, "games": games}
        results = [g for g in evening["games"] if g["state"] == "post" and g["date"].startswith("2026-10-02")]
        before = copy.deepcopy(prev)
        doc, stats = C.merge_results_only(prev, results, utc(2026, 10, 2, 22, 5))
        self.assertEqual(prev, before)  # input untouched
        self.assertEqual(sorted(stats["updated"]), sorted(g["id"] for g in results))
        self.assertEqual(stats["added"], [])
        by = {g["id"]: g for g in doc["games"]}
        ev = {g["id"]: g for g in evening["games"]}
        for g in results:
            row = by[g["id"]]
            self.assertEqual((row["state"], row["homeScore"], row["awayScore"]), ("post", g["homeScore"], g["awayScore"]))
            self.assertNotIn("carried", row)
            self.assertNotIn("lastSeen", row)
            self.assertEqual(row["odds"], ev[g["id"]]["odds"] if "odds" in ev[g["id"]] else row["odds"])  # prev odds survived
            self.assertIn("odds", row)
        self.assertEqual(doc["generated_at"], noon["generated_at"])  # the FULL refresh stamp is untouched
        self.assertEqual(doc["resultsRefreshedAt"], "2026-10-02 22:05 UTC")
        self.assertEqual(doc["teams"], {"x": 1})
        # every non-result game is byte-identical
        rid = {g["id"] for g in results}
        for g in prev["games"]:
            if g["id"] not in rid:
                self.assertEqual(by[g["id"]], g)

    def test_idle_scrape_changes_nothing(self):
        prev = {"generated_at": "x", "games": [game(), game(state="pre", hs=None, as_=None, id="g2")]}
        prev["games"][0]["id"] = "g1"
        doc, stats = C.merge_results_only(prev, [copy.deepcopy(prev["games"][0])], utc(2026, 10, 2, 22))
        self.assertEqual(doc, prev)
        self.assertEqual(stats, {"updated": [], "added": [], "unchanged": 1})
        self.assertNotIn("resultsRefreshedAt", doc)

    def test_unknown_game_is_added_and_scoreless_results_are_ignored(self):
        prev = {"games": [game(id="a")]}
        new = game(id="b", start="2026-10-02T15:30Z")
        junk = [game(id="c", state="pre", hs=None, as_=None), game(id="d", state="post", hs=None, as_=None),
                {"id": None, "state": "post", "homeScore": 1, "awayScore": 0}, "x", None]
        doc, stats = C.merge_results_only(prev, [new] + junk, utc(2026, 10, 2, 22))
        self.assertEqual(stats["added"], ["b"])
        self.assertEqual([g["id"] for g in doc["games"]], ["a", "b"])

    def test_pre_rows_and_odds_are_never_touched_by_other_results(self):
        pre = game(id="p", state="pre", hs=None, as_=None, odds={"ml": {"home": 1.5}}, ou=5.5)
        prev = {"games": [pre, game(id="q", state="pre", hs=None, as_=None, start="2026-10-02T14:00Z")]}
        doc, _ = C.merge_results_only(prev, [game(id="q", hs=2, as_=1, start="2026-10-02T14:00Z")], utc(2026, 10, 2, 22))
        self.assertEqual(next(g for g in doc["games"] if g["id"] == "p"), pre)

    def test_apply_results_writes_only_on_change_and_never_on_dry_run(self):
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "s.json"
            prev = {"generated_at": "x", "games": [game(id="a", state="pre", hs=None, as_=None)]}
            f.write_text(json.dumps(prev, indent=2))
            mtime = f.stat().st_mtime_ns
            st = R.apply_results(f, [game(id="a", hs=3, as_=1)], utc(2026, 10, 2, 22), dry_run=True)
            self.assertEqual(st["updated"], ["a"])
            self.assertEqual(json.loads(f.read_text()), prev)  # dry run: untouched
            st = R.apply_results(f, [], utc(2026, 10, 2, 22))
            self.assertIn("skipped", st)
            self.assertEqual(f.stat().st_mtime_ns, mtime)
            st = R.apply_results(f, [game(id="a", hs=3, as_=1)], utc(2026, 10, 2, 22))
            self.assertEqual(json.loads(f.read_text())["games"][0]["state"], "post")
            st = R.apply_results(f, [game(id="a", hs=3, as_=1)], utc(2026, 10, 2, 23))
            self.assertEqual(st["unchanged"], 1)
            self.assertEqual(json.loads(f.read_text())["resultsRefreshedAt"], "2026-10-02 22:00 UTC")  # idle pass didn't restamp
            missing = Path(td) / "nope.json"
            self.assertIn("skipped", R.apply_results(missing, [game()], utc(2026, 10, 2, 22)))
            self.assertFalse(missing.exists())

    def test_team_names_from_previous_file(self):
        self.assertEqual(R.team_names_from({"teams": {"a": {"name": "A"}, "b": {}, "c": 5}}), {"a": "A"})
        self.assertEqual(R.team_names_from({}), {})


if __name__ == "__main__":
    unittest.main(verbosity=1)
