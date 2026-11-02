#!/usr/bin/env python3
"""European hockey refresh alerting (2026-11-02): a single failed run no longer alerts, two in a row (or no success inside the limit) still do, and the display-only NHL over/under step cannot turn the run red.
Basis: 100 runs Oct 16 - Nov 2 had 4 failures, all single and all healed by the next scheduled run 2.8-4 h later.  No network: the GitHub API is faked.

    python3 scripts/test_euro_refresh_alerting.py
"""
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
import daily_health_check as H  # noqa: E402

NOW = datetime(2026, 11, 2, 18, 0, tzinfo=timezone.utc)
WF = "hockey-euro-refresh.yml"


def run(conclusion, hours_ago):
    t = (NOW - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"conclusion": conclusion, "created_at": t, "updated_at": t, "html_url": "https://example/run"}


def check(runs):
    entry = next(e for e in H.REFRESH_MONITORED if e[0] == WF)
    return H.check_refresh_workflow(*entry, api_get=lambda path: {"workflow_runs": runs}, now=NOW)


class Threshold(unittest.TestCase):
    def test_the_monitored_entry_alerts_on_two_in_a_row(self):
        entry = next(e for e in H.REFRESH_MONITORED if e[0] == WF)
        self.assertEqual(entry[2:], (20, 2))

    def test_one_failed_run_after_successes_is_quiet(self):
        self.assertIsNone(check([run("failure", 1), run("success", 5), run("success", 9)]))      # the real pattern: 4 of 4 failures were this

    def test_cancelled_runs_do_not_count_either_way(self):
        self.assertIsNone(check([run("cancelled", 1), run("failure", 2), run("success", 6)]))

    def test_two_failures_in_a_row_alert(self):
        res = check([run("failure", 1), run("failure", 5), run("success", 9)])
        self.assertEqual(res[0], "alert")
        self.assertIn("last 2 run(s) FAILED", res[1])

    def test_a_missing_success_inside_the_limit_still_alerts(self):
        res = check([run("success", 25)])                       # healthy streak but the last success is older than 20 h
        self.assertEqual(res[0], "alert")
        self.assertIn("last SUCCESS", res[1])

    def test_no_success_among_recent_runs_alerts(self):
        self.assertEqual(check([run("failure", 1), run("failure", 5)])[0], "alert")


class Workflow(unittest.TestCase):
    def final_step(self):
        wf = (ROOT / ".github" / "workflows" / WF).read_text()
        return wf[wf.index("Fail the run if any league failed"):]

    def test_the_four_leagues_still_fail_the_run(self):
        step = self.final_step()
        for lg, sid in (("Liiga", "liiga"), ("SHL", "shl"), ("NLA", "nla"), ("Extraliga", "extraliga")):
            self.assertIn(f'steps.{sid}.outcome }}}}" = "failure" ] && bad="$bad {lg}"', step)
        self.assertIn('exit 1', step)

    def test_the_display_only_nhl_over_under_step_only_warns(self):
        step = self.final_step()
        self.assertNotIn('bad="$bad NHL-over-under"', step)
        self.assertIn("::warning::NHL over/under records did not refresh", step)


if __name__ == "__main__":
    unittest.main()
