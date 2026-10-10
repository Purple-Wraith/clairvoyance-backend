#!/usr/bin/env python3
"""Regression tests for the 2026-10-10 email-system audit (coverage / timing / correctness / deliverability).  Nothing here sends mail, touches git, Supabase or the network:
every send, every pending-record write and every browser call is stubbed.

    python3 scripts/test_email_audit_20261010.py
"""
from __future__ import annotations

import json
import smtplib
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import auto_lock_settle as a  # noqa: E402
import _gmail_email as gm  # noqa: E402
import _subscribers as sub  # noqa: E402
import daily_health_check as hc  # noqa: E402

MT = ZoneInfo("America/Denver")


def leg(sport, label, side="mlFav", tier=3, prob=0.7, hA="HOM", awA="AWY"):
    return {"kind": "GAME", "sport": sport, "hA": hA, "awA": awA, "side": side, "label": label, "prob": prob, "ml": "-110", "dec": 1.91, "tierN": tier,
            "evVal": 0.05, "lane": False, "mcSummary": None, "best": None, "startMs": 1_800_000_000_000}


class Patch:
    """Tiny attribute patcher (restores everything on exit) -- the repo's tests do not use unittest.mock."""
    def __init__(self, *pairs):
        self.pairs, self.saved = pairs, []

    def __enter__(self):
        for obj, name, val in self.pairs:
            self.saved.append((obj, name, getattr(obj, name)))
            setattr(obj, name, val)
        return self

    def __exit__(self, *exc):
        for obj, name, old in reversed(self.saved):
            setattr(obj, name, old)
        return False


# ───────────────────────── 1. watchdog auto-lock must not re-email everything ─────────────────────────
class SegmentedOnlyNew(unittest.TestCase):
    """Real 2026-10-08 20:50Z / 22:22Z and 10-10 20:42Z watchdog runs called run_lock_segmented(send_email=True), which emailed every product on every call: a second
    full copy of the day's NHL picks and (on a Saturday) an NFL 'no picks' email.  only_new=True mirrors the rolling runners: email a product only when it locked something new."""

    def run_seg(self, legs, new_by_product, only_new, send_email=True, pending=()):
        sent = []
        calls = {"n": 0}

        def lock(page, qualifying, *k, **kw):
            sports = {q["sport"] for q in qualifying}
            new = sum(v for p, v in new_by_product.items() if a.PRODUCT_SPORTS[p] & sports)
            return a.LockResult(new, len(qualifying) - min(new, len(qualifying)), 0)

        with Patch((a, "gather_legs", lambda page: {"gameLegs": [], "propLegs": []}),
                   (a, "build_qualifying", lambda result, **k: list(legs)),
                   (a, "analyze_games", lambda result, only=None, now=None: {"games": 3, "started": 0, "upcoming": 3, "nextStartMs": None, "noPrice": []}),
                   (a, "recipients_for", lambda product: [f"{product}@x.com"]),
                   (a, "_lock_qualifying_legs", lock),
                   (a, "flush_to_supabase", lambda page: None),
                   (a, "pending_labels", lambda now=None: set(pending)),
                   (a, "send_locks_email", lambda qualifying, live, locked_count=None, label="", to=None, date_str=None, key=None: sent.append((label, key, len(qualifying))) or True),
                   (a, "PASS_REPORTS", [])):
            a.run_lock_segmented(None, True, send_email=send_email, only_new=only_new)
        return sent

    def test_watchdog_mode_emails_only_products_with_new_locks(self):
        legs = [leg("NHL", "TOR ML"), leg("NHL", "EDM ML", hA="EDM"), leg("NFL", "KC ML", hA="KC")]
        sent = self.run_seg(legs, {"hockey": 1}, only_new=True)
        self.assertEqual([s[0] for s in sent], ["HOCKEY"])                    # the NHL email, listing the whole NHL slate (rolling-runner convention)
        self.assertEqual(sent[0][2], 2)

    def test_watchdog_mode_sends_no_zero_pick_email(self):
        self.assertEqual(self.run_seg([], {}, only_new=True), [])             # nothing qualifying anywhere: NO NFL 'no picks' email on a Saturday

    def test_watchdog_mode_nothing_new_nothing_sent(self):
        self.assertEqual(self.run_seg([leg("NHL", "TOR ML")], {}, only_new=True), [])

    def test_normal_final_pass_is_unchanged(self):
        sent = self.run_seg([leg("NHL", "TOR ML")], {}, only_new=False)
        labels = [s[0] for s in sent]
        self.assertIn("NFL", labels)                                           # the once-daily email still goes to NFL even with nothing qualifying (owner-visible behaviour kept)
        self.assertIn("HOCKEY", labels)
        self.assertNotIn("SOCCER", labels)                                      # soccer / CFB keep their own dedicated passes
        self.assertNotIn("CFB", labels)

    def test_a_pending_failed_main_email_is_retried_by_a_later_non_final_pass(self):
        sent = self.run_seg([leg("NHL", "TOR ML")], {}, only_new=False, send_email=False, pending={"MAIN HOCKEY"})
        self.assertEqual([(s[0], s[1]) for s in sent], [("HOCKEY", "MAIN HOCKEY")])
        self.assertEqual(self.run_seg([leg("NHL", "TOR ML")], {}, only_new=False, send_email=False), [])   # nothing pending, not the final slot: quiet as before

    def test_auto_lock_uses_only_new(self):
        seen = {}
        with Patch((a, "run_lock_segmented", lambda page, live, send_email=True, only_new=False: seen.update(only_new=only_new, send_email=send_email))):
            self.assertEqual(a._auto_lock(None, [{"sport": "NHL"}]), [])
        self.assertTrue(seen["only_new"])


# ───────────────────────── 2. a failed send is retried, not lost ─────────────────────────
class PendingRetry(unittest.TestCase):
    def setUp(self):
        self.writes = []
        self.state = {}
        self.sendres = (True, "sent")
        self.patch = Patch((a, "PENDING_PERSIST", True),
                           (a, "read_marker_from_origin", lambda rel: json.dumps(self.state)),
                           (a, "_pending_write", lambda mutate, message: (mutate(self.state), self.writes.append(message))),
                           (a, "_send_gmail", lambda subject, to, body: self.sendres),
                           (a, "OWNER_EMAIL", "owner@x.com"),
                           (a, "EMAIL_FAILURES", []))
        self.patch.__enter__()
        a._PENDING_SEEN.clear()

    def tearDown(self):
        self.patch.__exit__()
        a._PENDING_SEEN.clear()

    def send(self, live=True, key=None):
        return a.send_locks_email([leg("NHL", "TOR ML")], live, 1, label="HOCKEY", to=["a@x.com"], key=key)

    def test_a_send_that_reached_nobody_is_recorded_pending(self):
        self.sendres = (False, "SMTPServerDisconnected")
        self.assertFalse(self.send())
        self.assertIn("HOCKEY", self.state)
        self.assertEqual(self.state["HOCKEY"]["date"], datetime.now(MT).strftime("%Y-%m-%d"))
        self.assertEqual(len(a.EMAIL_FAILURES), 1)                              # the run still ends red

    def test_a_partial_send_is_not_retried(self):
        self.sendres = (False, "partial send -- 1/2 delivered; failures: 1 recipient(s) refused by the mail server: bad@x.com")
        self.send()
        self.assertEqual(self.state, {})                                         # a retry would double-deliver to everyone who did get it
        self.assertEqual(len(a.EMAIL_FAILURES), 1)

    def test_a_dry_run_never_records_anything(self):
        self.sendres = (False, "boom")
        self.send(live=False)
        self.assertEqual(self.state, {})

    def test_the_retry_clears_the_record(self):
        self.state["HOCKEY"] = {"date": datetime.now(MT).strftime("%Y-%m-%d")}
        self.assertEqual(a.pending_labels(), {"HOCKEY"})
        self.assertTrue(self.send())
        self.assertEqual(self.state, {})

    def test_a_distinct_key_keeps_the_main_and_early_hockey_emails_apart(self):
        self.sendres = (False, "boom")
        self.send(key="MAIN HOCKEY")
        self.assertEqual(list(self.state), ["MAIN HOCKEY"])

    def test_stale_records_are_ignored(self):
        old = (datetime.now(MT) - timedelta(days=4)).strftime("%Y-%m-%d")
        self.state.update({"HOCKEY": {"date": old}, "CFB": {"date": datetime.now(MT).strftime("%Y-%m-%d")}})
        self.assertEqual(a.pending_labels(), {"CFB"})

    def test_unreadable_record_fails_open(self):
        with Patch((a, "read_marker_from_origin", lambda rel: "{not json")):
            self.assertEqual(a.pending_labels(), set())

    def test_bookkeeping_never_touches_the_repo_outside_a_live_run(self):
        calls = []
        with Patch((a, "PENDING_PERSIST", False), (a, "read_marker_from_origin", lambda rel: calls.append("fetch") or "{}"),
                   (a, "_commit_and_push", lambda *x, **k: calls.append("push"))):
            self.assertEqual(a.pending_labels(), set())
            a.mark_email_pending("X")
            a.clear_email_pending("X")
        self.assertEqual(calls, [])
        src = (ROOT / "scripts" / "auto_lock_settle.py").read_text()
        self.assertIn("\nPENDING_PERSIST = False\n", src)                        # the default for every importer (tests, tools) ...
        self.assertIn("PENDING_PERSIST = bool(args.live)", src)                  # ... only main() on a --live run turns it on

    def test_rolling_pass_resends_a_pending_email_even_when_nothing_is_new(self):
        sent = []

        def run(pending):
            sent.clear()
            res = {"gameLegs": []}
            with Patch((a, "horizon_dates", lambda now=None, days=2: ["2026-10-10", "2026-10-11"]),
                       (a, "build_qualifying", lambda result, **k: [leg("CFB", "ARMY +3.5")]),
                       (a, "analyze_games", lambda result, only=None, now=None: {"games": 1, "started": 0, "upcoming": 1, "nextStartMs": None, "noPrice": []}),
                       (a, "_lock_qualifying_legs", lambda page, q, **k: a.LockResult(0, 1, 0)),
                       (a, "flush_to_supabase", lambda page: None),
                       (a, "pending_labels", lambda now=None: set(pending)),
                       (a, "send_locks_email", lambda qualifying, live, locked_count=None, label="", to=None, date_str=None, key=None: sent.append(label) or True),
                       (a, "PASS_REPORTS", [])):
                a._run_rolling_pass(None, True, True, ["a@x.com"], kind="cfb-evening", label="CFB — UPCOMING SLATE", title="CFB", gather=lambda page, d: res,
                                    only_sports=frozenset({"CFB"}), dry_label="cfb")
            return list(sent)

        self.assertEqual(run(set()), [])                                          # nothing new, nothing pending: quiet (the duplicate guard)
        self.assertEqual(run({"CFB — UPCOMING SLATE"}), ["CFB — UPCOMING SLATE"])  # the earlier send reached nobody: send now

    def test_main_marker_is_written_as_soon_as_the_email_was_attempted(self):
        src = (ROOT / "scripts" / "auto_lock_settle.py").read_text()
        i = src.index("sends_before = LIVE_SEND_ATTEMPTS")
        block = src[i:i + 2600]
        self.assertIn("finally:", block)
        self.assertIn("LIVE_SEND_ATTEMPTS > sends_before", block)
        self.assertLess(block.index("email_marker_path.write_text"), block.index("verify_todays_locks"))   # before the verify step, so a crash there cannot cause a re-send


# ───────────────────────── 3. watchdog: no phantom legs from the main gather ─────────────────────────
class WatchdogPhantoms(unittest.TestCase):
    def test_main_gather_only_counts_the_sports_the_main_pass_owns(self):
        self.assertEqual(a.MAIN_PASS_SPORTS, frozenset({"NFL", "NBA", "NHL"}))
        for owned_elsewhere in ("CFB", "SOC_PL", "SOC_CL", "SOC_LIGA", "SOC_ITA", "SHL", "LIIGA", "NLA", "EXTRALIGA"):
            self.assertNotIn(owned_elsewhere, a.MAIN_PASS_SPORTS)

    def test_a_leg_only_the_main_gather_qualifies_is_not_reported(self):
        now = 1_800_000_000_000.0
        phantom = leg("CFB", "MIZ ML", hA="MIZ", awA="TAM")
        phantom["startMs"] = now + 90 * 60000

        def build(res, only_sports=None, now=None, **k):
            if res.get("which") == "main" and (only_sports is None or "CFB" in only_sports):
                return [phantom]                      # the main gather's Monte-Carlo draw qualifies it ...
            return []                                 # ... the CFB runner's own gather never does

        owner = []
        with Patch((a, "gather_legs", lambda page: {"which": "main"}),
                   (a, "gather_hockey_legs_for_dates", lambda page, d: {"which": "hockey"}),
                   (a, "gather_cfb_legs_for_dates", lambda page, d: {"which": "cfb"}),
                   (a, "gather_soccer_legs_for_dates", lambda page, d: {"which": "soccer"}),
                   (a, "build_qualifying", build),
                   (a, "leg_locked_in_ledger", lambda page, q, d: False),
                   (a, "send_owner_alert", lambda s, h: owner.append(s) or True),
                   (a, "write_automation_status", lambda *x, **k: None),
                   (a, "LINEUP_CTX", None)):
            left = a.run_watchdog(None, True, now=now, auto_lock=True)
        self.assertEqual(left, [])
        self.assertEqual(owner, [])


# ───────────────────────── 4. email content ─────────────────────────
class Content(unittest.TestCase):
    def test_settlement_sections_use_league_names_not_ledger_codes(self):
        settled = [
            {"betOn": "Arsenal ML", "sport": "SOCCER", "league": "PL", "outcome": "win", "date": "2026-10-09", "hA": "ARS", "awA": "CHE", "hScore": 2, "aScore": 1},
            {"betOn": "Madrid ML", "sport": "SOCCER", "league": "LIGA", "outcome": "loss", "date": "2026-10-09", "hA": "RMA", "awA": "BAR", "hScore": 0, "aScore": 1},
            {"betOn": "Inter ML", "sport": "SOCCER", "league": "SERIEA", "outcome": "win", "date": "2026-10-09", "hA": "INT", "awA": "JUV", "hScore": 1, "aScore": 0},
            {"betOn": "Napoli ML", "sport": "SOCCER", "league": "CL", "outcome": "win", "date": "2026-10-09", "hA": "NAP", "awA": "RMA", "hScore": 1, "aScore": 0},
            {"betOn": "SF ML", "sport": "FOOTBALL", "league": "NFL", "outcome": "win", "date": "2026-10-09", "hA": "SF", "awA": "DAL", "hScore": 20, "aScore": 10},
        ]
        html = a.build_settlement_email_html(settled, unresolved=[{"betOn": "Liverpool ML", "sport": "SOCCER", "league": "PL"}])
        for want in ("Premier League (1W-0L)", "La Liga (0W-1L)", "Serie A (1W-0L)", "Champions League (1W-0L)", "NFL (1W-0L)", "Premier League: Liverpool ML"):
            self.assertIn(want, html)
        for bad in (">PL (", ">LIGA (", ">SERIEA (", ">CL (", "undefined", "NaN"):
            self.assertNotIn(bad, html)

    def test_cap_note_only_in_emails_that_carry_a_cfb_or_nfl_pick(self):
        self.assertIn("62% cap", a.build_locks_email_html([leg("CFB", "ARMY -3.5", side="sprdFav", tier=2, prob=0.62)], True, 1))
        self.assertIn("62% cap", a.build_locks_email_html([leg("NFL", "UNDER 43.5", side="under", tier=2, prob=0.62)], True, 1))
        for sport in ("NHL", "NBA", "SOC_PL", "SHL"):
            self.assertNotIn("62% cap", a.build_locks_email_html([leg(sport, "X ML")], True, 1))
        self.assertNotIn("62% cap", a.build_locks_email_html([], True, 0))

    def test_no_retired_league_names_in_a_current_email(self):
        html = a.build_locks_email_html([leg("SOC_PL", "Arsenal ML"), leg("NHL", "TOR ML"), leg("CFB", "ARMY ML")], True, 3)
        for bad in ("MLS", "Bundesliga", "MLB", "WNBA", "NCAAH", "undefined", "NaN", "None"):
            self.assertNotIn(bad, html)

    def test_every_sold_league_has_a_display_name(self):
        for sport in set().union(*a.PRODUCT_SPORTS.values()):
            self.assertIn(sport, a.SPORT_DISPLAY_NAME)
            self.assertIn(sport, a.SPORT_TO_LOCKPICK_TYPE)
        self.assertEqual(set(a.PRODUCT_SPORTS), set(sub.PRODUCTS))
        self.assertEqual(set(a.PRODUCT_LABEL), set(sub.PRODUCTS))


# ───────────────────────── 5. SMTP behaviour ─────────────────────────
class FakeSMTP:
    attempts = 0
    error = None

    def __init__(self, *a, **k):
        FakeSMTP.attempts += 1

    def __enter__(self): return self
    def __exit__(self, *a): return False
    def starttls(self): pass
    def login(self, *a): pass

    def sendmail(self, frm, to, msg):
        if FakeSMTP.error:
            raise smtplib.SMTPException(FakeSMTP.error)
        return {}


class Smtp(unittest.TestCase):
    def setUp(self):
        FakeSMTP.attempts, FakeSMTP.error = 0, None
        self.patch = Patch((smtplib, "SMTP", FakeSMTP), (gm, "GMAIL_APP_PASSWORD", "pw"), (gm.time, "sleep", lambda s: None))
        self.patch.__enter__()

    def tearDown(self):
        self.patch.__exit__()

    def test_a_transient_error_is_retried_then_reported(self):
        FakeSMTP.error = "Connection unexpectedly closed"
        ok, msg = gm.send_email("s", ["a@x.com"], "<p>x</p>")
        self.assertFalse(ok)
        self.assertEqual(FakeSMTP.attempts, gm.MAX_SEND_ATTEMPTS)
        self.assertGreaterEqual(gm.MAX_SEND_ATTEMPTS, 4)
        self.assertGreaterEqual(sum(gm.RETRY_BACKOFF_SECONDS), 30)               # rides out a short Gmail hiccup, not just a blink

    def test_a_quota_or_login_error_is_not_retried(self):
        for err in ("(550, b'5.4.5 Daily user sending limit exceeded')", "(535, b'5.7.8 Username and Password not accepted')"):
            FakeSMTP.attempts, FakeSMTP.error = 0, err
            ok, msg = gm.send_email("s", ["a@x.com"], "<p>x</p>")
            self.assertFalse(ok)
            self.assertEqual(FakeSMTP.attempts, 1, err)
            self.assertIn("not retried", msg)

    def test_recipients_are_deduplicated_case_insensitively(self):
        sent = {}

        class Cap(FakeSMTP):
            def sendmail(self, frm, to, msg):
                sent["to"] = list(to)
                return {}

        with Patch((smtplib, "SMTP", Cap)):
            ok, msg = gm.send_email("s", ["Owner@X.com", "owner@x.com", " a@x.com ", "", None], "<p>x</p>")
        self.assertTrue(ok)
        self.assertEqual(sent["to"], ["Owner@X.com", "a@x.com"])
        self.assertIn("2 recipient", msg)

    def test_blank_recipient_is_no_recipient(self):
        self.assertEqual(gm.send_email("s", "   ", "<p>x</p>"), (False, "no recipient"))
        self.assertEqual(gm.send_email("s", [], "<p>x</p>"), (False, "no recipient"))

    def test_a_large_list_is_batched_under_the_gmail_cap(self):
        calls = []

        class Cap(FakeSMTP):
            def sendmail(self, frm, to, msg):
                calls.append(len(to))
                return {}

        with Patch((smtplib, "SMTP", Cap)):
            ok, msg = gm.send_email("s", [f"u{i}@x.com" for i in range(1000)], "<p>x</p>")
        self.assertTrue(ok)
        self.assertTrue(all(n <= 500 for n in calls))
        self.assertEqual(sum(calls), 1000)


# ───────────────────────── 6. subscriber list robustness ─────────────────────────
class Subscribers(unittest.TestCase):
    def test_one_malformed_row_does_not_stop_everyone_else(self):
        import os
        added = sub._now().isoformat()
        data = {"nfl": [{"email": "good@x.com", "added": added}, {"added": added}, "junk", {"email": "no-at-sign", "added": added}, {"email": "ok2@x.com", "added": added}]}
        saved = os.environ.get("GITHUB_ACTIONS")
        os.environ["GITHUB_ACTIONS"] = "true"
        sub.DATA_ERRORS.clear()
        sub._BAD_ROWS_SEEN.clear()
        try:
            with Patch((sub, "load_subscribers", lambda: data), (sub, "OWNER_EMAIL", "")):
                self.assertEqual(sub.recipients_for("nfl"), ["good@x.com", "ok2@x.com"])
            self.assertEqual(len(sub.DATA_ERRORS), 3)                          # loud in CI (the run ends red) ...
            self.assertTrue(all("malformed subscriber row" in m for m in sub.DATA_ERRORS))
        finally:
            sub.DATA_ERRORS.clear()
            sub._BAD_ROWS_SEEN.clear()
            if saved is None:
                os.environ.pop("GITHUB_ACTIONS", None)
            else:
                os.environ["GITHUB_ACTIONS"] = saved

    def test_owner_address_in_other_case_is_not_duplicated(self):
        added = sub._now().isoformat()
        with Patch((sub, "load_subscribers", lambda: {"nba": [{"email": "owner@x.com", "added": added}, {"email": "a@x.com", "added": added}]}),
                   (sub, "OWNER_EMAIL", "Owner@X.com")):
            self.assertEqual(sub.recipients_for("nba"), ["Owner@X.com", "a@x.com"])

    def test_expired_and_unknown_products_are_excluded(self):
        now = sub._now()
        data = {"nfl": [{"email": "old@x.com", "added": (now - timedelta(days=31)).isoformat()}, {"email": "new@x.com", "added": (now - timedelta(days=29)).isoformat()},
                        {"email": "nodate@x.com"}],
                "mlb": [{"email": "retired@x.com", "added": now.isoformat()}]}
        with Patch((sub, "load_subscribers", lambda: data), (sub, "OWNER_EMAIL", "")):
            self.assertEqual(sub.recipients_for("nfl"), ["new@x.com"])
            self.assertNotIn("retired@x.com", sum((sub.recipients_for(p) for p in sub.PRODUCTS), []))


# ───────────────────────── 7. owner-side monitoring ─────────────────────────
class Monitoring(unittest.TestCase):
    def test_pending_email_is_an_alert_level_health_problem(self):
        d = Path(tempfile.mkdtemp())
        now = datetime(2026, 10, 10, 15, 0, tzinfo=MT)
        (d / "p.json").write_text(json.dumps({"MAIN HOCKEY": {"date": "2026-10-10"}, "CFB": {"date": "2026-10-09"}, "OLD": {"date": "2026-10-01"}}))
        out = hc.check_pending_emails(d / "p.json", now)
        self.assertEqual(len(out), 2)
        self.assertTrue(any("MAIN HOCKEY" in m for m in out) and any("CFB" in m for m in out))
        self.assertEqual(hc.check_pending_emails(d / "missing.json", now), [])
        (d / "bad.json").write_text("{nope")
        self.assertEqual(hc.check_pending_emails(d / "bad.json", now), [])

    def test_alert_keys_are_stable_across_passes(self):
        m1 = hc.check_pending_emails(self._file({"X": {"date": "2026-10-10"}}), datetime(2026, 10, 10, 9, tzinfo=MT))[0]
        m2 = hc.check_pending_emails(self._file({"X": {"date": "2026-10-10"}}), datetime(2026, 10, 10, 18, tzinfo=MT))[0]
        self.assertEqual(hc._problem_key(m1), hc._problem_key(m2))

    def _file(self, data):
        p = Path(tempfile.mkdtemp()) / "p.json"
        p.write_text(json.dumps(data))
        return p

    def test_the_email_senders_without_a_watcher_are_now_monitored(self):
        monitored = {f for f, _l, _h in hc.MONITORED}
        for wf in ("lock-watchdog.yml", "verify-lock-workflows.yml", "weekly-health-digest.yml"):
            self.assertIn(wf, monitored)
            self.assertTrue((ROOT / ".github" / "workflows" / wf).exists())

    def test_health_check_takes_a_pass_input_and_the_worker_fires_each_pass(self):
        wf = (ROOT / ".github" / "workflows" / "daily-health-check.yml").read_text()
        self.assertIn("pass:", wf)
        self.assertIn("early|full|late) PASS=\"$PASS_INPUT\"", wf)
        self.assertNotIn("${{ inputs.pass }}\"", wf.split("run: |")[1])          # the input reaches the shell through an env var, never interpolated into the script
        worker = (ROOT / "scheduler" / "worker.js").read_text()
        for t, p in (("17:15", "early"), ("20:33", "full"), ("23:15", "late")):
            self.assertIn(f'at: ["{t}"], inputs: {{ pass: "{p}" }}', worker)
            self.assertIn(f"cron: '{t[3:]} {int(t[:2])} * * *'", wf)             # the worker table mirrors the workflow's own cron


if __name__ == "__main__":
    unittest.main(verbosity=2)
