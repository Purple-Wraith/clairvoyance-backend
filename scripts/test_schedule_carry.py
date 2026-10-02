#!/usr/bin/env python3
"""Tests for scripts/_schedule_carry.py and its wiring into fetch_{liiga,shl,nla,extraliga}.py (no network, no browser).

    python3 scripts/test_schedule_carry.py

Fixtures in scripts/fixtures/schedule_carry/ are REAL committed snapshots of docs/{liiga,nla,extraliga}_schedule.json from
2026-10-02 (07:5x AM, 11:5x AM and 4:0x PM Mountain), trimmed to games dated 2026-10-01..03 with the rows unmodified.  They are
the incident: games in progress at the 11:5x scrape were on neither Flashscore page, so the merge dropped them.
"""
from __future__ import annotations

import contextlib
import copy
import importlib
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _schedule_carry as C  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures" / "schedule_carry"


def snap(league, tag):
    return json.loads((FIX / f"{league}_{tag}.json").read_text())


def scrape_time(doc):
    return C.parse_generated_at(doc["generated_at"])


def d10_02(doc):
    return [g for g in doc["games"] if g["date"].startswith("2026-10-02")]


class RealIncident(unittest.TestCase):
    """07:5x snapshot (all 'pre') -> 11:5x scrape (games in progress: absent from both pages) -> 4:0x PM (all final)."""

    # league -> (games dated 10-02 at 07:5x, how many the 11:5x scrape still listed, how many vanished)
    EXPECT = {"liiga": (5, 0, 5), "nla": (5, 0, 5), "extraliga": (6, 2, 4)}

    def test_the_drop_is_real_in_the_committed_files(self):
        for lg, (n_morning, n_noon, n_gone) in self.EXPECT.items():
            with self.subTest(lg):
                morning, noon, evening = snap(lg, "0757mt" if lg == "liiga" else "0756mt"), None, None
                noon = snap(lg, {"liiga": "1153mt", "nla": "1155mt", "extraliga": "1156mt"}[lg])
                evening = snap(lg, "1604mt")
                self.assertEqual(len(d10_02(morning)), n_morning)
                self.assertEqual(len(d10_02(noon)), n_noon)  # the committed noon file really lacks them
                gone = {g["id"] for g in d10_02(morning)} - {g["id"] for g in noon["games"]}
                self.assertEqual(len(gone), n_gone)
                # ... all of them had started before that scrape ran, and all reappeared as final later
                t = scrape_time(noon)
                by_id = {g["id"]: g for g in morning["games"]}
                for gid in gone:
                    self.assertLess(C._parse_iso(by_id[gid]["date"]), t)
                ev = {g["id"]: g for g in evening["games"]}
                for gid in gone:
                    self.assertEqual(ev[gid]["state"], "post")

    def test_carry_keeps_every_started_game_at_the_noon_scrape(self):
        for lg, (n_morning, _n_noon, n_gone) in self.EXPECT.items():
            with self.subTest(lg):
                tag_m = "0757mt" if lg == "liiga" else "0756mt"
                tag_n = {"liiga": "1153mt", "nla": "1155mt", "extraliga": "1156mt"}[lg]
                morning, noon = snap(lg, tag_m), snap(lg, tag_n)
                games = copy.deepcopy(noon["games"])  # exactly what the scraper's merge produced
                res = C.carry_over_missing(games, morning, scrape_time(noon))
                self.assertEqual(len(res["carried"]), n_gone)
                self.assertEqual(len(d10_02({"games": games})), n_morning)  # every 10-02 game is back
                for g in games:
                    if g["id"] in res["carried"]:
                        self.assertIs(g["carried"], True)
                        self.assertEqual(g["lastSeen"], "2026-10-02T13:55Z")  # the 07:5x scrape that really saw it
                        self.assertEqual(g["state"], "pre")  # never promoted
                        self.assertIsNone(g["homeScore"])
                        self.assertIsNone(g["awayScore"])
                # sorted by date like the scripts' own output
                self.assertEqual([g["date"] for g in games], sorted(g["date"] for g in games))

    def test_next_scrape_replaces_carried_rows_with_the_final(self):
        lg = "liiga"
        morning, noon, evening = snap(lg, "0757mt"), snap(lg, "1153mt"), snap(lg, "1604mt")
        carried = copy.deepcopy(noon["games"])
        C.carry_over_missing(carried, morning, scrape_time(noon))
        prev_doc = {"generated_at": noon["generated_at"], "games": carried}
        new = copy.deepcopy(evening["games"])  # the 4 PM scrape sees all five as final
        res = C.carry_over_missing(new, prev_doc, scrape_time(evening))
        self.assertEqual(res["carried"], [])
        for g in d10_02({"games": new}):
            self.assertEqual(g["state"], "post")
            self.assertNotIn("carried", g)
            self.assertNotIn("lastSeen", g)

    def test_still_missing_at_the_next_scrape_keeps_the_original_lastSeen(self):
        lg = "liiga"
        morning, noon = snap(lg, "0757mt"), snap(lg, "1153mt")
        first = copy.deepcopy(noon["games"])
        C.carry_over_missing(first, morning, scrape_time(noon))
        prev_doc = {"generated_at": noon["generated_at"], "games": first}
        later = datetime(2026, 10, 2, 20, 0, tzinfo=timezone.utc)
        second = [g for g in copy.deepcopy(noon["games"])]  # a scrape that again sees none of the five
        res = C.carry_over_missing(second, prev_doc, later)
        self.assertEqual(len(res["carried"]), 5)
        for g in second:
            if g.get("carried"):
                self.assertEqual(g["lastSeen"], "2026-10-02T13:55Z")  # not bumped to the noon scrape


class Rules(unittest.TestCase):
    NOW = datetime(2026, 10, 2, 18, 0, tzinfo=timezone.utc)

    def g(self, gid, start, state="pre", hs=None, as_=None, **kw):
        d = {"id": gid, "date": start.strftime("%Y-%m-%dT%H:%MZ"), "home": "h", "homeName": "H", "away": "a", "awayName": "A",
             "state": state, "homeScore": hs, "awayScore": as_}
        d.update(kw)
        return d

    def prev(self, *games, generated="2026-10-02 12:00 UTC"):
        return {"generated_at": generated, "games": list(games)}

    def test_not_started_fixture_is_never_carried(self):
        # a fixture that merely dropped off the page must not become a phantom upcoming game (the lock pipeline would see it)
        games = []
        res = C.carry_over_missing(games, self.prev(self.g("fut", self.NOW + timedelta(hours=1)),
                                                    self.g("soon", self.NOW + timedelta(minutes=1))), self.NOW)
        self.assertEqual(games, [])
        self.assertEqual(res["carried"], [])

    def test_exactly_at_start_counts_as_started(self):
        games = []
        C.carry_over_missing(games, self.prev(self.g("x", self.NOW)), self.NOW)
        self.assertEqual([g["id"] for g in games], ["x"])

    def test_dropped_after_three_days(self):
        old = self.g("old", self.NOW - timedelta(days=3, minutes=1))
        edge = self.g("edge", self.NOW - timedelta(days=3))
        games = []
        res = C.carry_over_missing(games, self.prev(old, edge), self.NOW)
        self.assertEqual(res["expired"], ["old"])
        self.assertEqual([g["id"] for g in games], ["edge"])

    def test_present_game_is_untouched(self):
        fresh = self.g("a", self.NOW - timedelta(hours=3), state="post", hs=3, as_=2)
        games = [fresh]
        res = C.carry_over_missing(games, self.prev(self.g("a", self.NOW - timedelta(hours=3))), self.NOW)
        self.assertEqual(res["carried"], [])
        self.assertEqual(games, [fresh])
        self.assertNotIn("carried", games[0])

    def test_carried_post_keeps_its_real_scores(self):
        games = []
        C.carry_over_missing(games, self.prev(self.g("p", self.NOW - timedelta(hours=30), state="post", hs=4, as_=1)), self.NOW)
        self.assertEqual((games[0]["state"], games[0]["homeScore"], games[0]["awayScore"]), ("post", 4, 1))
        self.assertTrue(games[0]["carried"])

    def test_carried_row_can_never_be_a_scoreless_post(self):
        bad = [self.g("b1", self.NOW - timedelta(hours=5), state="post"),
               self.g("b2", self.NOW - timedelta(hours=5), state="post", hs=2),
               self.g("b3", self.NOW - timedelta(hours=5), state="post", hs="2", as_="1"),
               self.g("b4", self.NOW - timedelta(hours=5), state="post", hs=True, as_=False)]
        games = []
        C.carry_over_missing(games, self.prev(*bad), self.NOW)
        self.assertEqual(len(games), 4)
        for g in games:
            self.assertEqual(g["state"], "pre", g["id"])
            self.assertIsNone(g["homeScore"])
            self.assertIsNone(g["awayScore"])

    def test_never_mutates_the_previous_doc(self):
        pdoc = self.prev(self.g("m", self.NOW - timedelta(hours=2)))
        before = copy.deepcopy(pdoc)
        C.carry_over_missing([], pdoc, self.NOW)
        self.assertEqual(pdoc, before)

    def test_odds_and_other_fields_survive(self):
        odds = {"ml": {"home": 1.5, "away": 2.5}, "at": "2026-10-02T12:00Z"}
        games = []
        C.carry_over_missing(games, self.prev(self.g("o", self.NOW - timedelta(hours=2), odds=odds, ou=5.5)), self.NOW)
        self.assertEqual(games[0]["odds"], odds)
        self.assertEqual(games[0]["ou"], 5.5)

    def test_malformed_previous_rows_are_skipped_not_fatal(self):
        weird = [None, "x", {}, {"id": "nodate"}, {"id": "badate", "date": "tomorrow-ish"}, {"date": "2026-10-02T10:00Z"}]
        games = []
        res = C.carry_over_missing(games, self.prev(*weird, self.g("good", self.NOW - timedelta(hours=1))), self.NOW)
        self.assertEqual(res["carried"], ["good"])
        for bad in (None, {}, {"games": None}, {"games": "oops"}, {"generated_at": "??", "games": [self.g("z", self.NOW - timedelta(hours=1))]}):
            C.carry_over_missing([], bad, self.NOW)  # must not raise

    def test_load_previous_is_fail_open(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(C.load_previous(Path(td) / "missing.json"), {})
            p = Path(td) / "bad.json"
            p.write_text("{not json")
            self.assertEqual(C.load_previous(p), {})
            p.write_text("[1,2]")
            self.assertEqual(C.load_previous(p), {})

    def test_naive_now_is_treated_as_utc(self):
        games = []
        C.carry_over_missing(games, self.prev(self.g("n", self.NOW - timedelta(hours=1))), self.NOW.replace(tzinfo=None))
        self.assertEqual(len(games), 1)


class DownstreamSafety(unittest.TestCase):
    """The carried rows against the real consumers: the pre-start lock guard and the settle matcher's preconditions."""

    def test_carried_started_game_is_refused_by_the_lock_guard(self):
        import auto_lock_settle as A
        morning, noon = snap("liiga", "0757mt"), snap("liiga", "1153mt")
        games = copy.deepcopy(noon["games"])
        C.carry_over_missing(games, morning, scrape_time(noon))
        now = scrape_time(noon)  # a lock pass running right after that scrape
        carried = [g for g in games if g.get("carried")]
        self.assertTrue(carried)
        for g in carried:
            start_ms = C._parse_iso(g["date"]).timestamp() * 1000.0
            ok, reason, _ = A.start_guard("LIIGA", start_ms, now=now)  # app.html passes startMs = Date.parse(g.date)
            self.assertFalse(ok, reason)

    def test_settle_precondition_never_true_for_a_carried_pre_row(self):
        # mirrors the first line of _autoSettleFlashscoreHockey's loop: skip unless state==='post' && both scores != null
        morning, noon = snap("liiga", "0757mt"), snap("liiga", "1153mt")
        games = copy.deepcopy(noon["games"])
        C.carry_over_missing(games, morning, scrape_time(noon))
        for g in games:
            if g.get("carried"):
                settleable = g["state"] == "post" and g["homeScore"] is not None and g["awayScore"] is not None
                self.assertFalse(settleable)


def _load_fetch(name):
    return importlib.import_module(name)


class FetchScriptWiring(unittest.TestCase):
    """Drive each fetch script's real run() with the browser/scrape functions stubbed: the 11:5x scrape returns exactly the
    games the committed noon file lists (fixtures + results), the previous file is the 07:5x one."""

    LEAGUES = {"liiga": ("fetch_liiga", "0757mt", "1153mt"), "nla": ("fetch_nla", "0756mt", "1155mt"),
               "extraliga": ("fetch_extraliga", "0756mt", "1156mt")}

    def _run(self, mod_name, prev_doc, scraped_doc, now, extra_fixture=True):
        m = _load_fetch(mod_name)

        class FakeDT(datetime):
            @classmethod
            def now(cls, tz=None):
                return now

        fixtures = [g for g in scraped_doc["games"] if g["state"] == "pre"]
        if extra_fixture:  # the trimmed real fixtures keep only 10-01..03; the real scrape also listed later fixtures
            fixtures.append({**scraped_doc["games"][0], "id": "FUTURE1", "date": "2026-10-09T15:30Z", "state": "pre",
                             "homeScore": None, "awayScore": None})
        results = [g for g in scraped_doc["games"] if g["state"] == "post"]

        @contextlib.contextmanager
        def fake_pw():
            pw = mock.MagicMock()
            yield pw

        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "sched.json"
            if prev_doc is not None:
                out.write_text(json.dumps(prev_doc))
            with mock.patch.object(m, "OUT", out), mock.patch.object(m, "datetime", FakeDT), \
                    mock.patch.object(m, "sync_playwright", fake_pw), \
                    mock.patch.object(m, "fetch_standings", lambda *a, **k: {}), \
                    mock.patch.object(m, "fetch_gm_rates", lambda *a, **k: {}), \
                    mock.patch.object(m, "fetch_fixtures", lambda *a, **k: copy.deepcopy(fixtures)), \
                    mock.patch.object(m, "fetch_results", lambda *a, **k: copy.deepcopy(results)), \
                    mock.patch.object(m, "log_scrape", mock.MagicMock()) as ls:
                try:
                    m.run()
                    written = json.loads(out.read_text())
                    err = None
                except Exception as exc:  # noqa: BLE001
                    written, err = None, exc
                return written, err, ls

    def test_run_carries_started_games_and_reports_scraped_counts(self):
        for lg, (mod, tm, tn) in self.LEAGUES.items():
            with self.subTest(lg):
                morning, noon = snap(lg, tm), snap(lg, tn)
                written, err, ls = self._run(mod, morning, noon, scrape_time(noon))
                self.assertIsNone(err)
                n_morning = len(d10_02(morning))
                self.assertEqual(len(d10_02(written)), n_morning)
                carried = [g for g in written["games"] if g.get("carried")]
                self.assertTrue(carried)
                for g in carried:
                    self.assertEqual(g["state"], "pre")
                    self.assertIsNone(g["homeScore"])
                # scrape-health counts are the SCRAPED rows, carried rows excluded
                args = ls.call_args[0]
                self.assertEqual(args[1], sum(1 for g in noon["games"] if g["state"] == "pre") + 1)  # +1: the synthetic future fixture
                self.assertEqual(args[2], sum(1 for g in noon["games"] if g["state"] == "post"))

    def test_zero_fixture_guard_still_fires_with_carried_rows_present(self):
        # a failed fixtures scrape (0 'pre' rows) must still abort the write even though carry-over would add 'pre' rows
        lg, (mod, tm, tn) = "liiga", self.LEAGUES["liiga"]
        morning, noon = snap(lg, tm), snap(lg, tn)
        scraped = {"games": [g for g in noon["games"] if g["state"] == "post"]}
        written, err, _ = self._run(mod, morning, scraped, scrape_time(noon), extra_fixture=False)
        self.assertIsNotNone(err)
        self.assertIn("Refusing to overwrite", str(err))
        self.assertIsNone(written)

    def test_first_run_without_a_previous_file_still_works(self):
        lg, (mod, tm, tn) = "liiga", self.LEAGUES["liiga"]
        noon = snap(lg, tn)
        written, err, _ = self._run(mod, None, noon, scrape_time(noon))
        self.assertIsNone(err)
        self.assertFalse(any(g.get("carried") for g in written["games"]))

    def test_all_four_scripts_use_the_shared_carry(self):
        for name in ("fetch_liiga", "fetch_shl", "fetch_nla", "fetch_extraliga"):
            src = (Path(__file__).resolve().parent / f"{name}.py").read_text()
            self.assertIn("carry_over_missing(games, load_previous(OUT), now)", src, name)
            self.assertLess(src.index('if fixtures_count == 0 and OUT.exists():'),
                            src.index("carry_over_missing(games, load_previous(OUT), now)"), name + ": guard must precede carry")


if __name__ == "__main__":
    unittest.main(verbosity=1)
