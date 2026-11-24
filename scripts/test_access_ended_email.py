#!/usr/bin/env python3
"""The 'access ended' email (owner 2026-11-24): one email when a 30-day window has run out -- paid or a free / promo month -- saying it has ended and how to start again.

  * who is told: windows that ended in the last few days, once (ended_sent), never an old lapse, never an active window, never the owner;
  * a renewal / referral extension / re-add starts a fresh window that can be told about again;
  * the copy: a free month says so and gives the price (from the live ladder); a paid one points to the usual renewal; several products share ONE email;
  * the runner: sends, marks only what was delivered, exits non-zero on a failure, dry-run sends and marks nothing.

    python3 scripts/test_access_ended_email.py
"""
from __future__ import annotations

import json, sys, tempfile, unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import _subscribers as S  # noqa: E402
import send_expiry_reminders as R  # noqa: E402


def ago(days):
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


class Selection(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.f = self.tmp / "subscribers.json"
        self.ev = self.tmp / "subscriber_events.json"
        self.patch = [mock.patch.object(S, "SUBSCRIBERS_FILE", self.f), mock.patch.object(S, "EVENTS_FILE", self.ev), mock.patch.object(S, "OWNER_EMAIL", "owner@example.com")]
        for p in self.patch:
            p.start()

    def tearDown(self):
        for p in self.patch:
            p.stop()

    def seed(self, data):
        self.f.write_text(json.dumps(data))

    def test_who_is_told(self):
        self.seed({"nfl": [
            {"email": "just@x.com", "added": ago(30.2)},                              # ended a few hours ago -> told
            {"email": "free@x.com", "added": ago(31), "promo": True},                  # free month ended yesterday -> told, flagged promo
            {"email": "active@x.com", "added": ago(29)},                               # still active -> no
            {"email": "old@x.com", "added": ago(40)},                                  # lapsed long ago -> never emailed now
            {"email": "done@x.com", "added": ago(31), "ended_sent": True},             # already told
            {"email": "owner@example.com", "added": ago(31)},                          # the owner is never emailed
            {"email": "bad"}, "junk", {"added": ago(31)},                              # malformed rows are skipped, never crash
        ]})
        got = {r["email"]: r for r in S.subscribers_needing_ended_notice()}
        self.assertEqual(set(got), {"just@x.com", "free@x.com"})
        self.assertFalse(got["just@x.com"]["promo"]); self.assertTrue(got["free@x.com"]["promo"])
        self.assertEqual(got["just@x.com"]["product"], "nfl")

    def test_the_window_edge(self):
        self.seed({"cfb": [{"email": "a@x.com", "added": ago(32.9)}, {"email": "b@x.com", "added": ago(33.1)}]})
        self.assertEqual([r["email"] for r in S.subscribers_needing_ended_notice()], ["a@x.com"])      # ended 2.9 days ago yes, 3.1 days ago no
        self.assertEqual([r["email"] for r in S.subscribers_needing_ended_notice(window_days=5)], ["a@x.com", "b@x.com"])

    def test_retired_products_are_ignored(self):
        self.seed({"mlb": [{"email": "a@x.com", "added": ago(31)}], "nba": [{"email": "b@x.com", "added": ago(31)}]})
        self.assertEqual([(r["product"], r["email"]) for r in S.subscribers_needing_ended_notice()], [("nba", "b@x.com")])

    def test_mark_ended_sent_stops_the_notice_and_a_renewal_resets_it(self):
        self.seed({"nfl": [{"email": "a@x.com", "added": ago(31)}, {"email": "b@x.com", "added": ago(31)}]})
        S.mark_ended_sent("nfl", "A@x.com")
        self.assertEqual([r["email"] for r in S.subscribers_needing_ended_notice()], ["b@x.com"])       # only the marked one is done
        self.assertTrue(json.loads(self.f.read_text())["nfl"][0]["ended_sent"])
        S.add_subscriber("nfl", "a@x.com")                                                                 # they paid: fresh window
        row = json.loads(self.f.read_text())["nfl"][0]
        self.assertNotIn("ended_sent", row); self.assertFalse(row["reminder_sent"])
        self.assertEqual([r["email"] for r in S.subscribers_needing_ended_notice()], ["b@x.com"])       # active again, so not told

    def test_a_referral_extension_clears_the_flag_too(self):
        self.seed({"nfl": [{"email": "a@x.com", "added": ago(10), "ended_sent": True}]})
        with mock.patch.object(S, "REFERRAL_BONUS_DAYS", 30):
            S.grant_referral_reward("a@x.com", "new@x.com", "nfl")
        self.assertNotIn("ended_sent", json.loads(self.f.read_text())["nfl"][0])


class Copy(unittest.TestCase):
    def test_a_free_month_says_so_and_gives_the_price(self):
        subj, body = R._build_ended_email([{"product": "nfl", "ended": "2026-11-25T02:22:00+00:00", "promo": True}])
        self.assertIn("free month of", subj); self.assertIn("has ended", subj)
        self.assertIn("Your free month of", body); self.assertIn("November 24, 2026", body)     # 02:22 UTC on the 25th is still the 24th in Mountain time
        self.assertIn(R._price_line(), body); self.assertIn("$20 for 30 days of one sport, plus $15 for each extra sport", body)
        self.assertIn("Thanks for trying Clairvoyance", body); self.assertIn("want to stop these emails", body)
        self.assertNotIn("usual amount", body)

    def test_a_paid_window_points_to_the_usual_renewal_without_the_price_pitch(self):
        subj, body = R._build_ended_email([{"product": "nfl", "ended": "2026-11-25T02:22:00+00:00", "promo": False}])
        self.assertIn("access has ended", subj); self.assertIn("Your access to", body)
        self.assertIn("usual amount", body); self.assertIn("30 days from the day we record your payment", body)
        self.assertNotIn("plus $15", body); self.assertNotIn("free month", body)

    def test_several_products_share_one_email(self):
        rows = [{"product": "nfl", "ended": "2026-11-25T02:22:00+00:00", "promo": True}, {"product": "cfb", "ended": "2026-11-24T03:00:00+00:00", "promo": False}]
        subj, body = R._build_ended_email(rows)
        self.assertIn("2 subscriptions have ended", subj); self.assertIn("2 of your subscriptions have ended", body)
        self.assertIn("free month", body); self.assertIn("usual amount", body); self.assertIn(R._price_line(), body)    # mixed: both paragraphs
        self.assertLess(body.index("November 23, 2026"), body.index("November 24, 2026"))                              # earliest end first

    def test_all_free_months_plural_subject(self):
        rows = [{"product": "nfl", "ended": "2026-11-25T02:22:00+00:00", "promo": True}, {"product": "cfb", "ended": "2026-11-25T03:00:00+00:00", "promo": True}]
        self.assertIn("free months have ended", R._build_ended_email(rows)[0])

    def test_the_price_follows_the_ladder(self):
        with mock.patch.object(R, "PRICING_BY_COUNT", {1: 25, 2: 45, 3: 65, 4: 85, 5: 105}):
            self.assertIn("$25 for 30 days of one sport, plus $20 for each extra sport ($45 for two, $65 for three)", R._price_line())


class Runner(unittest.TestCase):
    ROWS = [{"product": "nfl", "email": "a@x.com", "ended": "2026-11-25T02:22:00+00:00", "promo": False},
            {"product": "cfb", "email": "b@x.com", "ended": "2026-11-25T02:22:00+00:00", "promo": True},
            {"product": "nba", "email": "b@x.com", "ended": "2026-11-25T02:22:00+00:00", "promo": True}]

    def run_main(self, send_ok, argv=()):
        sent, marked, rem_marked = [], [], []
        patches = [mock.patch.object(R, "subscribers_needing_reminder", lambda days_before=3: []),
                   mock.patch.object(R, "subscribers_needing_ended_notice", lambda: list(self.ROWS)),
                   mock.patch.object(R, "mark_ended_sent", lambda p, e: marked.append((p, e))),
                   mock.patch.object(R, "mark_reminder_sent", lambda p, e: rem_marked.append((p, e))),
                   mock.patch.object(R, "send_email", lambda subj, to, body: (sent.append((to, subj)) or (to in send_ok, "boom"))),
                   mock.patch.object(sys, "argv", ["send_expiry_reminders.py", *argv])]
        for p in patches:
            p.start()
        try:
            R.main(); code = 0
        except SystemExit as e:
            code = e.code
        finally:
            for p in patches:
                p.stop()
        return code, sent, marked, rem_marked

    def test_one_email_per_person_and_every_product_is_marked(self):
        code, sent, marked, rem = self.run_main({"a@x.com", "b@x.com"})
        self.assertEqual(code, 0)
        self.assertEqual(sorted(t for t, _ in sent), ["a@x.com", "b@x.com"])                  # b has two products, one email
        self.assertEqual(sorted(marked), [("cfb", "b@x.com"), ("nba", "b@x.com"), ("nfl", "a@x.com")]); self.assertEqual(rem, [])

    def test_a_failed_send_is_not_marked_and_exits_nonzero(self):
        code, sent, marked, _ = self.run_main({"a@x.com"})
        self.assertEqual(code, 1); self.assertEqual(marked, [("nfl", "a@x.com")])               # b retries tomorrow (inside the window)

    def test_dry_run_sends_and_marks_nothing(self):
        code, sent, marked, _ = self.run_main({"a@x.com", "b@x.com"}, ["--dry-run"])
        self.assertEqual((code, sent, marked), (0, [], []))

    def test_an_unreadable_list_ends_the_run_red_before_any_send(self):
        with mock.patch.object(S, "DATA_ERRORS", ["list unavailable"]):
            code, sent, marked, _ = self.run_main({"a@x.com"})
        self.assertEqual((code, sent, marked), (1, [], []))


class Workflow(unittest.TestCase):
    def test_the_daily_workflow_runs_the_script_and_commits_both_flags(self):
        wf = (HERE.parent / ".github" / "workflows" / "send-expiry-reminders.yml").read_text()
        self.assertIn("scripts/send_expiry_reminders.py", wf)
        self.assertIn("git add subscribers.json", wf)                                         # reminder_sent AND ended_sent live in this file
        self.assertIn("ended_sent", wf)


if __name__ == "__main__":
    unittest.main(verbosity=1)
