#!/usr/bin/env python3
"""scripts/calibration_watch.py -- the alert-only overconfidence WATCH. Offline, fixtures only (plus a shape check on the committed JSON).

    python3 scripts/test_calibration_watch.py
"""
import json
import math
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import calibration_watch as cw  # noqa: E402

NOW = datetime(2026, 10, 10, 18, 0, tzinfo=timezone.utc)


def pick(i, league, bt, p, win, date="2026-10-01", **kw):
    """A settled pick as the ledger stores it. startMs 2h after lockedAt = a pre-start lock unless overridden."""
    t0 = 1_790_000_000_000 + i * 60_000
    d = {"id": f"{league}-{bt}-{i}", "date": date, "sport": league, "league": league, "betType": bt, "winProb": p, "outcome": "win" if win else "loss",
         "lockedAt": t0, "startMs": t0 + 7_200_000, "settledAt": t0 + 20_000_000 + i}
    d.update(kw)
    return d


def stream(n, p, wins):
    """n picks at stated p whose outcomes are the repeating pattern `wins` (list of 0/1)."""
    return [(p, wins[i % len(wins)], f"2026-10-{1 + (i // 10) % 28:02d}") for i in range(n)]


class Replay(unittest.TestCase):
    def test_boundary_is_ln_10(self):
        self.assertAlmostEqual(cw.BOUNDARY, math.log(10), places=9)
        self.assertEqual((cw.MIN_N, cw.WINDOW, cw.DELTA), (15, 50, 0.10))

    def test_llr_increment_matches_the_formula(self):
        self.assertAlmostEqual(cw.llr_increment(.8, True), math.log(.7 / .8), places=9)
        self.assertAlmostEqual(cw.llr_increment(.8, False), math.log(.3 / .2), places=9)
        self.assertLess(cw.llr_increment(.8, True), 0)          # a win is evidence FOR the stated probability
        self.assertGreater(cw.llr_increment(.8, False), 0)

    def test_calibrated_stream_never_trips(self):
        """70% stated, wins exactly 7 of every 10 (a perfectly calibrated deterministic stream), 300 picks: the CUSUM never gets near the boundary."""
        r = cw.replay(stream(300, .70, [1, 1, 1, 0, 1, 1, 0, 1, 1, 0]))
        self.assertEqual((r["status"], r["first_trip_n"], r["since"]), ("ok", None, None))
        self.assertLess(r["llr"], 1.0)
        self.assertAlmostEqual(r["gap_pp"], 0.0, places=1)

    def test_an_underconfident_stream_never_trips(self):
        r = cw.replay(stream(120, .60, [1, 1, 1, 0]))            # stated 60, wins 75
        self.assertEqual((r["status"], r["first_trip_n"]), ("ok", None))

    def test_overconfident_stream_trips_at_a_known_n(self):
        """Stated 80%, wins every other pick (50%): L grows ~0.136 per pick and first crosses ln(10) at pick 16 (win-first) / 15 (loss-first) -- the minimum n is 15."""
        r = cw.replay(stream(60, .80, [1, 0]))
        self.assertEqual((r["status"], r["first_trip_n"], r["since"] is not None), ("watch", 16, True))
        r2 = cw.replay(stream(60, .80, [0, 1]))
        self.assertEqual((r2["status"], r2["first_trip_n"]), ("watch", 15))

    def test_min_n_is_enforced(self):
        """Ten straight losses at 80% stated is overwhelming evidence but the key still needs 15 settled picks."""
        r = cw.replay(stream(14, .80, [0]))
        self.assertGreater(r["llr"], cw.BOUNDARY)
        self.assertEqual(r["status"], "ok")
        self.assertEqual(cw.replay(stream(15, .80, [0]))["status"], "watch")

    def test_it_reports_n_mean_p_win_rate_gap_z_and_the_last_50_window(self):
        st = stream(80, .75, [1, 0, 0, 1])                       # 50% wins at 75% stated
        r = cw.replay(st)
        self.assertEqual(r["n"], 80)
        self.assertAlmostEqual(r["mean_p"], .75, places=4)
        self.assertAlmostEqual(r["win_rate"], .5, places=4)
        self.assertAlmostEqual(r["gap_pp"], -25.0, places=1)
        self.assertAlmostEqual(r["z"], (40 - 60) / math.sqrt(80 * .75 * .25), places=1)
        self.assertEqual(r["window"]["n"], 50)
        self.assertAlmostEqual(r["window"]["win_rate"], .5, places=2)

    def test_it_recovers_when_the_last_picks_are_honest_and_does_not_flap(self):
        bad = stream(30, .80, [1, 0])
        self.assertEqual(cw.replay(bad)["status"], "watch")
        good = bad + [(.80, w, "2026-10-20") for w in ([1, 1, 1, 1, 0] * 20)]          # 100 picks at 80% stated, wins exactly 80%
        r = cw.replay(good)
        self.assertEqual(r["status"], "ok")
        self.assertIsNone(r["since"])
        self.assertEqual(r["first_trip_n"], 16)                                       # the history of the first trip is kept
        again = cw.replay(good + stream(10, .80, [1, 1, 1, 1, 0]))
        self.assertEqual(again["status"], "ok")                                       # an honest tail does not re-trip a recovered key


class Ledger(unittest.TestCase):
    def rows(self, picks):
        return cw.eligible_picks(picks, {})

    def test_retired_leagues_are_ignored(self):
        picks = [pick(i, "MLB", "OU", .8, False) for i in range(30)] + [pick(i, "MLS", "ML", .8, False) for i in range(30)] + \
                [pick(i, "WNBA", "SPREAD", .8, False) for i in range(30)] + [pick(i, "BUND", "ML", .8, False) for i in range(30)]
        self.assertEqual(self.rows(picks), [])
        doc = cw.build_document([], None, NOW)
        self.assertEqual(doc["keys"], {})
        # ... while the same losses in an active league DO make a key
        doc = cw.build_document(self.rows([pick(i, "CFB", "OU", .8, False) for i in range(30)]), None, NOW)
        self.assertEqual(doc["watch"], ["CFB:OU"])

    def test_pushes_parlays_missing_probability_and_pending_are_excluded(self):
        picks = [pick(1, "CFB", "OU", .8, True, outcome="push"), pick(2, "CFB", "OU", .8, True, outcome="pending"), pick(3, "CFB", "OU", None, True),
                 pick(4, "CFB", "PARLAY", .8, True), pick(5, "CFB", "OU", 1.4, True), pick(6, "CFB", "OU", .8, True)]
        got = self.rows(picks)
        self.assertEqual([r["id"] for r in got], ["CFB-OU-6"])

    def test_known_late_locks_are_excluded_but_unknown_timing_is_kept(self):
        late = pick(1, "CFB", "OU", .8, True, lockedAt=1_790_000_000_000 + 3 * 3_600_000, startMs=1_790_000_000_000)       # locked 3h after the start
        manual = pick(2, "CFB", "OU", .8, True, lockTiming="late-manual", startMs=None)
        unknown = pick(3, "CFB", "OU", .8, True, startMs=None, lockedAt=None)
        pre = pick(4, "CFB", "OU", .8, True)
        self.assertEqual(sorted(r["id"] for r in self.rows([late, manual, unknown, pre])), ["CFB-OU-3", "CFB-OU-4"])

    def test_rl_and_pl_count_as_spread(self):
        self.assertEqual(self.rows([pick(1, "NHL", "RL", .6, True), pick(2, "NHL", "PL", .6, True)])[0]["market"], "SPREAD")

    def test_pooled_keys_euro_hockey_and_soccer(self):
        picks = [pick(1, "SHL", "SPREAD", .7, True), pick(2, "LIIGA", "SPREAD", .7, False), pick(3, "NLA", "SPREAD", .7, True), pick(4, "EXTRALIGA", "SPREAD", .7, True),
                 pick(5, "NHL", "SPREAD", .7, True), pick(6, "PL", "OU", .7, True), pick(7, "CL", "OU", .7, False), pick(8, "LIGA", "OU", .7, True), pick(9, "SERIEA", "OU", .7, True),
                 pick(10, "SHL", "OU", .7, True)]
        doc = cw.build_document(self.rows(picks), None, NOW)
        k = doc["keys"]
        self.assertEqual(k["EUROHKY:SPREAD"]["n"], 4)
        self.assertEqual(k["EUROHKY:SPREAD"]["win_rate"], .75)
        self.assertTrue(k["EUROHKY:SPREAD"]["pooled"])
        self.assertEqual(k["SOCCER:OU"]["n"], 4)
        self.assertEqual(k["EUROHKY:OU"]["n"], 1)
        self.assertEqual(k["SHL:SPREAD"]["n"], 1)
        self.assertNotIn("EUROHKY:SPREAD", [x for x in k if x.startswith("NHL")])
        self.assertEqual(k["NHL:SPREAD"]["n"], 1)                 # NHL is not part of the pooled euro family
        self.assertNotIn("PL:ML", k)

    def test_active_leagues_match_the_paid_products(self):
        import auto_lock_settle as A
        tags = set()
        for s in A.PRODUCT_SPORTS.values():
            tags |= {("SERIEA" if t == "SOC_ITA" else t[4:]) if t.startswith("SOC_") else t for t in s}      # the ledger tags Serie A 'SERIEA', the lock pipeline 'SOC_ITA'
        self.assertEqual(set(cw.ACTIVE_LEAGUES), tags)


class Dedupe(unittest.TestCase):
    def doc(self, extra_watch=()):
        picks = [pick(i, "CFB", "OU", .8, i % 2 == 0) for i in range(30)] + [pick(i, "NFL", "SPREAD", .8, i % 2 == 0) for i in range(30)] + \
                [pick(i, "NHL", "ML", .6, True) for i in range(30)] + list(extra_watch)
        return cw.build_document(cw.eligible_picks(picks, {}), None, NOW)

    def test_first_run_every_watch_key_is_new_and_names_the_numbers(self):
        d = self.doc()
        self.assertEqual(d["watch"], ["CFB:OU", "NFL:SPREAD"])
        problems, notes = cw.health_items(d)
        self.assertEqual(len(problems), 2)
        self.assertEqual(notes, [])
        self.assertTrue(all(p.startswith("CALIBRATION WATCH (new): ") and "alert only, nothing is blocked" in p for p in problems))
        self.assertIn("stated 80.0%", problems[0])
        self.assertIn("n=30", problems[0])

    def test_a_notified_key_is_not_repeated_a_new_key_is_alone(self):
        d = self.doc()
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "calibration_watch.json"
            cw.write_document(d, path)
            cw.mark_notified(["CFB:OU", "NFL:SPREAD"], "2026-10-10", path)
            old = cw.load_document(path)
            # next day: same ledger -> nothing new, both listed as known
            d2 = cw.build_document(cw.eligible_picks(
                [pick(i, "CFB", "OU", .8, i % 2 == 0) for i in range(30)] + [pick(i, "NFL", "SPREAD", .8, i % 2 == 0) for i in range(30)] + [pick(i, "NHL", "ML", .6, True) for i in range(30)], {}), old, NOW)
            problems, notes = cw.health_items(d2)
            self.assertEqual(problems, [])
            self.assertEqual(len(notes), 2)
            self.assertTrue(notes[0].startswith("calibration watch (still watching): "))
            self.assertEqual(d2["keys"]["CFB:OU"]["notified"], "2026-10-10")
            self.assertEqual(d2["keys"]["CFB:OU"]["since"], d["keys"]["CFB:OU"]["since"])
            # a third key starts losing: only it is new
            extra = [pick(i, "NBA", "OU", .8, i % 2 == 0) for i in range(30)]
            d3 = cw.build_document(cw.eligible_picks(
                [pick(i, "CFB", "OU", .8, i % 2 == 0) for i in range(30)] + [pick(i, "NFL", "SPREAD", .8, i % 2 == 0) for i in range(30)] + [pick(i, "NHL", "ML", .6, True) for i in range(30)] + extra, {}), old, NOW)
            problems, notes = cw.health_items(d3)
            self.assertEqual(len(problems), 1)
            self.assertIn("NBA:OU", problems[0])
            self.assertEqual(len(notes), 2)

    def test_a_key_that_recovers_and_re_enters_is_a_new_episode_and_alerts_again(self):
        bad = [pick(i, "CFB", "OU", .8, i % 2 == 0) for i in range(30)]
        d1 = cw.build_document(cw.eligible_picks(bad, {}), None, NOW)
        d1["keys"]["CFB:OU"]["notified"] = "2026-10-10"
        good = [pick(100 + i, "CFB", "OU", .8, (i % 5) != 4, date="2026-10-20") for i in range(100)]
        d2 = cw.build_document(cw.eligible_picks(bad + good, {}), d1, NOW)
        self.assertEqual(d2["keys"]["CFB:OU"]["status"], "ok")
        self.assertEqual(d2["watch"], [])
        worse = [pick(300 + i, "CFB", "OU", .8, i % 3 == 0, date="2026-11-02") for i in range(60)]
        d3 = cw.build_document(cw.eligible_picks(bad + good + worse, {}), d2, NOW)
        self.assertEqual(d3["keys"]["CFB:OU"]["status"], "watch")
        self.assertIsNone(d3["keys"]["CFB:OU"]["notified"])                          # new episode: the owner is told again
        self.assertEqual(len(cw.health_items(d3)[0]), 1)

    def test_update_watch_writes_the_file_keeps_notified_and_marks_after_send(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "docs").mkdir()
            picks = [pick(i, "CFB", "OU", .8, i % 2 == 0) for i in range(30)] + [pick(i, "NHL", "ML", .6, True) for i in range(20)]
            (root / "docs" / "picks_backup.json").write_text(json.dumps(picks))
            doc, problems, notes, new = cw.update_watch(root, NOW)
            self.assertEqual((new, len(problems), notes), (["CFB:OU"], 1, []))
            self.assertTrue((root / "docs" / "calibration_watch.json").exists())
            cw.mark_notified(new, "2026-10-10", root / "docs" / "calibration_watch.json")
            doc, problems, notes, new = cw.update_watch(root, NOW)                    # next day's pass
            self.assertEqual((new, problems, len(notes)), ([], [], 1))
            self.assertEqual(json.loads((root / "docs" / "calibration_watch.json").read_text())["keys"]["CFB:OU"]["notified"], "2026-10-10")

    def test_a_failed_send_leaves_the_key_un_notified_so_it_is_retried(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "docs").mkdir()
            (root / "docs" / "picks_backup.json").write_text(json.dumps([pick(i, "CFB", "OU", .8, i % 2 == 0) for i in range(30)]))
            cw.update_watch(root, NOW)                                                # no mark_notified (the email failed)
            _doc, problems, _notes, new = cw.update_watch(root, NOW)
            self.assertEqual((new, len(problems)), (["CFB:OU"], 1))


class DailyHealthCheck(unittest.TestCase):
    def test_main_wires_the_watch_through_the_existing_deduped_report(self):
        """daily_health_check.main() (full pass): a NEW watch key becomes an alert-level problem line, and is stamped notified only when _report succeeded."""
        import daily_health_check as D
        calls = {}
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "docs").mkdir()
            (root / "docs" / "picks_backup.json").write_text(json.dumps([pick(i, "CFB", "OU", .8, i % 2 == 0) for i in range(30)]))
            old = (D.ROOT, D._report, D.check_lock_markers, D.check_data_freshness, D.check_odds_sanity, D.check_ledger_archive, D.GITHUB_TOKEN, sys.argv)
            try:
                D.ROOT = root
                D.GITHUB_TOKEN = ""
                D.check_lock_markers = lambda which="full": []
                D.check_data_freshness = lambda *a, **k: []
                D.check_odds_sanity = lambda *a, **k: []
                D.check_ledger_archive = lambda *a, **k: []
                sys.argv = ["x", "--pass", "full"]

                def fake_report(problems, notes, state_path=None, today=None):
                    calls["problems"], calls["notes"] = list(problems), list(notes)
                    return calls.get("rc", 0)
                D._report = fake_report
                calls["rc"] = 1                                                        # send failed
                self.assertEqual(D.main(), 1)
                self.assertEqual(len(calls["problems"]), 1)
                self.assertIn("CALIBRATION WATCH (new): CFB:OU", calls["problems"][0])
                doc = json.loads((root / "docs" / "calibration_watch.json").read_text())
                self.assertIsNone(doc["keys"]["CFB:OU"]["notified"])                   # not stamped: will be retried
                calls["rc"] = 0                                                        # send ok
                self.assertEqual(D.main(), 0)
                self.assertEqual(len(calls["problems"]), 1)                            # still new this pass (it was never notified)
                doc = json.loads((root / "docs" / "calibration_watch.json").read_text())
                self.assertIsNotNone(doc["keys"]["CFB:OU"]["notified"])                # stamped after the successful send
                self.assertEqual(D.main(), 0)
                self.assertEqual(calls["problems"], [])                                # tomorrow: nothing to say about it ...
                self.assertTrue(any("still watching" in n for n in calls["notes"]))    # ... but it is listed as already known
            finally:
                D.ROOT, D._report, D.check_lock_markers, D.check_data_freshness, D.check_odds_sanity, D.check_ledger_archive, D.GITHUB_TOKEN, sys.argv = old

    def test_the_real_report_sends_one_email_for_all_new_keys_and_not_again_the_same_day(self):
        """Through the REAL _report (only send_email is faked): several new keys -> exactly ONE email; the same problems later the same day -> none."""
        import daily_health_check as D
        d = cw.build_document(cw.eligible_picks([pick(i, "CFB", "OU", .8, i % 2 == 0) for i in range(30)] + [pick(i, "NFL", "SPREAD", .8, i % 2 == 0) for i in range(30)], {}), None, NOW)
        problems, _notes = cw.health_items(d)
        sent = []
        old = (D.send_email, D.ALERT_TO)
        try:
            D.send_email = lambda subject, to, body: (sent.append((subject, body)) or (True, "ok"))
            D.ALERT_TO = "owner@example.test"
            with tempfile.TemporaryDirectory() as td:
                state = Path(td) / "state.json"
                self.assertEqual(D._report(list(problems), [], state, "2026-10-10"), 0)
                self.assertEqual(len(sent), 1)
                self.assertIn("CFB:OU", sent[0][1])
                self.assertIn("NFL:SPREAD", sent[0][1])
                self.assertEqual(D._report(list(problems), [], state, "2026-10-10"), 0)
                self.assertEqual(len(sent), 1)                                          # deduped
        finally:
            D.send_email, D.ALERT_TO = old

    def test_a_crashing_watch_never_hides_the_real_alerts(self):
        import daily_health_check as D
        import calibration_watch
        seen = {}
        old = (D._report, D.check_lock_markers, D.check_data_freshness, D.check_odds_sanity, D.check_ledger_archive, D.GITHUB_TOKEN, sys.argv, calibration_watch.update_watch)
        try:
            D.GITHUB_TOKEN = ""
            D.check_lock_markers = lambda which="full": ["a real problem"]
            D.check_data_freshness = lambda *a, **k: []
            D.check_odds_sanity = lambda *a, **k: []
            D.check_ledger_archive = lambda *a, **k: []
            sys.argv = ["x", "--pass", "full"]
            D._report = lambda problems, notes, *a, **k: seen.update(p=list(problems), n=list(notes)) or 0
            calibration_watch.update_watch = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
            self.assertEqual(D.main(), 0)
            self.assertEqual(seen["p"], ["a real problem"])
            self.assertTrue(any("calibration watch crashed" in n for n in seen["n"]))
        finally:
            D._report, D.check_lock_markers, D.check_data_freshness, D.check_odds_sanity, D.check_ledger_archive, D.GITHUB_TOKEN, sys.argv, calibration_watch.update_watch = old

    def test_early_and_late_passes_do_not_touch_the_watch(self):
        import daily_health_check as D
        import calibration_watch
        old = (D._report, D.check_lock_markers, calibration_watch.update_watch, sys.argv)
        try:
            D.check_lock_markers = lambda which="full": []
            D._report = lambda problems, notes, *a, **k: 0
            calibration_watch.update_watch = lambda *a, **k: self.fail("the early pass must not run the watch")
            sys.argv = ["x", "--pass", "early"]
            self.assertEqual(D.main(), 0)
        finally:
            D._report, D.check_lock_markers, calibration_watch.update_watch, sys.argv = old

    def test_weekly_digest_gets_a_short_still_watching_section(self):
        import weekly_health_digest as W
        d = cw.build_document(cw.eligible_picks([pick(i, "CFB", "OU", .8, i % 2 == 0) for i in range(30)], {}), None, NOW)
        lines = cw.weekly_lines(d)
        self.assertEqual(len(lines), 1)
        html = W.build_email_html(None, [], [], [], 0, lines)
        self.assertIn("Calibration WATCH", html)
        self.assertIn("CFB:OU", html)
        self.assertNotIn("Calibration WATCH", W.build_email_html(None, [], [], [], 0, []))
        self.assertEqual(cw.weekly_lines(None), [])


class CommittedFile(unittest.TestCase):
    def test_the_committed_json_has_the_documented_shape(self):
        p = ROOT / "docs" / "calibration_watch.json"
        self.assertTrue(p.exists(), "run: python3 scripts/calibration_watch.py --write")
        d = json.loads(p.read_text())
        for f in ("generated_at", "basis", "params", "watch", "keys"):
            self.assertIn(f, d)
        self.assertEqual(d["params"]["min_n"], 15)
        self.assertTrue(set(d["watch"]) <= set(d["keys"]))
        for k, e in d["keys"].items():
            self.assertIn(e["status"], ("ok", "watch"), k)
            for f in ("n", "mean_p", "win_rate", "gap_pp", "z", "window", "since", "notified", "first_seen"):
                self.assertIn(f, e, k)
            self.assertEqual(e["status"] == "watch", k in d["watch"], k)
            if e["status"] == "watch":
                self.assertGreaterEqual(e["n"], cw.MIN_N, k)
        for retired in ("MLB", "WNBA", "MLS", "BUND", "NCAAH", "WTA"):
            self.assertFalse([k for k in d["keys"] if k.startswith(retired + ":")], retired)

    def test_the_watch_module_is_not_imported_by_the_lock_pipeline(self):
        """Alert-only: nothing that locks, grades or prices a pick may depend on the watch."""
        for f in ("auto_lock_settle.py", "lock_prep.py", "lineups.py", "settle_gate.py"):
            self.assertNotIn("calibration_watch", (ROOT / "scripts" / f).read_text(), f)
        self.assertNotIn("calibration_watch", (ROOT / "docs" / "app.html").read_text())


if __name__ == "__main__":
    unittest.main(verbosity=2)
