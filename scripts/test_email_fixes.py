#!/usr/bin/env python3
"""Regression tests for the 2026-10-04 email audit fixes (owner-facing social/digest emails, subscriber emails, alerts).  Pure-Python builders (no network, nothing is sent)
plus one browser check for the yearly-total scope. Sections are added per fix group."""
import functools, http.server, json, socketserver, sys, threading, unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import generate_social_cards as g  # noqa: E402


class SocialCards(unittest.TestCase):
    def test_stale_hockey_stripper_is_gone(self):
        self.assertFalse(hasattr(g, "_strip_stale_hockey"))            # it deleted REAL hockey from the monthly recap

    def test_basis_wording_is_honest(self):
        self.assertEqual(g.TALLY_NOTE, "Excludes picks known to have been locked after game start.")
        stats = {"w": 10, "l": 5, "pct": 0.66, "units": 4.0, "lockedCount": 15}
        now = datetime(2026, 11, 1, 9, 0, tzinfo=ZoneInfo("America/Denver"))
        caps = [g.build_monthly_caption(stats, now), g.build_yearly_caption(stats, 2026), g.build_alltime_caption(stats), g.build_covers_caption()]
        for c in caps:
            blob = (c["instagram"] + c["x"]).lower()
            for bad in ("cherry", "deleted losses", "every result"):
                self.assertNotIn(bad, blob)

    def test_no_retired_leagues_or_typos(self):
        self.assertNotIn("KHL", g.SPORT_LEAGUES["HOCKEY"])
        cap = g.build_daily_caption({"w": 3, "l": 1, "pct": .75, "units": 1.0, "lockedCount": 4}, datetime(2026, 10, 3, tzinfo=ZoneInfo("America/Denver")))
        self.assertNotIn("Yesterdays", cap["instagram"])

    def test_failed_send_is_recorded_not_raised(self):
        calls = []
        orig = (g._send_gmail, g.SOCIAL_CARD_EMAIL_TO)
        g._send_gmail = lambda *a, **k: (calls.append(a[0]) or (False, "boom"))
        g.SOCIAL_CARD_EMAIL_TO = "owner@example.com"
        g.SEND_FAILURES.clear()
        try:
            g.send_email("Subj A", [], {"instagram": "i", "x": "x"})        # must not raise: later emails still go out
            g.send_email("Subj B", [], {"instagram": "i", "x": "x"})
            self.assertEqual(calls, ["Subj A", "Subj B"]); self.assertEqual(g.SEND_FAILURES, ["Subj A", "Subj B"])
        finally:
            g._send_gmail, g.SOCIAL_CARD_EMAIL_TO = orig; g.SEND_FAILURES.clear()


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a, **k):
        pass


class YearScope(unittest.TestCase):
    def test_year_total_matches_its_rows_and_excludes_parlays_and_retired(self):
        from playwright.sync_api import sync_playwright
        socketserver.TCPServer.allow_reuse_address = True
        srv = socketserver.TCPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=str(ROOT / "docs")))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        def pk(i, sport, bt, outcome, hA="AAA"):
            return {"id": f"y{i}", "sport": sport, "betType": bt, "betOn": "X", "hA": hA, "awA": "BBB", "date": "2026-10-02", "lockedAt": 1791000000000 + i, "outcome": outcome, "decOdds": 2.0, "ml": "+100", "winProb": .6}
        picks = [pk(1, "NHL", "ML", "win"), pk(2, "NFL", "ML", "loss"), pk(3, "NHL", "PARLAY", "win", hA="PARLAY"), pk(4, "MLS", "ML", "win"), pk(5, "MLB", "ML", "win")]
        with sync_playwright() as p:
            b = p.chromium.launch(); pg = b.new_page()
            pg.goto(f"http://127.0.0.1:{srv.server_address[1]}/app.html?nosb=1")
            pg.wait_for_function("typeof saveP==='function'"); pg.wait_for_timeout(800); pg.evaluate("(x)=>saveP(x)", picks)
            ys = g.get_year_stats(pg, 2026, {"ids": [], "rows": picks, "idx": {}})
            b.close()
        srv.shutdown()
        self.assertEqual((ys["w"], ys["l"]), (1, 1))                                  # only the NHL win and the NFL loss count
        self.assertEqual(sum(s["w"] for s in ys["bySport"]), ys["w"]); self.assertEqual(sum(s["l"] for s in ys["bySport"]), ys["l"])


# ───────────────────────── locks / digest emails (auto_lock_settle.py) ─────────────────────────
import auto_lock_settle as a  # noqa: E402


def _game(sport="NFL", label="SF +3.5", alt=None, tier=3, ev=0.08, **kw):
    q = {"kind": "GAME", "sport": sport, "hA": "SF", "awA": "SEA", "side": "sprdDog", "label": label, "prob": 0.69, "ml": "-272", "dec": 1.37,
         "tierN": tier, "evVal": ev, "lane": False, "priceSource": "estimated" if alt else "market"}
    if alt:
        q["altLine"] = alt
    q.update(kw)
    return q


ALT = {"posted": -3.0, "line": 3.5, "shift": 6.5, "postedLabel": "SF -3.0", "postedProb": 0.85}


class LocksEmail(unittest.TestCase):
    def test_adjusted_line_pick_is_labelled_and_has_no_ev_or_posted_tier(self):
        html = a._leg_html(_game(alt=ALT))
        self.assertIn("ADJUSTED LINE", html); self.assertIn("SF -3.0", html)             # the original posted line is shown
        self.assertIn("estimate", html); self.assertNotIn("EV ", html); self.assertNotIn("PREMIUM", html)
        plain = a._leg_html(_game())                                                   # a normal pick is unchanged: tier badge + EV
        self.assertIn("PREMIUM", plain); self.assertIn("EV +8.0%", plain); self.assertNotIn("ADJUSTED", plain)

    def test_legend_explains_adjusted_lines_and_hockey_claims_are_true(self):
        html = a.build_locks_email_html([_game(alt=ALT)], live=True, locked_count=1)
        self.assertIn("ADJUSTED LINE", html)
        self.assertIn("exception (see ADJUSTED LINE)", html)                           # hockey 'real price' claim no longer absolute
        self.assertIn("price at the posted line is the consensus", html)
        self.assertIn("clairvoyanceengine.info", html); self.assertIn("reply", html.lower())  # subscriber contact footer
        for retired in ("MLS", "Bundesliga", "parlay", "MLB", "WNBA"):
            self.assertNotIn(retired, html)

    def test_posted_line_context_is_labelled_when_a_leg_is_shifted(self):
        leg = _game(alt=ALT, mcSummary="MC: SF wins 85%", best={"label": "SF -3.0", "tierN": 3})
        html = a.build_locks_email_html([leg], live=True, locked_count=1)
        self.assertIn("Context at the POSTED line", html)

    def test_send_failure_is_recorded_and_returned(self):
        orig = (a._send_gmail, a.LOCKS_EMAIL_TO, a.OWNER_EMAIL)
        a._send_gmail = lambda *x, **k: (False, "smtp down"); a.LOCKS_EMAIL_TO = "to@example.com"
        a.EMAIL_FAILURES.clear()
        try:
            self.assertFalse(a.send_locks_email([_game()], live=True, locked_count=1, label="NFL", to=["to@example.com"]))
            self.assertEqual(len(a.EMAIL_FAILURES), 1)
            a._send_gmail = lambda *x, **k: (True, "")
            self.assertTrue(a.send_locks_email([_game()], live=True, locked_count=1, label="NFL", to=["to@example.com"]))
        finally:
            a._send_gmail, a.LOCKS_EMAIL_TO, a.OWNER_EMAIL = orig; a.EMAIL_FAILURES.clear()

    def test_subject_date_is_mountain_time(self):
        seen = {}
        orig = a._send_gmail
        a._send_gmail = lambda subject, *x, **k: (seen.setdefault("s", subject) and (True, ""))
        try:
            a.send_locks_email([_game()], live=True, locked_count=1, label="NFL", to=["to@example.com"])
        finally:
            a._send_gmail = orig
        today_mt = datetime.now(ZoneInfo("America/Denver")).strftime("%Y-%m-%d")
        self.assertIn(today_mt, seen["s"])


class NbaPreseasonGuard(unittest.TestCase):
    def test_preseason_legs_are_never_qualified(self):
        pre = datetime.now(ZoneInfo("America/Denver")).strftime("%Y-%m-%dT18:00Z")
        orig = a._nba_preseason_pairs
        a._nba_preseason_pairs = lambda: {frozenset({"UTAH", "DEN"})}
        try:
            game = {"sport": "NBA", "hA": "DEN", "awA": "UTAH", "startMs": None, "markets": [{"side": "over", "label": "OVER 220.5", "prob": .8, "tierN": 3, "evVal": .1, "ml": "-110", "dec": 1.9}]}
            reg = dict(game, hA="BOS", awA="NYK")
            props = [{"grade": "PREMIUM", "sportTag": "NBA", "hA": "DEN", "awA": "UTAH", "player": "X"}, {"grade": "PREMIUM", "sportTag": "NBA", "hA": "BOS", "awA": "NYK", "player": "Y"}]
            q = a.build_qualifying({"gameLegs": [game, reg], "propLegs": props})
        finally:
            a._nba_preseason_pairs = orig
        teams = {(x.get("hA") or x["leg"].get("hA")) for x in q}
        self.assertNotIn("DEN", teams); self.assertIn("BOS", teams)          # preseason pair dropped (game AND prop), regular-season pair kept

    def test_pairs_come_from_the_schedule_files(self):
        pairs = a._nba_preseason_pairs()                                      # runs against docs/nba_schedule.json: must be a set and never raise
        self.assertIsInstance(pairs, set)


class TopPicksDigestRetired(unittest.TestCase):
    def test_digest_is_gone(self):
        for name in ("send_top_picks_digest_email", "build_top_picks_digest", "build_top_picks_digest_html", "gather_todays_locked_bets"):
            self.assertFalse(hasattr(a, name), name)
        self.assertFalse((ROOT / ".github" / "workflows" / "top-picks-digest.yml").exists())
        self.assertNotIn("--top-picks-digest", (ROOT / "scripts" / "auto_lock_settle.py").read_text())


class PickOfDay(unittest.TestCase):
    def setUp(self):
        import generate_pick_of_day_social as pod
        self.pod = pod

    def test_only_premium_optimal_in_scope_sports(self):
        qs = [_game(tier=3), _game(tier=1, label="SF -1.5"), _game(tier=0, label="SF -2.5"), _game(sport="NCAAB", tier=3, label="DUKE -4"), _game(sport="MLS", tier=3, label="X -1")]
        picks = self.pod.select_top_picks(qs, n=5)
        self.assertEqual([c["rank_tier"] for c in picks], [3])

    def test_alt_line_caption_has_no_edge_claim_or_lock_emoji(self):
        c = self.pod.select_top_picks([_game(alt=ALT, ev=0.12)])[0]
        cap = self.pod.build_pick_caption(c, "October 4, 2026")
        self.assertNotIn("Model edge", cap)
        self.assertNotIn("\U0001f512", cap)
        self.assertIn("price is an estimate", cap)
        self.assertIn("SF -3.0", cap)

    def test_market_priced_caption_keeps_edge(self):
        c = self.pod.select_top_picks([_game(ev=0.08)])[0]
        self.assertIn("Model edge: EV +8.0%", self.pod.build_pick_caption(c, "October 4, 2026"))

    def test_workflow_marker_requires_a_real_send(self):
        wf = (ROOT / ".github" / "workflows" / "pick-of-day-social-daily.yml").read_text()
        self.assertIn("id: post", wf)
        self.assertIn('if [ "${{ steps.post.outputs.sent }}" = "1" ]', wf)       # the "email went out" marker is conditional on a real send
        self.assertIn("_checked.txt", wf)                                          # the health marker is written by any completed run

    def test_sent_flag_written_only_when_called(self):
        import os, tempfile
        with tempfile.NamedTemporaryFile("r+", delete=False) as fh:
            os.environ["GITHUB_OUTPUT"] = fh.name
            self.pod._flag_sent()
            fh.seek(0)
            self.assertEqual(fh.read(), "sent=1\n")
        os.environ.pop("GITHUB_OUTPUT")


class Lifecycle(unittest.TestCase):
    def setUp(self):
        import _subscribers as sub, send_expiry_reminders as rem, _gmail_email as gm, send_demo_emails as demo
        self.sub, self.rem, self.gm, self.demo = sub, rem, gm, demo

    def test_reminder_copy_dates_and_footer(self):
        subj, body = self.rem._build_email([("nfl", 2), ("cfb", 3)], {"nfl": "2026-10-06T03:00:00+00:00", "cfb": "2026-10-07T03:00:00+00:00"})
        self.assertIn("October 05, 2026", body)                        # 03:00 UTC on the 6th is still the 5th in Mountain time
        self.assertNotIn("whenever it", body)
        self.assertIn("does not add to the time you have left", body)
        self.assertIn("want to stop these emails", body)
        self.assertIn("2 of your subscriptions", body)

    def _run_reminders(self, send_ok):
        rem, marked, sent = self.rem, [], []
        rows = [{"product": "nfl", "email": "a@x.com", "days_left": 2, "expires": "2026-10-06T03:00:00+00:00"},
                {"product": "cfb", "email": "b@x.com", "days_left": 2, "expires": "2026-10-06T03:00:00+00:00"}]
        orig = (rem.subscribers_needing_reminder, rem.mark_reminder_sent, rem.send_email, sys.argv)
        rem.subscribers_needing_reminder = lambda days_before=3: rows
        rem.mark_reminder_sent = lambda p, e: marked.append(e)
        rem.send_email = lambda subj, to, body: (sent.append(to) or (to in send_ok, "boom"))
        sys.argv = ["send_expiry_reminders.py"]
        try:
            rem.main()
            code = 0
        except SystemExit as e:
            code = e.code
        finally:
            rem.subscribers_needing_reminder, rem.mark_reminder_sent, rem.send_email, sys.argv = orig
        return code, marked

    def test_failed_reminder_exits_nonzero_and_is_not_marked(self):
        code, marked = self._run_reminders({"a@x.com"})
        self.assertEqual(code, 1)
        self.assertEqual(marked, ["a@x.com"])                          # only the delivered one is marked; b retries tomorrow

    def test_all_sent_exits_zero(self):
        self.assertEqual(self._run_reminders({"a@x.com", "b@x.com"})[0], 0)

    def test_reminder_workflow_serialised_and_commits_state_on_failure(self):
        wf = (ROOT / ".github" / "workflows" / "send-expiry-reminders.yml").read_text()
        self.assertIn("concurrency:", wf)
        self.assertIn("always() && !inputs.dry_run", wf)

    def test_retired_product_keys_are_ignored(self):
        orig = self.sub.load_subscribers
        added = self.sub._now().isoformat()
        self.sub.load_subscribers = lambda: {"mlb": [{"email": "a@x.com", "added": added}], "nfl": [{"email": "a@x.com", "added": added}]}
        try:
            self.assertEqual([r["product"] for r in self.sub.products_for_email("a@x.com")], ["nfl"])
        finally:
            self.sub.load_subscribers = orig

    def test_receipt_is_an_access_confirmation_not_a_payment_receipt(self):
        orig_load, orig_send, captured = self.sub.load_subscribers, self.sub._send_gmail, {}
        added = self.sub._now().isoformat()
        self.sub.load_subscribers = lambda: {"nfl": [{"email": "a@x.com", "added": added}]}
        self.sub._send_gmail = lambda subj, to, body: (captured.update(subject=subj, body=body) or (True, "sent"))
        try:
            self.assertTrue(self.sub.send_receipt_email("a@x.com")[0])
        finally:
            self.sub.load_subscribers, self.sub._send_gmail = orig_load, orig_send
        blob = captured["subject"] + captured["body"]
        for bad in ("Payment Receipt", "/mo)", "per month", "whenever you pay again"):
            self.assertNotIn(bad, blob)
        self.assertIn("Access Confirmation", captured["body"])
        self.assertIn("standard price for 1 product", captured["body"])

    def test_refused_recipient_is_reported_not_swallowed(self):
        import smtplib

        class FakeSMTP:
            def __init__(self, *a, **k): pass
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def starttls(self): pass
            def login(self, *a): pass
            def sendmail(self, frm, to, msg): return {"bad@x.com": (550, b"no such user")}

        orig_smtp, orig_pw = smtplib.SMTP, self.gm.GMAIL_APP_PASSWORD
        smtplib.SMTP, self.gm.GMAIL_APP_PASSWORD = FakeSMTP, "pw"
        try:
            ok, msg = self.gm.send_email("s", ["good@x.com", "bad@x.com"], "<p>x</p>")
        finally:
            smtplib.SMTP, self.gm.GMAIL_APP_PASSWORD = orig_smtp, orig_pw
        self.assertFalse(ok)
        self.assertIn("1/2 delivered", msg)
        self.assertIn("bad@x.com", msg)

    def _run_demo(self, ok):
        demo, calls = self.demo, []
        orig_send, argv = demo.send_email, sys.argv
        demo.send_email = lambda subj, to, body: (calls.append((subj, to, body)) or (ok, "boom"))
        sys.argv = ["send_demo_emails.py", "--to", "me@x.com"]
        try:
            demo.main()
            code = 0
        except SystemExit as e:
            code = e.code
        finally:
            demo.send_email, sys.argv = orig_send, argv
        return code, calls

    def test_demo_goes_to_requested_recipient_labelled_as_sample(self):
        code, calls = self._run_demo(True)
        self.assertEqual(code, 0)
        self.assertEqual(len(calls), 5)
        for subj, to, body in calls:
            self.assertEqual(to, "me@x.com")
            self.assertIn("DEMO (sample data)", subj)
            self.assertNotIn("DRY RUN", subj)
            self.assertIn("SAMPLE DATA", body)

    def test_demo_failure_is_fatal(self):
        self.assertNotEqual(self._run_demo(False)[0], 0)

    def test_demo_workflow_has_no_shell_interpolated_input(self):
        wf = (ROOT / ".github" / "workflows" / "send-demo-emails.yml").read_text()
        self.assertIn("DEMO_TO: ${{ inputs.to }}", wf)
        self.assertNotIn("format('--to", wf)


class HealthAlerts(unittest.TestCase):
    def setUp(self):
        import daily_health_check as dh, weekly_health_digest as wd, tempfile
        self.dh, self.wd = dh, wd
        dh.ALERT_STATE_PATH = Path(tempfile.mkdtemp()) / "health_alert_state.json"      # tests must never write the real alert state

    def test_marker_passes_are_scoped_and_pm_slate_is_checked_late(self):
        mt = ZoneInfo("America/Denver")
        at = lambda h: datetime(2026, 10, 4, h, 15, tzinfo=mt)
        orig = self.dh.LOCK_MARKERS
        self.dh.LOCK_MARKERS = [(ROOT / "data" / "nope_am.txt", "AM", 10), (ROOT / "data" / "nope_pm.txt", "PM", 16)]
        try:
            self.assertEqual(len(self.dh.check_lock_markers("early", at(11))), 1)     # AM only
            self.assertEqual(len(self.dh.check_lock_markers("late", at(17))), 1)      # PM only
            self.assertEqual(len(self.dh.check_lock_markers("full", at(17))), 2)
            self.assertEqual(self.dh.check_lock_markers("late", at(15)), [])          # before the PM cutoff: nothing yet
        finally:
            self.dh.LOCK_MARKERS = orig

    def test_pick_of_day_health_markers_are_the_checked_files(self):
        names = [p.name for p, _l, _h in self.dh.LOCK_MARKERS]
        self.assertIn("last_pick_of_day_am_checked.txt", names)
        self.assertIn("last_pick_of_day_pm_checked.txt", names)
        wf = (ROOT / ".github" / "workflows" / "pick-of-day-social-daily.yml").read_text()
        self.assertIn("_checked.txt", wf)

    def test_undeliverable_alert_is_a_nonzero_exit(self):
        orig_to, orig_send = self.dh.ALERT_TO, self.dh.send_email
        try:
            self.dh.ALERT_TO = ""
            self.assertEqual(self.dh._report(["x is down"], []), 1)
            self.dh.ALERT_TO, self.dh.send_email = "me@x.com", lambda *a, **k: (False, "boom")
            self.assertEqual(self.dh._report(["x is down"], []), 1)
            self.dh.send_email = lambda *a, **k: (True, "sent")
            self.assertEqual(self.dh._report(["x is down"], []), 0)
            self.assertEqual(self.dh._report([], []), 0)
        finally:
            self.dh.ALERT_TO, self.dh.send_email = orig_to, orig_send

    def test_each_problem_is_emailed_once_per_day(self):
        import tempfile
        from pathlib import Path as P
        dh, sent = self.dh, []
        orig = dh.ALERT_TO, dh.send_email
        dh.ALERT_TO, dh.send_email = "me@x.com", lambda subj, to, body: (sent.append(body) or (True, "sent"))
        st = P(tempfile.mkdtemp()) / "state.json"
        try:
            a1 = "NFL schedule: STALE -- nfl_schedule.json stamp is 40.2h old (stale at 36h) -- <a href=\"https://x/y\">Run</a>"
            a2 = "NFL schedule: STALE -- nfl_schedule.json stamp is 46.9h old (stale at 36h) -- <a href=\"https://x/z\">Run</a>"   # same problem, later pass
            b1 = "CFB Early Lock: no successful live lock recorded"
            self.assertEqual(dh._report([a1], [], st, "2026-10-04"), 0); self.assertEqual(len(sent), 1)
            self.assertEqual(dh._report([a2], [], st, "2026-10-04"), 0); self.assertEqual(len(sent), 1)        # repeat suppressed
            self.assertEqual(dh._report([a2, b1], [], st, "2026-10-04"), 0); self.assertEqual(len(sent), 2)    # only the new one goes out
            self.assertNotIn("NFL schedule", sent[1]); self.assertIn("CFB Early Lock", sent[1])
            self.assertEqual(dh._report([a2], [], st, "2026-10-05"), 0); self.assertEqual(len(sent), 3)        # next day: sent again
            dh.send_email = lambda *x, **k: (False, "boom")
            self.assertEqual(dh._report(["brand new problem"], [], st, "2026-10-05"), 1)                       # failed send is not recorded...
            dh.send_email = lambda subj, to, body: (sent.append(body) or (True, "sent"))
            dh._report(["brand new problem"], [], st, "2026-10-05"); self.assertEqual(len(sent), 4)            # ...so the next pass retries it
        finally:
            dh.ALERT_TO, dh.send_email = orig

    def test_pick_of_day_marker_accepts_either_scheme_during_the_switch(self):
        import tempfile
        from pathlib import Path as P
        d = P(tempfile.mkdtemp())
        (d / "last_pick_of_day_am_date.txt").write_text("2026-10-04")
        orig = self.dh.LOCK_MARKERS
        self.dh.LOCK_MARKERS = [(d / "last_pick_of_day_am_checked.txt", "AM", 10)]
        try:
            at = datetime(2026, 10, 4, 12, 0, tzinfo=ZoneInfo("America/Denver"))
            self.assertEqual(self.dh.check_lock_markers("full", at), [])
            (d / "last_pick_of_day_am_date.txt").write_text("2026-10-03")
            self.assertEqual(len(self.dh.check_lock_markers("full", at)), 1)
        finally:
            self.dh.LOCK_MARKERS = orig

    def test_cancelled_latest_run_is_not_a_failure(self):
        orig = self.dh._api_get
        runs = [{"conclusion": "cancelled", "status": "completed", "created_at": datetime.now(ZoneInfo("UTC")).isoformat()},
                {"conclusion": "success", "status": "completed", "created_at": datetime.now(ZoneInfo("UTC")).isoformat()}]
        self.dh._api_get = lambda path: {"workflow_runs": runs}
        try:
            self.assertIsNone(self.dh.check_workflow("x.yml", "X", 26))
        finally:
            self.dh._api_get = orig

    def test_watchdog_alert_does_not_claim_a_lock_failed(self):
        rep = {"live": True, "label": "WATCHDOG", "failedLabels": [],
               "skippedDetail": [{"sport": "NHL", "game": "A @ B", "leg": "A ML", "startMs": 1, "why": "kicks off in 30 min", "prob": .7, "tier": "PREMIUM"}]}
        h, subject, html = a.owner_alert_for([rep], watchdog=True)
        self.assertIn("WATCHDOG", subject)
        self.assertIn("never locks anything", html)
        self.assertNotIn("lock failed", html.lower())
        self.assertNotIn("could not lock", html)

    def test_failed_owner_alert_marks_the_run_red(self):
        before = len(a.EMAIL_FAILURES)
        orig = a.send_owner_alert, a.write_automation_status, a.pass_entries
        a.send_owner_alert = lambda s, h: False
        a.write_automation_status = lambda *x, **k: None
        a.pass_entries = lambda *x, **k: []
        rep = {"live": True, "label": "X", "failedLabels": ["boom"], "skippedDetail": []}
        try:
            a.finish_live_pass("lastLock", True, "d", [rep], None)
        finally:
            a.send_owner_alert, a.write_automation_status, a.pass_entries = orig
        self.assertEqual(len(a.EMAIL_FAILURES), before + 1)
        a.EMAIL_FAILURES[:] = a.EMAIL_FAILURES[:before]

    def test_lock_missed_alert_has_no_stale_times_and_fails_loudly(self):
        src = (ROOT / "scripts" / "auto_lock_settle.py").read_text()
        seg = src[src.index("if args.alert_lock_missed:"):src.index("do_lock, do_settle = (args.lock")]
        for stale in ("11:27 PM", "3:07 AM", "9am-12pm", "4:44pm"):
            self.assertNotIn(stale, seg)
        self.assertIn("sys.exit(0 if ok else 1)", seg)

    def test_weekly_digest_scope_and_window(self):
        wd = self.wd
        self.assertNotIn("MLS", wd.ACTIVE_TAGS)
        self.assertNotIn("BUND", wd.ACTIVE_TAGS)
        self.assertFalse(wd.is_active_sport({"sport": "NFL", "betType": "PARLAY"}))
        self.assertTrue(wd.is_active_sport({"sport": "NFL", "betType": "SPREAD"}))
        seen = {}
        orig = wd._api_get
        wd._api_get = lambda path: (seen.update(path=path) or {"workflow_runs": []})
        try:
            wd.workflow_success_rate("live-tracker.yml")
        finally:
            wd._api_get = orig
        self.assertIn("per_page=100", seen["path"])
        self.assertIn("created=%3E%3D", seen["path"])

    def test_weekly_digest_exit_codes(self):
        wd, orig = self.wd, (self.wd.ALERT_TO, self.wd.send_email, self.wd.probe_supabase, self.wd.load_ledger, self.wd.GITHUB_TOKEN)
        try:
            wd.GITHUB_TOKEN = ""
            wd.probe_supabase = lambda u, k: "down"
            wd.ALERT_TO = ""
            self.assertEqual(wd.main(), 1)
            wd.ALERT_TO, wd.send_email = "me@x.com", lambda *x, **k: (False, "boom")
            self.assertEqual(wd.main(), 1)
            wd.send_email = lambda *x, **k: (True, "sent")
            self.assertEqual(wd.main(), 0)
        finally:
            wd.ALERT_TO, wd.send_email, wd.probe_supabase, wd.load_ledger, wd.GITHUB_TOKEN = orig


class PrivateData(unittest.TestCase):
    """The subscriber list lives in a private repo: where it is read from, and that an unreadable list is loud in CI, not "no subscribers"."""
    def reload_with(self, **env):
        import importlib, os, tempfile
        saved = {k: os.environ.get(k) for k in ("CV_PRIVATE_DIR", "GITHUB_ACTIONS", "HOME")}
        for k, v in env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        import _subscribers
        try:
            return importlib.reload(_subscribers)
        finally:
            self._restore = saved

    def tearDown(self):
        import importlib, os
        for k, v in getattr(self, "_restore", {}).items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        import _subscribers
        importlib.reload(_subscribers)

    def test_env_dir_wins_and_sync_commits_inside_it(self):
        import tempfile
        d = tempfile.mkdtemp()
        m = self.reload_with(CV_PRIVATE_DIR=d, GITHUB_ACTIONS=None)
        self.assertEqual(m.DATA_SOURCE, "private")
        self.assertEqual(str(m.SUBSCRIBERS_FILE), d + "/subscribers.json")
        self.assertEqual(str(m.REPO_ROOT), d)
        self.assertEqual(m._SYNC_RELS, ["subscribers.json", "subscriber_events.json"])
        m.add_subscriber("nfl", "a@x.com")
        self.assertTrue((Path(d) / "subscribers.json").exists())
        legacy = ROOT / "data" / "subscribers.json"                                              # (deleted from the public repo once the private repo went live)
        self.assertFalse(legacy.exists() and "a@x.com" in legacy.read_text())                   # nothing leaked into the public repo's file

    def test_legacy_fallback_when_nothing_is_configured(self):
        import tempfile
        m = self.reload_with(CV_PRIVATE_DIR=None, GITHUB_ACTIONS=None, HOME=tempfile.mkdtemp())
        self.assertEqual(m.DATA_SOURCE, "legacy")
        self.assertEqual(m._SYNC_RELS, ["data/subscribers.json", "data/subscriber_events.json"])

    def test_unreadable_list_in_actions_is_an_error_not_an_empty_list(self):
        import tempfile
        m = self.reload_with(CV_PRIVATE_DIR=tempfile.mkdtemp(), GITHUB_ACTIONS="true")          # a checkout that has no subscribers.json
        self.assertEqual(m.load_subscribers(), {})
        self.assertEqual(len(m.DATA_ERRORS), 1)
        self.assertIn("subscribers will NOT be emailed", m.DATA_ERRORS[0])
        self.assertEqual(m.recipients_for("nfl"), [e for e in [m.OWNER_EMAIL] if e])

    def test_missing_list_locally_is_just_empty(self):
        import tempfile
        m = self.reload_with(CV_PRIVATE_DIR=tempfile.mkdtemp(), GITHUB_ACTIONS=None)
        self.assertEqual(m.load_subscribers(), {})
        self.assertEqual(m.DATA_ERRORS, [])

    def test_workflows_use_the_private_data_action_and_expiry_commits_there(self):
        for wf in ("auto-lock-settle", "cfb-lock-early", "cfb-lock-evening", "european-lock-early", "hockey-lock-evening", "soccer-lock-evening", "send-expiry-reminders"):
            text = (ROOT / ".github" / "workflows" / f"{wf}.yml").read_text()
            self.assertIn("uses: ./.github/actions/private-data", text, wf)
            self.assertIn("PRIVATE_REPO_TOKEN", text, wf)
        text = (ROOT / ".github" / "workflows" / "send-expiry-reminders.yml").read_text()
        self.assertIn('"$CV_PRIVATE_DIR"', text)
        self.assertIn(".private-data/", (ROOT / ".gitignore").read_text())


if __name__ == "__main__":
    unittest.main(verbosity=2)
