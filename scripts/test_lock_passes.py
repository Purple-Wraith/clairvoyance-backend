#!/usr/bin/env python3
"""Unit tests for the lock-pass reliability helpers in scripts/auto_lock_settle.py (no browser, no network, no email).

    python3 scripts/test_lock_passes.py

Covers: rolling-horizon dates (the after-midnight bug), per-leg lock dates, game analysis / completeness inputs, the zero-pick
email decision, the owner pre-kickoff alert (content, hash, owner-only), Engine Health pass entries, the lock_prep kickoff window.
"""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
import auto_lock_settle as A  # noqa: E402
import lock_prep as P  # noqa: E402

MT = ZoneInfo("America/Denver")


def at(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=MT)


def ms(dt):
    return dt.timestamp() * 1000.0


def game(sport, start_dt, price="market", teams=("A", "B"), tier=3, label="A ML"):
    return {"sport": sport, "hA": teams[0], "awA": teams[1], "startMs": ms(start_dt), "mcSummary": None, "best": None,
            "markets": [{"label": label, "side": "mlFav", "prob": 0.7, "evVal": 0.1, "dec": 1.8, "ml": "-125", "tierN": tier,
                         "priceSource": price, "hkLane": False}]}


class Horizon(unittest.TestCase):
    def test_after_midnight_run_covers_the_games_about_to_start(self):
        # the 10 PM MT slot that GitHub runs at 4:30 AM MT: tomorrow-only would be 10-04 and skip SHL's 7:15 AM game on 10-03
        self.assertEqual(A.horizon_dates(at(2026, 10, 3, 4, 30)), ["2026-10-03", "2026-10-04"])

    def test_before_midnight_run(self):
        self.assertEqual(A.horizon_dates(at(2026, 10, 2, 19, 30)), ["2026-10-02", "2026-10-03"])

    def test_midnight_boundary(self):
        self.assertEqual(A.horizon_dates(at(2026, 10, 2, 23, 59))[0], "2026-10-02")
        self.assertEqual(A.horizon_dates(at(2026, 10, 3, 0, 1))[0], "2026-10-03")

    def test_mt_date_of_start(self):
        self.assertEqual(A.mt_date_of_ms(ms(at(2026, 10, 3, 7, 15))), "2026-10-03")
        self.assertEqual(A.mt_date_of_ms(ms(at(2026, 10, 3, 23, 30))), "2026-10-03")  # 05:30Z next day, still the 3rd in MT
        self.assertIsNone(A.mt_date_of_ms(None))
        self.assertIsNone(A.mt_date_of_ms("junk"))


class Qualifying(unittest.TestCase):
    def test_each_leg_carries_its_own_lock_date_and_guard_applies(self):
        now = at(2026, 10, 3, 5, 30)
        res = {"gameLegs": [game("SHL", at(2026, 10, 3, 7, 15), teams=("S1", "S2")),
                            game("EXTRALIGA", at(2026, 10, 4, 8, 0), teams=("E1", "E2")),
                            game("LIIGA", at(2026, 10, 3, 5, 0), teams=("L1", "L2"))]}  # started 30 min ago
        g: dict = {}
        q = A.build_qualifying(res, only_sports=A.EARLY_HOCKEY_SPORTS, now=now, guard_stats=g)
        self.assertEqual({(x["sport"], x["lockDate"]) for x in q}, {("SHL", "2026-10-03"), ("EXTRALIGA", "2026-10-04")})
        self.assertEqual(g["skipped"], 1)
        self.assertEqual(g["detail"][0]["sport"], "LIIGA")
        self.assertIn("already started", g["detail"][0]["why"])

    def test_no_real_price_never_qualifies_for_hockey(self):
        res = {"gameLegs": [game("SHL", at(2026, 10, 3, 9), price="assumed")]}
        self.assertEqual(A.build_qualifying(res, only_sports=A.EARLY_HOCKEY_SPORTS, now=at(2026, 10, 3, 5)), [])


class Analysis(unittest.TestCase):
    def test_counts_and_missing_price(self):
        now = at(2026, 10, 3, 5, 30)
        res = {"gameLegs": [game("SHL", at(2026, 10, 3, 7, 15)), game("LIIGA", at(2026, 10, 3, 8), price="assumed", teams=("L1", "L2")),
                            game("NLA", at(2026, 10, 3, 4), teams=("N1", "N2"))]}
        a = A.analyze_games(res, A.EARLY_HOCKEY_SPORTS, now)
        self.assertEqual((a["games"], a["started"], a["upcoming"]), (3, 1, 2))
        self.assertEqual(a["nextStartMs"], ms(at(2026, 10, 3, 7, 15)))
        self.assertEqual(a["noPrice"], ["LIIGA L2 @ L1"])

    def test_non_hockey_games_never_count_as_missing_price(self):
        res = {"gameLegs": [game("CFB", at(2026, 10, 3, 10), price=None)]}
        self.assertEqual(A.analyze_games(res, None, at(2026, 10, 3, 5))["noPrice"], [])


class ZeroPick(unittest.TestCase):
    def test_late_pass_does_not_email_subscribers_no_picks(self):
        ok, why = A._zero_pick_decision([], {"skipped": 3}, True)
        self.assertFalse(ok)
        self.assertIn("already started", why)

    def test_incomplete_pass_does_not_email(self):
        ok, _ = A._zero_pick_decision([], {"skipped": 0}, False)
        self.assertFalse(ok)

    def test_complete_pass_with_nothing_qualifying_still_emails_as_before(self):
        ok, _ = A._zero_pick_decision([], {"skipped": 0}, True)
        self.assertTrue(ok)


class OwnerAlert(unittest.TestCase):
    def rep(self, **kw):
        r = {"live": True, "label": "HOCKEY", "skippedDetail": [], "failedLabels": []}
        r.update(kw)
        return r

    def test_none_when_nothing_unlocked(self):
        self.assertIsNone(A.owner_alert_for([self.rep()]))
        self.assertIsNone(A.owner_alert_for([self.rep(live=False, skippedDetail=[{"sport": "SHL"}])]))  # dry run never alerts

    def test_alert_lists_legs_and_hash_is_stable(self):
        d = {"sport": "SHL", "game": "B @ A", "leg": "A ML", "startMs": ms(at(2026, 10, 3, 7, 15)), "why": "game already started 12m ago",
             "prob": 0.71, "tier": "PREMIUM"}
        a1 = A.owner_alert_for([self.rep(skippedDetail=[d])])
        a2 = A.owner_alert_for([self.rep(skippedDetail=[dict(d)])])
        self.assertIsNotNone(a1)
        self.assertEqual(a1[0], a2[0])
        self.assertIn("SHL", a1[2])
        self.assertIn("A ML", a1[2])
        self.assertIn("late-manual", a1[2])
        self.assertIn("not locked before kickoff", a1[1])
        d2 = dict(d, leg="A -1.5")
        self.assertNotEqual(a1[0], A.owner_alert_for([self.rep(skippedDetail=[d2])])[0])

    def test_failed_locks_alone_raise_an_alert(self):
        a = A.owner_alert_for([self.rep(failedLabels=["A ML"])])
        self.assertIsNotNone(a)
        self.assertIn("LOCK FAILED", a[2])

    def test_alert_goes_to_owner_only(self):
        sent = []
        orig = A._send_gmail
        A._send_gmail = lambda subject, to, html, **k: (sent.append(to) or (True, "x"))
        A.OWNER_EMAIL, A.LOCKS_EMAIL_TO = "owner@example.invalid", "owner@example.invalid"
        try:
            self.assertTrue(A.send_owner_alert("s", "<p>x</p>"))
        finally:
            A._send_gmail = orig
        self.assertEqual(sent, ["owner@example.invalid"])


class EngineHealthEntries(unittest.TestCase):
    def test_entry_flags_late_pass_and_computes_margin(self):
        nxt = ms(datetime.now(timezone.utc) + timedelta(minutes=90))
        e = A.pass_entries("lastHockeyEveningLock", [{"kind": "hockey-evening", "label": "HOCKEY", "dates": ["2026-10-03"], "live": True,
                                                       "complete": True, "qualifying": 4, "new": 4, "skipped": 0, "games": 8, "started": 0,
                                                       "upcoming": 8, "nextStartMs": nxt, "noPrice": []}])[0]
        self.assertFalse(e["late"])
        self.assertAlmostEqual(e["marginMin"], 90, delta=2)
        late = A.pass_entries("lastLock", [{"kind": "main", "label": "ALL", "live": True, "skipped": 5, "nextStartMs": None}])[0]
        self.assertTrue(late["late"])
        self.assertIsNone(late["marginMin"])


class LockPrep(unittest.TestCase):
    def test_group_expansion_and_unknown_jobs(self):
        self.assertEqual(P.expand("hockey"), ["shl", "liiga", "nla", "extraliga"])
        self.assertEqual(P.expand("hockey,nhl,shl"), ["shl", "liiga", "nla", "extraliga", "nhl"])  # de-duplicated, order kept
        self.assertEqual(P.expand("nope"), [])

    def test_every_job_names_an_existing_script(self):
        for job, (argv, files) in P.JOBS.items():
            self.assertTrue((P.ROOT / argv[0]).exists(), job)
            self.assertTrue(files)


if __name__ == "__main__":
    unittest.main(verbosity=1)
