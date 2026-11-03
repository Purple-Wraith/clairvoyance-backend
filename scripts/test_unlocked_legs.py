#!/usr/bin/env python3
"""The daily health check's count of unlocked legs (owner 2026-11-02).  The watchdog records what became of every qualifying-but-unlocked leg it saw (scripts/watchdog_legs.py, data/watchdog_legs.json) and the
health check counts yesterday's: 'missed' legs (open before kickoff, then the game started unlocked) are an alert, held / not-qualified / seen-only-after-kickoff legs are a note.  No network, no browser.

    python3 scripts/test_unlocked_legs.py
"""
import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import auto_lock_settle as a  # noqa: E402
import daily_health_check as H  # noqa: E402
import watchdog_legs as W  # noqa: E402
from test_watchdog_autolock import Harness, NOW, q  # noqa: E402

MT = ZoneInfo("America/Denver")
MIN = 60000


def leg(game="AWY @ HOM", name="HOM ML", mins=60, code="owed", sport="NFL"):
    return {"sport": sport, "game": game, "leg": name, "startMs": NOW + mins * MIN, "code": code}


class Transitions(unittest.TestCase):
    def setUp(self):
        self.s = {"days": {}}

    def status(self, l):
        day = W._day(l["startMs"])
        return self.s["days"][day][W.leg_key(l["sport"], l["game"], l["leg"])]["status"]

    def test_code_for_maps_a_watchdog_classification(self):
        self.assertEqual(W.code_for(True, "the lock passes qualify it"), "owed")
        self.assertEqual(W.code_for(False, "held by the confirmed-lineup gate on purpose"), "held")
        self.assertEqual(W.code_for(False, "the lock passes' own run (lineup gate / selection) does not qualify it"), "unqualified")

    def test_an_open_leg_is_recorded_once_and_a_repeat_changes_nothing(self):
        l = leg()
        self.assertTrue(W.update(self.s, [l], NOW))
        self.assertEqual(self.status(l), "owed")
        self.assertFalse(W.update(self.s, [l], NOW + 20 * MIN))               # same status: nothing to write (no commit per watchdog run)

    def test_a_leg_that_gets_locked_before_kickoff_is_cleared_and_not_counted(self):
        l = leg()
        W.update(self.s, [l], NOW)
        self.assertTrue(W.update(self.s, [], NOW + 30 * MIN))                  # no longer unlocked
        self.assertEqual(self.status(l), "cleared")
        self.assertEqual(W.summary(self.s, W._day(l["startMs"]))["total"], 0)

    def test_an_owed_leg_still_unlocked_after_kickoff_is_a_miss(self):
        l = leg(mins=30)
        W.update(self.s, [l], NOW)
        W.update(self.s, [l], NOW + 45 * MIN)                                  # the game started, the leg is still open
        self.assertEqual(self.status(l), "missed")

    def test_held_and_unqualified_legs_stay_what_they_were_after_the_start(self):
        h, u = leg(name="KC ML", code="held"), leg(name="ATL ML", code="unqualified")
        W.update(self.s, [h, u], NOW)
        W.update(self.s, [h, u], NOW + 120 * MIN)
        self.assertEqual((self.status(h), self.status(u)), ("held", "unqualified"))

    def test_a_leg_first_seen_after_kickoff_is_late(self):
        l = leg(mins=-33)
        W.update(self.s, [l], NOW)
        self.assertEqual(self.status(l), "late")

    def test_a_hold_that_lifts_before_kickoff_becomes_owed(self):
        h = leg(code="held")
        W.update(self.s, [h], NOW)
        W.update(self.s, [dict(h, code="owed")], NOW + 10 * MIN)
        self.assertEqual(self.status(h), "owed")

    def test_a_started_leg_that_ages_out_of_the_window_keeps_its_status(self):
        l = leg(mins=30, code="owed")
        W.update(self.s, [l], NOW)
        W.update(self.s, [l], NOW + 45 * MIN)                                  # missed
        W.update(self.s, [], NOW + 30 * MIN + 4 * 3600 * 1000)                 # 4 h after the start: out of the watch window, not 'cleared'
        self.assertEqual(self.status(l), "missed")

    def test_old_days_are_pruned(self):
        for d in range(12):
            W.update(self.s, [dict(leg(name=f"L{d}"), startMs=NOW + d * 86400000)], NOW + d * 86400000 - 3600000)
        self.assertLessEqual(len(self.s["days"]), W.KEEP_DAYS)

    def test_summary_counts_per_day_and_lists_the_legs(self):
        a_, b_, c_ = leg(name="A", mins=30), leg(name="B", code="held"), leg(name="C", mins=-10)
        W.update(self.s, [a_, b_, c_], NOW)
        W.update(self.s, [a_, c_], NOW + 50 * MIN)                             # B is no longer open (cleared); A is still open after its kickoff; C (first seen after its kickoff) is still open
        sm = W.summary(self.s, W._day(a_["startMs"]))
        self.assertEqual((sm["missed"], sm["held"], sm["late"], sm["total"]), (1, 0, 1, 2))
        self.assertEqual(sm["legs"]["missed"], ["AWY @ HOM A"])


class WatchdogWritesTheRecord(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()) / "watchdog_legs.json"
        self.orig = (a.LEGS_STATE_PATH, a.LEGS_PERSIST)
        a.LEGS_STATE_PATH, a.LEGS_PERSIST = self.tmp, True

    def tearDown(self):
        a.LEGS_STATE_PATH, a.LEGS_PERSIST = self.orig

    def run_wd(self, legs, now, live=True, locked=()):
        h = Harness(legs)
        h.locked |= set(locked)
        try:
            return a.run_watchdog(None, live, now=now, auto_lock=False)
        finally:
            h.restore()

    def rec(self):
        return json.loads(self.tmp.read_text())["days"]

    def test_open_then_started_unlocked_is_recorded_as_a_miss_and_a_locked_one_is_cleared(self):
        legs = [q("NFL", "OPEN ML", 30), q("NFL", "LOCKED ML", 30, hA="H2", awA="A2")]
        self.run_wd(legs, NOW)
        day = next(iter(self.rec()))
        self.assertEqual({r["leg"]: r["status"] for r in self.rec()[day].values()}, {"OPEN ML": "owed", "LOCKED ML": "owed"})
        self.run_wd(legs, NOW + 45 * MIN, locked={"LOCKED ML"})                # kickoff passed: OPEN ML never got locked, LOCKED ML did
        self.assertEqual({r["leg"]: r["status"] for r in self.rec()[day].values()}, {"OPEN ML": "missed", "LOCKED ML": "cleared"})

    def test_a_dry_run_or_a_test_process_writes_nothing(self):
        self.run_wd([q("NFL", "OPEN ML", 30)], NOW, live=False)
        self.assertFalse(self.tmp.exists())
        a.LEGS_PERSIST = False
        self.run_wd([q("NFL", "OPEN ML", 30)], NOW)
        self.assertFalse(self.tmp.exists())

    def test_nothing_unlocked_and_no_file_writes_no_file(self):
        self.run_wd([], NOW)
        self.assertFalse(self.tmp.exists())

    def test_a_failure_to_record_never_breaks_the_watchdog(self):
        a.LEGS_STATE_PATH = Path("/proc/definitely/not/writable/watchdog_legs.json")
        left = self.run_wd([q("NFL", "OPEN ML", 30)], NOW)
        self.assertEqual([u["leg"] for u in left], ["OPEN ML"])               # the alert path still ran

    def test_main_turns_the_record_on_only_for_a_live_watchdog_run(self):
        src = (HERE / "auto_lock_settle.py").read_text()
        self.assertIn("LEGS_PERSIST = bool(args.live and args.watchdog)", src)

    def test_the_workflow_commits_the_record(self):
        wf = (HERE.parent / ".github" / "workflows" / "lock-watchdog.yml").read_text()
        self.assertIn("paths: data/watchdog_legs.json", wf)


class HealthCheck(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.now = datetime(2026, 11, 4, 13, 33, tzinfo=MT)                  # the full pass; "yesterday" = 2026-11-03

    def write(self, recs):
        (self.root / "data").mkdir(exist_ok=True)
        (self.root / W.STATE_REL).write_text(json.dumps({"days": {"2026-11-03": {f"k{i}": dict(r, game="A @ B", leg=f"L{i}", sport="NFL") for i, r in enumerate(recs)}}}))

    def check(self):
        return H.check_unlocked_legs(self.root, self.now)

    def test_a_missed_leg_is_an_alert_naming_it(self):
        self.write([{"status": "missed"}, {"status": "held"}, {"status": "unqualified"}, {"status": "late"}])
        (lvl, msg), = self.check()
        self.assertEqual(lvl, "alert")
        self.assertIn("1 qualifying leg(s) started without a lock", msg)
        self.assertIn("A @ B L0", msg)
        for part in ("1 held by the lineup gate", "1 not qualified", "1 first seen only after kickoff"):
            self.assertIn(part, msg)

    def test_only_held_unqualified_or_late_legs_is_a_note_with_the_counts(self):
        self.write([{"status": "held"}, {"status": "held"}, {"status": "unqualified"}, {"status": "cleared"}])
        (lvl, msg), = self.check()
        self.assertEqual(lvl, "note")
        self.assertIn("3 qualifying leg(s) were seen unlocked, none missed", msg)
        self.assertIn("2 held", msg)

    def test_a_clean_day_says_none(self):
        self.write([{"status": "cleared"}])
        (lvl, msg), = self.check()
        self.assertEqual(lvl, "note")
        self.assertIn("none -- every qualifying leg the watchdog saw was locked", msg)

    def test_no_state_file_yet_is_a_note_not_an_alert(self):
        (lvl, msg), = self.check()
        self.assertEqual(lvl, "note")
        self.assertIn("no watchdog record yet", msg)

    def test_a_different_days_misses_are_not_counted(self):
        (self.root / "data").mkdir(exist_ok=True)
        (self.root / W.STATE_REL).write_text(json.dumps({"days": {"2026-11-02": {"k": {"status": "missed", "game": "A @ B", "leg": "L", "sport": "NFL"}}}}))
        (lvl, msg), = self.check()
        self.assertEqual(lvl, "note")

    def test_the_full_pass_runs_it(self):
        src = (HERE / "daily_health_check.py").read_text()
        self.assertIn("check_unlocked_legs()", src)


if __name__ == "__main__":
    unittest.main()
