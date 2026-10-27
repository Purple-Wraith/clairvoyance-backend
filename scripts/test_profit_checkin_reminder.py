#!/usr/bin/env python3
"""Owner-only PROFIT check-in reminders (start + end of every week, Mountain time): which reminder is due when, sent once per (week, kind), owner-only, content, Worker + workflow wiring.

Hermetic: no network, no real mail, no git.

    python3 scripts/test_profit_checkin_reminder.py
"""
from __future__ import annotations

import re
import sys
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import profit_checkin_reminder as r  # noqa: E402

MT = ZoneInfo("America/Denver")


def at(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=MT)


class Windows(unittest.TestCase):
    def test_end_of_week_is_sunday_evening_about_the_finishing_week(self):
        k, mon = r.due_kind(at(2026, 10, 11, 19))           # Sunday Oct 11, 7 PM
        self.assertEqual((k, mon), ("end", date(2026, 10, 5)))
        self.assertEqual(r.due_kind(at(2026, 10, 11, 15, 59))[0], None)     # before the window
        k, mon = r.due_kind(at(2026, 10, 12, 2))            # Monday 2 AM: a late END reminder is still about the week that just closed
        self.assertEqual((k, mon), ("end", date(2026, 10, 5)))

    def test_start_of_week_is_monday_morning_about_the_new_week(self):
        self.assertEqual(r.due_kind(at(2026, 10, 12, 8)), ("start", date(2026, 10, 12)))
        self.assertEqual(r.due_kind(at(2026, 10, 12, 4, 59))[0], "end")    # between the two windows the late END one still wins until 05:00
        self.assertEqual(r.due_kind(at(2026, 10, 13, 5)), ("start", date(2026, 10, 12)))      # a very late START (Tuesday small hours)

    def test_nothing_is_due_midweek(self):
        for d, h in ((13, 12), (14, 9), (15, 20), (16, 7), (17, 23), (13, 6)):
            self.assertIsNone(r.due_kind(at(2026, 10, d, h))[0], (d, h))


class Sending(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name) / "state.json"
        self.sent = []

    def tearDown(self):
        self.tmp.cleanup()

    def fake(self, subject, to, html):
        self.sent.append((subject, to, html))
        return True, "ok"

    def go(self, now, **kw):
        return r.run("auto", now, send=self.fake, state_path=self.state, to="owner@example.com", **kw)

    def test_sends_once_per_week_and_kind(self):
        self.assertEqual(self.go(at(2026, 10, 11, 19))[0], "sent")
        self.assertEqual(self.go(at(2026, 10, 11, 21))[0], "skipped")        # the Worker AND the cron fallback may both fire: only one email
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.go(at(2026, 10, 12, 8))[0], "sent")             # the START reminder is a separate send
        self.assertEqual(len(self.sent), 2)
        self.assertEqual(self.go(at(2026, 10, 18, 19))[0], "sent")            # and next week's END again
        self.assertEqual(len(self.sent), 3)

    def test_owner_only_and_content(self):
        self.go(at(2026, 10, 11, 19))
        subject, to, html = self.sent[0]
        self.assertEqual(to, "owner@example.com")
        self.assertIn("end-of-week check-in", subject)
        self.assertIn("Oct 5\u201311", subject)
        for want in ("ENDING", "PERIOD CHECK-IN", "DraftKings", "PrizePicks", "Hard Rock", "app.html"):
            self.assertIn(want, html)
        self.go(at(2026, 10, 12, 8))
        self.assertIn("start-of-week check-in", self.sent[1][0])
        self.assertIn("Oct 12\u201318", self.sent[1][0])
        self.assertIn("STARTING", self.sent[1][2])
        for _s, _t, h in self.sent:
            for bad in ("undefined", "None", "NaN", "{"):
                self.assertNotIn(bad, h)

    def test_month_boundary_range_label(self):
        self.assertEqual(r._rng(date(2026, 10, 26)), "Oct 26\u2013Nov 1")

    def test_failed_send_is_not_recorded_and_retries(self):
        out = r.run("auto", at(2026, 10, 11, 19), send=lambda s, t, h: (False, "smtp down"), state_path=self.state, to="o@example.com")
        self.assertEqual(out[0], "failed")
        self.assertEqual(self.go(at(2026, 10, 11, 20))[0], "sent")

    def test_dry_run_sends_and_records_nothing(self):
        self.assertEqual(self.go(at(2026, 10, 11, 19), dry=True)[0], "dry-run")
        self.assertEqual(self.sent, [])
        self.assertFalse(self.state.exists())

    def test_no_owner_address_is_a_failure_not_a_silent_skip(self):
        out = r.run("auto", at(2026, 10, 11, 19), send=self.fake, state_path=self.state, to="")
        self.assertEqual(out[0], "failed")

    def test_explicit_kind_outside_its_window_is_skipped_unless_forced(self):
        self.assertEqual(r.run("end", at(2026, 10, 14, 12), send=self.fake, state_path=self.state, to="o@x.com")[0], "skipped")
        self.assertEqual(r.run("end", at(2026, 10, 14, 12), send=self.fake, state_path=self.state, to="o@x.com", force=True)[0], "sent")


class Wiring(unittest.TestCase):
    """2026-10-27: the reminders were merged into the Monday Weekly Health Digest (owner: too many emails)."""

    def test_worker_no_longer_dispatches_it(self):
        js = (ROOT / "scheduler" / "worker.js").read_text()
        self.assertNotRegex(js, r'\{ wf: "profit-checkin-reminder\.yml"')

    def test_workflow_has_no_schedule_and_never_emails_subscribers(self):
        wf = (ROOT / ".github" / "workflows" / "profit-checkin-reminder.yml").read_text()
        self.assertNotIn("cron:", wf)
        self.assertIn("workflow_dispatch", wf)
        self.assertIn("profit_checkin_reminder.py", wf)
        self.assertNotIn("subscribers", wf.lower().replace("never emails subscribers", ""))
        self.assertIn("./.github/actions/publish", wf)
        self.assertRegex(wf, r"dispatch: never")

    def test_script_never_touches_the_subscriber_list(self):
        src = (ROOT / "scripts" / "profit_checkin_reminder.py").read_text()
        self.assertNotIn("recipients_for", src)
        self.assertNotIn("subscribers.json", src)

    def test_stale_worker_dispatch_of_kind_auto_sends_nothing(self):
        self.assertEqual(r.main(["--kind", "auto"]), 0)         # not forced: skipped before any send / state write

    def test_weekly_section_covers_last_weeks_ending_and_this_weeks_starting(self):
        html = r.weekly_section_html(at(2026, 10, 26, 9))        # Monday Oct 26
        self.assertIn("Profit tracker check-in", html)
        self.assertIn("Oct 19", html)                           # last week
        self.assertIn("Oct 26", html)                           # this week
        self.assertIn("ENDING AMOUNT", html)
        self.assertIn("STARTING AMOUNT", html)

    def test_weekly_digest_includes_the_section_and_the_subject_says_so(self):
        import weekly_health_digest as W
        body = W.build_email_html(None, [], [], [], 0, [])
        self.assertIn("5. Profit tracker check-in", body)
        self.assertIn("Profit Check-in", (ROOT / "scripts" / "weekly_health_digest.py").read_text())


if __name__ == "__main__":
    unittest.main(verbosity=2)
