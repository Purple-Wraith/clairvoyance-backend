#!/usr/bin/env python3
"""Health check: lock markers vs the layers of the same lock (scripts/daily_health_check.py), 2026-11-02.

The marker is written by the LAST layer of each lock and GitHub lands that workflow hours late (observed 7:03-11:45 AM MT), so "marker not today yet" used to alert on most weekdays even though the evening /
pre-dawn passes had already locked everything.  It is only a problem now when no layer has run successfully today.  No network: the GitHub API is faked.

    python3 scripts/test_lock_marker_layers.py
"""
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
import daily_health_check as H  # noqa: E402

MT = ZoneInfo("America/Denver")
LABEL = "European Early Lock (Soccer + SHL/Liiga)"


def runs(*created_utc, conclusion="success"):
    return {"workflow_runs": [{"created_at": c, "conclusion": conclusion} for c in created_utc]}


class Layers(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        (self.d / "marker.txt").write_text("2026-11-01")                          # yesterday's marker: the last layer has not landed yet today
        self.orig = H.LOCK_MARKERS
        H.LOCK_MARKERS = [(self.d / "marker.txt", LABEL, 11)]
        self.at = datetime(2026, 11, 2, 11, 30, tzinfo=MT)

    def tearDown(self):
        H.LOCK_MARKERS = self.orig

    def api(self, by_workflow):
        def get(path):
            for wf, payload in by_workflow.items():
                if f"/workflows/{wf}/" in path:
                    return payload
            return {"workflow_runs": []}
        return get

    def test_this_mornings_false_alarm_is_gone(self):
        """2026-11-02: no early-lock run had landed yet, but the pre-dawn soccer and hockey passes had all succeeded."""
        api = self.api({"soccer-lock-evening.yml": runs("2026-11-02T08:13:37Z", "2026-11-02T10:54:39Z"), "hockey-lock-evening.yml": runs("2026-11-02T07:50:14Z")})
        self.assertEqual(H.check_lock_markers("full", self.at, api), [])

    def test_the_problem_stays_when_no_layer_has_run_today(self):
        api = self.api({"soccer-lock-evening.yml": runs("2026-11-01T01:31:48Z"),                       # last night's evening pass: Mountain date Oct 31
                        "european-lock-early.yml": runs("2026-11-01T18:01:36Z")})                       # yesterday's early pass
        problems = H.check_lock_markers("full", self.at, api)
        self.assertEqual(len(problems), 1)
        self.assertIn("marker shows 2026-11-01", problems[0])

    def test_failed_or_cancelled_layer_runs_do_not_count(self):
        api = self.api({"soccer-lock-evening.yml": runs("2026-11-02T08:13:37Z", conclusion="failure"), "hockey-lock-evening.yml": runs("2026-11-02T07:50:14Z", conclusion="cancelled")})
        self.assertEqual(len(H.check_lock_markers("full", self.at, api)), 1)

    def test_an_unreadable_api_keeps_the_old_verdict(self):
        def boom(path):
            raise RuntimeError("rate limited")
        self.assertEqual(len(H.check_lock_markers("full", self.at, boom)), 1)
        self.assertEqual(len(H.check_lock_markers("full", self.at, None)), 1)           # no token / no api: unchanged behaviour

    def test_a_marker_for_today_needs_no_api_call(self):
        (self.d / "marker.txt").write_text("2026-11-02")
        def boom(path):
            raise AssertionError("must not be called")
        self.assertEqual(H.check_lock_markers("full", self.at, boom), [])

    def test_cutoffs_sit_after_the_observed_landing_envelope(self):
        real = {label: hour for _p, label, hour in self.orig}
        self.assertGreaterEqual(real[LABEL], 11)                       # observed 7:03-11:45 AM MT; the old 8 AM was missed on most weekdays
        self.assertGreaterEqual(real["CFB Early Lock"], 12)
        self.assertEqual(sorted(H.LOCK_LAYERS), sorted(real))          # every marker has its layers listed

    def test_before_the_cutoff_nothing_is_expected_yet(self):
        self.assertEqual(H.check_lock_markers("full", datetime(2026, 11, 2, 10, 10, tzinfo=MT), self.api({})), [])


if __name__ == "__main__":
    unittest.main()
