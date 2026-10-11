#!/usr/bin/env python3
"""2026-10-10 email follow-up: (1) ONE zero-pick rule for every product and lock path, (2) NO MISSING PICK EMAILS -- per-pick 'emailed' tracking + the catch-up.

Hermetic: no network, no repo access, no real mail, no Supabase, no browser. Every send is a fake, the tracking state's read/write boundary
(_emailed_fetch / _emailed_append) is replaced by an in-memory 'origin', and the ledger is a Python list behind a tiny fake page.

    python3 scripts/test_email_delta_20261010.py
"""
from __future__ import annotations

import copy
import json
import re
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import auto_lock_settle as a  # noqa: E402
import email_tracking as et  # noqa: E402

MT = ZoneInfo("America/Denver")


def utc_ms(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=timezone.utc).timestamp() * 1000.0


# A slate far in the future (2099) so "upcoming" never depends on the real clock; every other time is relative to it.
START = utc_ms(2099, 6, 15, 18, 0)                  # kickoff
START_DATE = a.mt_date_of_ms(START)                  # its Mountain date
TOMORROW_DATE = a.mt_date_of_ms(START + 86400000)
T_CUTOFF = START - 30 * 3600000                      # the tracking cutoff (first live run after the deploy)
T_LOCK = START - 20 * 3600000                        # picks locked after the cutoff, well before kickoff
NOW = START - 3 * 3600000                            # "now" for catch-up runs: 3 h before kickoff

PRODUCT_LEAGUE = {"nfl": ("NFL", "NFL", "NFL"), "cfb": ("CFB", "CFB", "CFB"), "nba": ("NBA", "NBA", "NBA"), "hockey": ("NHL", "NHL", "NHL"),
                  "soccer": ("SOC_PL", "PL", "PL")}     # product -> (lock-email sport tag, ledger sport, ledger league)


class Patch:
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


def leg(sport, label, hA="HOM", awA="AWY", start=START, tier=3, prob=0.7, side="mlFav"):
    return {"kind": "GAME", "sport": sport, "hA": hA, "awA": awA, "side": side, "label": label, "prob": prob, "ml": "-110", "dec": 1.91, "tierN": tier,
            "evVal": 0.05, "lane": False, "mcSummary": None, "best": None, "startMs": start, "lockDate": a.mt_date_of_ms(start)}


def game(sport, hA="HOM", awA="AWY", start=START):
    # a posted real price on every game: an upcoming hockey game WITHOUT one makes the pass incomplete (the odds have not posted), which suppresses a zero-pick email
    return {"sport": sport, "hA": hA, "awA": awA, "startMs": start, "markets": [{"label": "x", "tierN": 0, "priceSource": "market"}]}


def pick(pid, product="hockey", label="TOR ML", hA="TOR", awA="MTL", *, date=None, locked=T_LOCK, start=START, origin="manual", outcome="pending",
         reasoning="PICK: TOR ML — PREMIUM (70.0% win prob, EV +5.0%)", bt="ML", **kw):
    _tag, sport, league = PRODUCT_LEAGUE[product]
    p = {"id": pid, "date": date or START_DATE, "hA": hA, "awA": awA, "sport": sport, "league": league, "betType": bt, "betOn": label, "winProb": 0.7, "ml": "-110",
         "decOdds": 1.91, "wager": 0, "outcome": outcome, "lockedAt": locked, "source": "manual", "lockOrigin": origin}
    if start is not None:
        p.update({"startMs": start, "lockTiming": "pre" if locked < start else "late-manual"})
    if reasoning:
        p["reasoning"] = reasoning
    p.update(kw)
    return p


class FakePage:
    """Answers exactly the page.evaluate() calls the email code makes against the in-page ledger (getP())."""
    def __init__(self, ledger):
        self.ledger = ledger

    def evaluate(self, js, arg=None):
        if "a.dates.includes(p.date)" in js:                                   # run_email_catchup's pull of owed-candidate picks
            return copy.deepcopy([p for p in self.ledger if p.get("id") and p.get("outcome") == "pending" and p.get("date") in arg["dates"]
                                  and (p.get("lockedAt") or 0) >= arg["cutoff"]])
        if "ds.includes(p.date) && p.outcome !== '_removed'" in js:           # ledger_locked_dates
            return copy.deepcopy([p for p in self.ledger if p.get("date") in arg and p.get("outcome") != "_removed"])
        raise AssertionError("unexpected page.evaluate: " + js.strip()[:90])


class Env:
    """Patches every side effect: mail, recipients, the repo-backed tracking state ('origin' lives in self.remote), the ledger, the lock call."""
    LISTS = {p: [f"owner@x.com", f"{p}-sub@x.com"] for p in ("nfl", "cfb", "nba", "hockey", "soccer")}

    def __init__(self, ledger=None, legs=(), games=(), send_result=(True, "sent")):
        self.ledger = ledger if ledger is not None else []
        self.legs, self.games = list(legs), list(games)
        self.sent = []                                 # (subject, to, html)
        self.send_result = send_result
        self.remote_text = None                        # data/emailed_picks.jsonl on 'origin' (append-only text)
        self.flushes = []
        self.locked_now = []                           # ids locked by the fake lock call
        self.page = FakePage(self.ledger)
        self.patch = None

    # --- fakes -------------------------------------------------------------------------------------------------------------
    def _send(self, subject, to, html):
        self.sent.append((subject, list(to), html))
        return self.send_result

    def _fetch(self):
        return (None, "absent") if self.remote_text is None else (self.remote_text, "ok")

    def _append(self, records, message):
        self.remote_text = (self.remote_text or "") + et.render(records)
        self.flushes.append(message)

    def inject(self, records):
        """Another workflow appended these records to origin meanwhile."""
        self.remote_text = (self.remote_text or "") + et.render(records)

    @property
    def remote(self):
        return None if self.remote_text is None else et.parse_lines(self.remote_text)

    def _lock(self, page, q, date_override=None, now=None):
        date = q.get("lockDate") or date_override
        pid = f"{date}_{q['hA']}_{q['awA']}_{q['sport']}_{q['label'].replace(' ', '')}"
        if any(p["id"] == pid for p in self.ledger):
            return "already-locked"
        _t, sport, league = next(v for k, v in PRODUCT_LEAGUE.items() if v[0] == q["sport"]) if q["sport"] in {v[0] for v in PRODUCT_LEAGUE.values()} else (q["sport"], q["sport"], q["sport"])
        self.ledger.append({"id": pid, "date": date, "hA": q["hA"], "awA": q["awA"], "sport": sport, "league": league, "betType": "ML", "betOn": q["label"],
                            "winProb": q["prob"], "ml": "-110", "decOdds": 1.91, "wager": 0, "outcome": "pending", "lockedAt": T_LOCK + len(self.locked_now),
                            "lockOrigin": "auto", "lockTiming": "pre", "startMs": q["startMs"],
                            "reasoning": f"PICK: {q['label']} — PREMIUM (70.0% win prob, EV +5.0%)"})
        self.locked_now.append(pid)
        return "locked"

    def _find(self, page, q, date_key):
        date = q.get("lockDate") or date_key
        pid = f"{date}_{q['hA']}_{q['awA']}_{q['sport']}_{q['label'].replace(' ', '')}"
        row = next((p for p in self.ledger if p["id"] == pid), None)
        return {"id": row["id"], "date": row["date"], "lockedAt": row["lockedAt"]} if row else None

    def _build(self, result, only_sports=None, now=None, guard_stats=None, **k):
        if guard_stats is not None:
            guard_stats.update({"skipped": 0, "games": [], "unguarded": 0, "detail": []})
        return [l for l in self.legs if only_sports is None or l["sport"] in only_sports]

    def __enter__(self):
        self.patch = Patch(
            (a, "_send_gmail", self._send), (a, "OWNER_EMAIL", "owner@x.com"), (a, "recipients_for", lambda product: list(self.LISTS[product])),
            (a, "_emailed_fetch", self._fetch), (a, "_emailed_append", self._append), (a, "lock_game_leg", self._lock), (a, "find_leg_pick", self._find),
            (a, "build_qualifying", self._build), (a, "gather_legs", lambda page: {"gameLegs": list(self.games), "propLegs": []}),
            (a, "gather_cfb_legs_for_dates", lambda page, d: {"gameLegs": [g for g in self.games if g["sport"] == "CFB"]}),
            (a, "gather_soccer_legs_for_dates", lambda page, d: {"gameLegs": [g for g in self.games if g["sport"].startswith("SOC_")]}),
            (a, "gather_hockey_legs_for_dates", lambda page, d: {"gameLegs": [g for g in self.games if g["sport"] in a.EARLY_HOCKEY_SPORTS]}),
            (a, "flush_to_supabase", lambda page: None), (a, "_nba_preseason_pairs", lambda: set()), (a, "PASS_REPORTS", []), (a, "EMAIL_FAILURES", []),
            (a, "PENDING_PERSIST", False), (a, "LINEUP_CTX", None))
        self.patch.__enter__()
        a.reset_email_tracking()
        a._PENDING_SEEN.clear()
        return self

    def __exit__(self, *exc):
        self.patch.__exit__()
        a.reset_email_tracking()
        a.EMAILED_PERSIST = False
        return False

    # --- drivers -------------------------------------------------------------------------------------------------------------
    def init(self, cutoff=T_CUTOFF):
        a.init_email_tracking(True, now=cutoff)

    def products(self):
        """Which product's subscriber list each sent email went to (identified by the recipient list), in send order."""
        by_list = {tuple(v): k for k, v in self.LISTS.items()}
        return [by_list.get(tuple(s[1]), "?") for s in self.sent]

    def to_of(self, i=-1):
        return self.sent[i][1]

    def catchup(self, now=NOW):
        return a.run_email_catchup(self.page, True, now=now)

    def main_run(self, send_email=True, only_new=False):
        a.run_lock_segmented(self.page, True, send_email=send_email, only_new=only_new)

    def rolling(self, which="cfb", send_email=True, now=NOW):
        fn = {"cfb": a.run_cfb_evening_lock, "soccer": a.run_soccer_evening_lock, "hockey": a.run_hockey_evening_lock}[which]
        return fn(self.page, True, send_email=send_email, to=list(self.LISTS[which]), now=now)


# ═════════════════════════════════════════════ 1. ZERO-PICK RULE ═════════════════════════════════════════════
class ZeroPickRule(unittest.TestCase):
    """Identical for every product and path: ONE 'no picks' email per product per slate, only when the product HAS (upcoming, non-preseason) games and nothing
    qualified; nothing on no-game days; never after a picks email for the same slate."""

    def test_mode_constant_exists_and_defaults_to_games(self):
        self.assertEqual(a.ZERO_PICK_EMAIL_MODE, "games")

    # --- main run: NFL / NBA / NHL -----------------------------------------------------------------------------------
    def test_main_run_no_games_sends_nothing_for_any_product(self):
        with Env() as e:
            e.init()
            e.main_run()
            self.assertEqual(e.sent, [])

    def test_main_run_games_and_nothing_qualified_sends_one_per_product_with_games(self):
        with Env(games=[game("NFL", "KC", "BUF"), game("NBA", "LAL", "DEN"), game("NHL", "TOR", "MTL")]) as e:
            e.init()
            e.main_run()
            self.assertEqual(sorted(e.products()), ["hockey", "nba", "nfl"])      # each to its own product's list, never another product's
            for subject, to, html in e.sent:
                self.assertIn("No ", subject)
                self.assertIn("picks cleared the bar for", html)
            self.assertTrue(any("No NHL picks" in s[0] for s in e.sent))          # the hockey main email names the league it covers

    def test_main_run_each_zero_goes_only_to_its_own_products_subscribers(self):
        with Env(games=[game("NFL", "KC", "BUF")]) as e:
            e.init()
            e.main_run()
            self.assertEqual(len(e.sent), 1)
            self.assertEqual(e.to_of(), Env.LISTS["nfl"])

    def test_nba_preseason_game_is_not_a_slate(self):
        with Env(games=[game("NBA", "LAL", "DEN")]) as e, Patch((a, "_nba_preseason_pairs", lambda: {frozenset({"LAL", "DEN"})})):
            e.init()
            e.main_run()
            self.assertEqual(e.sent, [])                                       # preseason: nothing is done for it

    def test_a_game_that_already_started_is_not_an_upcoming_slate(self):
        with Env(games=[game("NFL", "KC", "BUF", start=utc_ms(2020, 1, 1, 12))]) as e:
            e.init()
            e.main_run()
            self.assertEqual(e.sent, [])

    def test_second_main_pass_does_not_repeat_the_zero_email(self):
        with Env(games=[game("NFL", "KC", "BUF")]) as e:
            e.init()
            e.main_run()
            e.main_run()
            e.main_run(send_email=True)
            self.assertEqual(len(e.sent), 1)                                    # at most one per product per slate
            self.assertIn(f"nfl|{START_DATE}", e.remote["zero"])               # the marker is in the committed state

    def test_zero_email_is_not_sent_by_a_watchdog_or_non_final_pass(self):
        with Env(games=[game("NFL", "KC", "BUF")]) as e:
            e.init()
            e.main_run(only_new=True)                                           # the watchdog's auto-lock call
            e.main_run(send_email=False)                                        # a non-final slot
            self.assertEqual(e.sent, [])

    def test_never_after_a_picks_email_for_the_same_slate(self):
        with Env(games=[game("NHL", "TOR", "MTL")], legs=[leg("NHL", "TOR ML", "TOR", "MTL")]) as e:
            e.init()
            e.main_run()                                                        # picks email (records the pick id + date)
            self.assertEqual(e.products(), ["hockey"])
            e.legs.clear()                                                      # the model drifts: nothing qualifies any more
            e.main_run()
            self.assertEqual(len(e.sent), 1)                                    # and NO 'no picks' email follows the picks email

    def test_never_when_the_ledger_already_holds_a_locked_pick_for_the_slate(self):
        with Env(games=[game("NFL", "KC", "BUF")], ledger=[pick("m1", "nfl", "KC ML", "KC", "BUF")]) as e:
            e.init()
            e.main_run()
            self.assertEqual(e.sent, [])                                        # an (as yet unemailed) manual pick exists: the catch-up tells subscribers, not 'no picks'

    def test_modes_always_and_never(self):
        with Env(games=[]) as e, Patch((a, "ZERO_PICK_EMAIL_MODE", "always")):
            e.init()
            e.main_run()
            e.main_run()
            self.assertEqual(sorted(e.products()), ["hockey", "nba", "nfl"])      # 'always': even with no games (the old NFL behaviour) -- but still ONE per product per day
        with Env(games=[game("NFL", "KC", "BUF")]) as e, Patch((a, "ZERO_PICK_EMAIL_MODE", "never")):
            e.init()
            e.main_run()
            self.assertEqual(e.sent, [])

    def test_an_unknown_mode_value_behaves_like_games_not_always(self):
        with Env() as e, Patch((a, "ZERO_PICK_EMAIL_MODE", "alwayz")):
            e.init()
            e.main_run()
            self.assertEqual(e.sent, [])

    def test_always_mode_sends_on_a_no_game_day_once(self):
        # 'always' = the pre-2026-10-10 behaviour (an email even with no games), still de-duplicated
        with Env() as e, Patch((a, "ZERO_PICK_EMAIL_MODE", "always")):
            e.init()
            ok, why, dates = a.zero_pick_gate("nfl", qualifying=[], guard={"skipped": 0}, complete=True, game_dates=[], eval_dates=["2099-06-15"], page=e.page)
            self.assertTrue(ok)
            self.assertEqual(dates, ["2099-06-15"])
            a._emailed_update(lambda st: et.mark_zero(st, "nfl", dates, "x", a._now_ms()), "t")
            self.assertFalse(a.zero_pick_gate("nfl", qualifying=[], guard={"skipped": 0}, complete=True, game_dates=[], eval_dates=["2099-06-15"], page=e.page)[0])

    # --- the dedicated passes -----------------------------------------------------------------------------------------
    def test_cfb_evening_pass_no_games_silent_games_one_email(self):
        with Env() as e:
            e.init()
            e.rolling("cfb")
            self.assertEqual(e.sent, [])
        with Env(games=[game("CFB", "ARMY", "NAVY")]) as e:
            e.init()
            e.rolling("cfb")
            e.rolling("cfb")
            self.assertEqual(len(e.sent), 1)
            self.assertEqual(e.to_of(), Env.LISTS["cfb"])

    def test_soccer_and_hockey_evening_passes(self):
        for which, sport, product in (("soccer", "SOC_PL", "soccer"), ("hockey", "SHL", "hockey")):
            with Env() as e:
                e.init()
                e.rolling(which)
                self.assertEqual(e.sent, [], which)
            with Env(games=[game(sport, "A", "B")]) as e:
                e.init()
                e.rolling(which)
                e.rolling(which)
                self.assertEqual(len(e.sent), 1, which)
                self.assertEqual(e.to_of(), Env.LISTS[product])

    def test_soccer_no_picks_email_never_follows_a_soccer_picks_email(self):
        # the audit saw soccer send 'No picks' 9 h after a picks email
        with Env(games=[game("SOC_PL", "ARS", "CHE")], legs=[leg("SOC_PL", "ARS ML", "ARS", "CHE")]) as e:
            e.init()
            e.rolling("soccer")
            self.assertEqual(len(e.sent), 1)
            e.legs.clear()
            e.rolling("soccer")
            e.rolling("soccer", now=NOW + 3600000)
            self.assertEqual(len(e.sent), 1)

    def test_euro_early_pass_per_product(self):
        def run(e):
            a.run_euro_early_lock(e.page, True, send_email=True)
        with Env() as e:
            e.init()
            run(e)
            self.assertEqual(e.sent, [])                                        # no games for either product
        with Env(games=[game("SOC_PL", "ARS", "CHE"), game("LIIGA", "A", "B")]) as e:
            e.init()
            run(e)
            run(e)
            self.assertEqual(sorted(s[1][-1] for s in e.sent), ["hockey-sub@x.com", "soccer-sub@x.com"])   # one each, to its own list
            self.assertEqual(len(e.sent), 2)

    def test_early_single_product_pass_run_lock(self):
        with Env() as e:
            e.init()
            a.run_lock(e.page, True, only_sports=frozenset({"CFB"}), label="CFB", to=list(Env.LISTS["cfb"]), send_email=True)
            self.assertEqual(e.sent, [])
        with Env(games=[game("CFB", "ARMY", "NAVY")]) as e:
            e.init()
            for _ in range(2):
                a.run_lock(e.page, True, only_sports=frozenset({"CFB"}), label="CFB", to=list(Env.LISTS["cfb"]), send_email=True)
            self.assertEqual(len(e.sent), 1)

    def test_late_or_incomplete_pass_is_still_not_a_no_picks_email(self):
        # the older completeness rules still apply on top of the new one
        with Env() as e:
            e.init()
            self.assertFalse(a.zero_pick_gate("cfb", qualifying=[], guard={"skipped": 2}, complete=True, game_dates=[START_DATE], eval_dates=[START_DATE])[0])
            self.assertFalse(a.zero_pick_gate("cfb", qualifying=[], guard={"skipped": 0}, complete=False, game_dates=[START_DATE], eval_dates=[START_DATE])[0])
            self.assertTrue(a.zero_pick_gate("cfb", qualifying=[], guard={"skipped": 0}, complete=True, game_dates=[START_DATE], eval_dates=[START_DATE])[0])

    # --- wording ------------------------------------------------------------------------------------------------------
    def test_wording_names_the_slate_not_a_bare_today(self):
        today = datetime.now(MT).strftime("%Y-%m-%d")
        tomorrow = a.mt_date_of_ms(a._now_ms() + 86400000)
        html_t = a.build_locks_email_html([], True, 0, zero={"dates": [today], "scope": "NHL"})
        self.assertIn("No NHL picks cleared the bar for today (", html_t)
        html_n = a.build_locks_email_html([], True, 0, zero={"dates": [tomorrow]})
        self.assertIn("cleared the bar for tomorrow (", html_n)
        self.assertNotIn("bar today", html_n)
        html_b = a.build_locks_email_html([], True, 0, zero={"dates": [today, tomorrow]})
        self.assertIn("today (", html_b)
        self.assertIn("and tomorrow (", html_b)
        self.assertIn("this slate", a.build_locks_email_html([], True, 0))
        for h in (html_t, html_n, html_b):
            self.assertNotIn("undefined", h)
            self.assertNotIn("None", h)

    def test_zero_email_subject_is_about_the_slate(self):
        with Env(games=[game("CFB", "ARMY", "NAVY")]) as e:
            e.init()
            e.rolling("cfb")
            self.assertIn("No CFB picks cleared the bar for", e.sent[0][0])
            self.assertNotIn("(0)", e.sent[0][0])


# ═════════════════════════════════════════════ 2. TRACKING: recorded only after the send reached recipients ═════════════════════════════════════════════
class Tracking(unittest.TestCase):
    def send(self, e, qualifying, **kw):
        return a.send_locks_email(qualifying, True, len(qualifying), label="HOCKEY", to=["a@x.com"], product="hockey", **kw)

    def stamped(self, pid="p1", date=START_DATE, locked=T_LOCK):
        q = leg("NHL", "TOR ML", "TOR", "MTL")
        q.update(pickId=pid, pickDate=date, pickLockedAt=locked)
        return q

    def test_state_is_recorded_after_a_successful_send_and_committed(self):
        with Env() as e:
            e.init()
            self.assertTrue(self.send(e, [self.stamped()]))
            self.assertIn("p1", e.remote["emailed"])
            self.assertEqual(e.remote["emailed"]["p1"]["p"], "hockey")
            self.assertEqual(e.remote["emailed"]["p1"]["d"], START_DATE)
            self.assertIn("recorded", e.flushes[-1])

    def test_a_send_that_reached_nobody_records_nothing_and_blocks_retry_in_this_run(self):
        with Env(send_result=(False, "SMTPServerDisconnected")) as e:
            e.init()
            self.assertFalse(self.send(e, [self.stamped()]))
            self.assertNotIn("p1", e.remote["emailed"])
            self.assertIn("hockey", a._FAILED_SEND_PRODUCTS)

    def test_a_partial_send_counts_as_sent_a_retry_would_double_deliver(self):
        with Env(send_result=(False, "partial send -- 1/2 delivered; failures: 1 recipient(s) refused by the mail server: bad@x.com")) as e:
            e.init()
            self.send(e, [self.stamped()])
            self.assertIn("p1", e.remote["emailed"])

    def test_a_dry_run_never_records(self):
        with Env() as e:
            e.init()
            a.send_locks_email([self.stamped()], False, 1, label="HOCKEY", to=["a@x.com"], product="hockey")
            self.assertNotIn("p1", e.remote["emailed"])

    def test_untracked_legs_without_a_product_record_nothing(self):
        with Env() as e:
            e.init()
            a.send_locks_email([self.stamped()], True, 1, label="HOCKEY", to=["a@x.com"])
            self.assertEqual(e.remote["emailed"], {})

    def test_pick_email_due_uses_the_state_not_the_new_count(self):
        with Env() as e:
            e.init()
            q = self.stamped()
            self.assertEqual(a._pick_email_due([q], "HOCKEY", set(), False)[0], True)       # owed although nothing was locked 'new' in this pass
            self.send(e, [q])
            self.assertEqual(a._pick_email_due([q], "HOCKEY", set(), True)[0], False)       # emailed: not due even if 'new' says otherwise
            old = self.stamped("p0", locked=T_CUTOFF - 1000)
            self.assertFalse(a._pick_email_due([old], "HOCKEY", set(), True)[0])            # locked before the cutoff: the old code handled it

    def test_without_tracking_the_old_rule_decides(self):
        a.reset_email_tracking()
        self.assertEqual(a._pick_email_due([self.stamped()], "K", set(), True)[0], True)
        self.assertEqual(a._pick_email_due([self.stamped()], "K", set(), False)[0], False)

    def test_a_pending_failed_send_is_cleared_once_the_picks_were_emailed_by_the_catch_up(self):
        cleared = []
        with Env() as e, Patch((a, "clear_email_pending", lambda k: cleared.append(k))):
            e.init()
            q = self.stamped()
            self.send(e, [q])
            self.assertFalse(a._pick_email_due([q], "MAIN HOCKEY", {"MAIN HOCKEY"}, True)[0])
            self.assertEqual(cleared, ["MAIN HOCKEY"])

    def test_two_workflows_merge_their_records(self):
        with Env() as e:
            e.init()
            e.inject([{"k": "email", "id": "other", "p": "cfb", "d": START_DATE, "t": "2099-01-01T00:00:00Z", "v": "CFB"}])   # another workflow appended this meanwhile
            q_other = self.stamped("other")
            self.assertFalse(a._pick_email_due([q_other], "K", set(), True)[0])           # this process learns of it BEFORE deciding (refresh from origin)
            self.send(e, [self.stamped()])
            self.assertIn("other", e.remote["emailed"])
            self.assertIn("p1", e.remote["emailed"])
            self.assertEqual(e.remote_text.count('"id":"p1"'), 1)                        # only the NEW record is appended, never a rewrite of the file

    def test_the_file_is_append_only_jsonl_and_tolerates_damage(self):
        text = ('{"k":"cutoff","ms":1800000000000}\n{"k":"cutoff","ms":1790000000000}\nnot json\n[1,2]\n'
                '{"k":"email","id":"a","p":"nfl","d":"2099-01-01","t":"2099-01-01T00:00:00Z","v":"x"}\n\n'
                '{"k":"zero","p":"nba","d":"2099-01-01","t":"2099-01-01T00:00:00Z","v":"y"}\n{"k":"email"}\n')
        st = et.parse_lines(text)
        self.assertEqual(st["cutoffMs"], 1790000000000)                                    # two racing first runs: the earlier cutoff wins
        self.assertEqual(list(st["emailed"]), ["a"])
        self.assertEqual(list(st["zero"]), ["nba|2099-01-01"])
        old = '{"k":"email","id":"old","p":"nfl","d":"2020-01-01","t":"2020-01-01T00:00:00Z","v":"x"}\n'
        self.assertEqual(et.parse_lines(old, now_ms=a._now_ms())["emailed"], {})            # expired records are ignored on load
        self.assertEqual(et.parse_lines(et.compact_text(st, 0)).get("emailed"), st["emailed"])
        self.assertIn("merge=union", (ROOT / ".gitattributes").read_text())               # concurrent appends merge instead of conflicting


# ═════════════════════════════════════════════ 3. SEED / CUTOFF ═════════════════════════════════════════════
class FirstRunCutoff(unittest.TestCase):
    def test_first_run_records_the_cutoff_and_history_is_never_re_sent(self):
        history = [pick("old1", "hockey", "TOR ML", locked=T_CUTOFF - 3600000), pick("old2", "cfb", "ARMY ML", "ARMY", "NAVY", locked=T_CUTOFF - 1000)]
        fresh = pick("new1", "hockey", "EDM ML", "EDM", "CGY", locked=T_CUTOFF + 60000)
        with Env(ledger=history + [fresh]) as e:
            self.assertIsNone(e.remote)
            a.init_email_tracking(True, now=T_CUTOFF)
            self.assertEqual(e.remote["cutoffMs"], int(T_CUTOFF))                    # recorded in the committed file, not guessed
            self.assertIn("cutoff", e.flushes[0])
            e.catchup()
            self.assertEqual(len(e.sent), 1)                                          # only the pick locked AFTER the cutoff
            self.assertIn("EDM ML", e.sent[0][2])
            for pid in ("old1", "old2"):
                self.assertNotIn(pid, e.remote["emailed"])
            self.assertNotIn("TOR ML", e.sent[0][2])

    def test_the_cutoff_never_moves_once_recorded(self):
        with Env() as e:
            a.init_email_tracking(True, now=T_CUTOFF)
            a.init_email_tracking(True, now=T_CUTOFF + 99999999)
            self.assertEqual(e.remote["cutoffMs"], int(T_CUTOFF))
            self.assertEqual(len(e.flushes), 1)

    def test_dry_run_has_no_tracking_and_no_catch_up(self):
        with Env(ledger=[pick("m1")]) as e:
            self.assertFalse(a.init_email_tracking(False))
            self.assertEqual(a.run_email_catchup(e.page, False, now=NOW), 0)
            self.assertEqual(e.sent, [])
            self.assertIsNone(e.remote)

    def test_an_unreadable_state_turns_tracking_off_instead_of_guessing(self):
        with Env(ledger=[pick("m1")]) as e, Patch((a, "_emailed_fetch", lambda: (None, "error"))):
            self.assertFalse(a.init_email_tracking(True, now=T_CUTOFF))
            self.assertEqual(a.run_email_catchup(e.page, True, now=NOW), 0)
            self.assertEqual(e.sent, [])


# ═════════════════════════════════════════════ 4. CATCH-UP ═════════════════════════════════════════════
class CatchUp(unittest.TestCase):
    def test_manual_in_app_lock_is_emailed_once_to_the_right_product_only(self):
        with Env(ledger=[pick("m1", "hockey", "TOR ML", origin="manual")]) as e:
            e.init()
            self.assertEqual(e.catchup(), 1)
            self.assertEqual(len(e.sent), 1)
            self.assertEqual(e.to_of(), Env.LISTS["hockey"])                         # owner + hockey subscribers, nobody else
            for other in ("nfl", "cfb", "nba", "soccer"):
                self.assertFalse(set(e.to_of()) & {f"{other}-sub@x.com"})
            self.assertIn("TOR ML", e.sent[0][2])
            self.assertIn("m1", e.remote["emailed"])
            self.assertEqual(e.catchup(), 0)                                          # second pass: not re-sent
            self.assertEqual(len(e.sent), 1)

    def test_every_product_goes_to_its_own_list_and_a_product_gets_one_grouped_email(self):
        ledger = [pick("h1", "hockey", "TOR ML"), pick("h2", "hockey", "EDM ML", "EDM", "CGY"), pick("n1", "nfl", "KC ML", "KC", "BUF"),
                  pick("c1", "cfb", "ARMY ML", "ARMY", "NAVY"), pick("b1", "nba", "LAL ML", "LAL", "DEN"), pick("s1", "soccer", "ARS ML", "ARS", "CHE")]
        with Env(ledger=ledger) as e:
            e.init()
            self.assertEqual(e.catchup(), 5)                                          # 6 picks, 5 emails: the two hockey picks are ONE email
            self.assertEqual(len(e.sent), 5)
            by = {tuple(s[1]): s[2] for s in e.sent}
            for product in ("nfl", "cfb", "nba", "hockey", "soccer"):
                self.assertIn(tuple(Env.LISTS[product]), by)
            hockey_html = by[tuple(Env.LISTS["hockey"])]
            self.assertIn("TOR ML", hockey_html)
            self.assertIn("EDM ML", hockey_html)
            self.assertNotIn("KC ML", hockey_html)
            self.assertEqual(sum(1 for s in e.sent if s[1] == Env.LISTS["hockey"]), 1)

    def test_a_failed_send_is_retried_by_the_next_pass(self):
        with Env(ledger=[pick("m1")], send_result=(False, "SMTPServerDisconnected")) as e:
            e.init()
            self.assertEqual(e.catchup(), 0)
            self.assertNotIn("m1", e.remote["emailed"])
            self.assertEqual(len(a.EMAIL_FAILURES), 1)                                # the run ends red
            self.assertEqual(e.catchup(), 0)                                          # same process: not hammered again
            self.assertEqual(len(e.sent), 1)
            a._FAILED_SEND_PRODUCTS.clear()                                           # a later process
            e.send_result = (True, "sent")
            self.assertEqual(e.catchup(), 1)
            self.assertEqual(len(e.sent), 2)
            self.assertIn("m1", e.remote["emailed"])
            self.assertEqual(e.catchup(), 0)

    def test_game_that_began_within_the_window_is_emailed_with_a_note(self):
        began = START - 3600000                                                        # kicked off 1 h ago relative to NOW+... (see below)
        with Env(ledger=[pick("m1", start=began, locked=began - 3600000)]) as e:
            e.init()
            e.catchup(now=began + 3600000)                                             # 1 h into the game
            self.assertEqual(len(e.sent), 1)
            self.assertIn("already under way", e.sent[0][2])
            self.assertIn("Locked before kickoff", e.sent[0][2])

    def test_old_games_final_and_settled_picks_are_never_emailed(self):
        began = START - 3 * 3600000
        ledger = [pick("old", start=began, locked=began - 3600000),                                       # began 3 h ago: too late / over
                  pick("won", outcome="win"), pick("lost", outcome="loss"), pick("push", outcome="push"),  # settled
                  pick("final", hScore=3, aScore=2),                                                       # a stored final score
                  pick("removed", outcome="_removed")]
        with Env(ledger=ledger) as e:
            e.init()
            self.assertEqual(e.catchup(now=began + 3 * 3600000), 0)
            self.assertEqual(e.sent, [])

    def test_a_game_over_by_the_sports_length_is_excluded_even_inside_the_window(self):
        began = START - 3600000
        with Env(ledger=[pick("m1", "soccer", "ARS ML", "ARS", "CHE", start=began, locked=began - 3600000)]) as e:
            e.init()
            self.assertEqual(e.catchup(now=began + 2 * 3600000 + 60000), 0)           # 2 h 1 min in: past both the 2 h window and soccer's 120-minute game length
            self.assertEqual(e.sent, [])
        with Env(ledger=[pick("m1", "soccer", "ARS ML", "ARS", "CHE", start=began, locked=began - 3600000)]) as e:
            e.init()
            self.assertEqual(e.catchup(now=began + 110 * 60000), 1)                   # 1 h 50 min in: still inside the window

    def test_late_manual_locks_are_excluded_from_figures_and_never_emailed(self):
        late = pick("late", start=START, locked=START + 600000)                        # locked 10 min AFTER kickoff
        stamped = pick("late2", start=START, locked=START - 60000, lockTiming="late-manual")   # an explicit late-manual stamp
        nostart_late = pick("late3", start=None, locked=T_LOCK, lockTiming="late-manual")      # stamp with no recoverable start
        with Env(ledger=[late, stamped, nostart_late]) as e:
            e.init()
            self.assertEqual(e.catchup(now=START + 700000), 0)
            self.assertEqual(e.catchup(now=NOW), 0)
            self.assertEqual(e.sent, [])

    def test_a_pre_start_manual_lock_is_not_late(self):
        with Env(ledger=[pick("ok", start=START, locked=START - 3600000)]) as e:
            e.init()
            self.assertEqual(e.catchup(), 1)

    def test_unknown_start_only_for_a_game_dated_after_today(self):
        today_pick = pick("u1", start=None, date=START_DATE)
        later_pick = pick("u2", "nfl", "KC ML", "KC", "BUF", start=None, date=TOMORROW_DATE)
        with Env(ledger=[today_pick, later_pick]) as e:
            e.init()
            self.assertEqual(e.catchup(), 1)                                            # only the one that cannot have started
            self.assertIn("KC ML", e.sent[0][2])
            self.assertNotIn("TOR ML", e.sent[0][2])

    def test_retired_and_personal_leagues_and_props_are_never_emailed(self):
        junk = []
        for i, (sport, league) in enumerate([("MLS", "MLS"), ("SOCCER", "BUND"), ("MLB", "MLB"), ("WNBA", "WNBA"), ("NCAAH", "NCAAH"), ("KHL", "KHL"),
                                             ("ATP", "ATP"), ("WC", "WC"), ("CBB", "CBB")]):
            p = pick(f"j{i}")
            p.update(sport=sport, league=league)
            junk.append(p)
        junk.append(pick("prop1", "hockey", "Connor McDavid OVER SOG 3.5", bt="PROP", player="Connor McDavid"))
        junk.append(pick("prop2", "nfl", "Patrick Mahomes OVER 250.5", bt="PROP"))
        junk.append(pick("parlay", "nfl", "PARLAY", hA="PARLAY", bt="PARLAY"))
        with Env(ledger=junk) as e:
            e.init()
            self.assertEqual(e.catchup(), 0)
            self.assertEqual(e.sent, [])

    def test_product_map_covers_exactly_the_sold_leagues(self):
        sold = {"nfl": {"NFL"}, "cfb": {"CFB"}, "nba": {"NBA"}, "hockey": {"NHL", "SHL", "LIIGA", "NLA", "EXTRALIGA"}, "soccer": {"PL", "LIGA", "SERIEA", "CL"}}
        got: dict = {}
        for code, product in et.PRODUCT_OF_CODE.items():
            got.setdefault(product, set()).add(code)
        self.assertEqual(got, sold)
        # and it equals the lock passes' own PRODUCT_SPORTS, once the lock-email sport tags (SOC_PL ...) are translated to ledger codes
        code_of_tag = {tag: code for code, tag in et.TAG_OF_CODE.items()}
        for product, tags in a.PRODUCT_SPORTS.items():
            self.assertEqual({code_of_tag.get(t, t) for t in tags}, got[product], product)
        for code in ("MLS", "BUND", "BL", "MLB", "WNBA", "NCAAH", "KHL", "ATP", "WTA", "WC", "CBB", "TEN"):
            self.assertNotIn(code, et.PRODUCT_OF_CODE)

    def test_kill_switch(self):
        with Env(ledger=[pick("m1")]) as e, Patch((et, "CATCHUP_ENABLED", False)):
            e.init()
            self.assertEqual(e.catchup(), 0)
            self.assertEqual(e.sent, [])

    def test_an_nba_preseason_manual_lock_is_never_emailed(self):
        with Env(ledger=[pick("pre", "nba", "LAL ML", "LAL", "DEN"), pick("reg", "nba", "BOS ML", "BOS", "NYK")]) as e, \
                Patch((a, "_nba_preseason_pairs", lambda: {frozenset({"LAL", "DEN"})})):
            e.init()
            self.assertEqual(e.catchup(), 1)
            self.assertIn("BOS ML", e.sent[0][2])
            self.assertNotIn("LAL ML", e.sent[0][2])

    def test_dates_outside_yesterday_to_tomorrow_are_not_caught_up(self):
        far = a.mt_date_of_ms(START + 5 * 86400000)
        with Env(ledger=[pick("f1", date=far, start=START + 5 * 86400000)]) as e:
            e.init()
            self.assertEqual(e.catchup(), 0)

    def test_sanity_brake_refuses_a_flood_for_one_product(self):
        flood = [pick(f"x{i}", "nfl", f"T{i} ML", f"T{i}", f"U{i}") for i in range(et.CATCHUP_MAX_PICKS_PER_PRODUCT + 1)]
        with Env(ledger=flood) as e:
            e.init()
            self.assertEqual(e.catchup(), 0)
            self.assertEqual(e.sent, [])
            self.assertEqual(len(a.EMAIL_FAILURES), 1)                                  # visible (red run), never a silent drop

    def test_the_manual_only_qualified_knob(self):
        manual_skip = pick("m1", reasoning="PICK: TOR ML — SKIP (48.0% win prob, EV -6.0%)", winProb=0.48)
        auto_skip = pick("a1", "hockey", "EDM ML", "EDM", "CGY", origin="auto", reasoning="PICK: EDM ML — SKIP (80.0% win prob, EV +1.0%)", winProb=0.8)
        with Env(ledger=[manual_skip, auto_skip]) as e, Patch((et, "CATCHUP_MANUAL_ONLY_QUALIFIED", True)):
            e.init()
            self.assertEqual(e.catchup(), 1)
            self.assertNotIn("TOR ML", e.sent[0][2])
            self.assertIn("EDM ML", e.sent[0][2])
        with Env(ledger=[manual_skip]) as e:                                             # default: the owner's decision -- every manual lock, honestly graded
            e.init()
            self.assertEqual(e.catchup(), 1)
            self.assertIn("SKIP", e.sent[0][2])

    def test_catch_up_email_uses_the_standard_template_and_honest_badges(self):
        ledger = [pick("g1", reasoning="PICK: TOR ML — PREMIUM (70.0% win prob, EV +5.0%)"),
                  pick("g2", "hockey", "EDM -1.5", "EDM", "CGY", bt="SPREAD", reasoning="PICK: EDM -1.5 — MANUAL LINE (60.2% estimated win prob at this line)"),
                  pick("g3", "hockey", "OVER 6.5", "VAN", "SEA", bt="OU", reasoning=None)]
        with Env(ledger=ledger) as e:
            e.init()
            e.catchup()
            html = e.sent[0][2]
            self.assertIn("What the grades mean", html)                                   # the same template as the lock emails
            self.assertIn("PREMIUM", html)
            self.assertIn("MANUAL LINE", html)
            self.assertIn("LOCKED PICK", html)                                            # no grade on record: said plainly, no invented tier
            self.assertIn("NHL (3)", html)
            for bad in ("undefined", "NaN", "None", "MLS", "Bundesliga"):
                self.assertNotIn(bad, html)
            self.assertIn("Locked HOCKEY picks for", e.sent[0][0])
            self.assertIn("(3)", e.sent[0][0])

    def test_adjusted_line_pick_keeps_its_adjusted_line_presentation(self):
        p = pick("al", "cfb", "TENN -6.5", "ARK", "TENN", bt="SPREAD", reasoning="PICK: TENN -6.5 — ADJUSTED LINE (69.0% estimated win prob at this line)",
                 altLine={"line": -6.5, "shift": 7, "posted": -13.5, "postedLabel": "TENN -13.5"})
        with Env(ledger=[p]) as e:
            e.init()
            e.catchup()
            self.assertIn("ADJUSTED LINE", e.sent[0][2])
            self.assertIn("TENN -13.5", e.sent[0][2])

    def test_catch_up_pulls_nothing_extra_it_reads_the_in_page_ledger_only(self):
        seen = []

        class Page(FakePage):
            def evaluate(self, js, arg=None):
                seen.append(js)
                return super().evaluate(js, arg)
        with Env(ledger=[pick("m1")]) as e:
            e.init()
            a.run_email_catchup(Page(e.ledger), True, now=NOW)
            self.assertEqual(len(seen), 1)
            self.assertIn("getP()", seen[0])
            self.assertNotIn("fetch(", seen[0])
            self.assertNotIn("SUPABASE", seen[0])


# ═════════════════════════════════════════════ 5. NO DOUBLE SEND ACROSS PATHS ═════════════════════════════════════════════
class NoDoubleSend(unittest.TestCase):
    def test_main_run_safety_net_lock_reaches_cfb_once_then_every_other_path_is_silent(self):
        cfb = leg("CFB", "ARMY ML", "ARMY", "NAVY")
        with Env(legs=[cfb], games=[game("CFB", "ARMY", "NAVY")]) as e:
            e.init()
            e.main_run()                                                                  # locks CFB as a safety net, emails nobody itself
            self.assertEqual(e.sent, [])
            self.assertEqual(len(e.locked_now), 1)
            self.assertEqual(e.catchup(), 1)                                              # the end-of-run catch-up tells the CFB subscribers
            self.assertEqual(len(e.sent), 1)
            self.assertEqual(e.to_of(), Env.LISTS["cfb"])
            self.assertIn("ARMY ML", e.sent[0][2])
            e.rolling("cfb")                                                              # the dedicated evening pass finds it already locked AND emailed
            e.rolling("cfb", now=NOW + 600000)
            a.run_lock(e.page, True, only_sports=frozenset({"CFB"}), label="CFB", to=list(Env.LISTS["cfb"]), send_email=True)   # early pass
            e.main_run()
            self.assertEqual(e.catchup(), 0)
            self.assertEqual(len(e.sent), 1)                                              # exactly one email for that pick, ever

    def test_dedicated_pass_email_records_the_picks_so_the_catch_up_stays_silent(self):
        soc = leg("SOC_PL", "ARS ML", "ARS", "CHE")
        with Env(legs=[soc], games=[game("SOC_PL", "ARS", "CHE")]) as e:
            e.init()
            e.rolling("soccer")
            self.assertEqual(len(e.sent), 1)
            self.assertEqual(e.catchup(), 0)
            e.rolling("soccer", now=NOW + 60000)
            self.assertEqual(len(e.sent), 1)

    def test_main_final_email_watchdog_and_catch_up_never_double_send_a_pick(self):
        nhl = leg("NHL", "TOR ML", "TOR", "MTL")
        with Env(legs=[nhl], games=[game("NHL", "TOR", "MTL")]) as e:
            e.init()
            e.main_run(send_email=True)                                                   # the day's main email (final slot)
            self.assertEqual(e.products(), ["hockey"])
            e.main_run(send_email=True, only_new=True)                                    # watchdog auto-lock: nothing new
            e.main_run(send_email=False)                                                  # a later non-final slot
            e.main_run(send_email=True)                                                   # the backup slot re-running the final email
            self.assertEqual(e.catchup(), 0)
            self.assertEqual(len(e.sent), 1)

    def test_a_manual_lock_then_the_main_email_then_the_catch_up(self):
        manual = pick("m1", "hockey", "EDM ML", "EDM", "CGY")
        nhl = leg("NHL", "TOR ML", "TOR", "MTL")
        with Env(ledger=[manual], legs=[nhl], games=[game("NHL", "TOR", "MTL")]) as e:
            e.init()
            e.main_run(send_email=True)                                                   # lists the engine's NHL pick
            self.assertEqual(len(e.sent), 1)
            self.assertNotIn("EDM ML", e.sent[0][2])
            self.assertEqual(e.catchup(), 1)                                              # the manual pick still owed: its own email, to hockey only
            self.assertIn("EDM ML", e.sent[1][2])
            self.assertNotIn("TOR ML", e.sent[1][2])
            self.assertEqual(e.catchup(), 0)
            self.assertEqual(len(e.sent), 2)

    def test_owner_lock_now_run_that_does_not_email_is_caught_up(self):
        # a manual dispatch / the 13:07Z slot locks with send_email=False (the day's email already went out): the catch-up covers it
        nhl = leg("NHL", "TOR ML", "TOR", "MTL")
        with Env(legs=[nhl], games=[game("NHL", "TOR", "MTL")]) as e:
            e.init()
            e.main_run(send_email=False)
            self.assertEqual(e.sent, [])
            self.assertEqual(e.catchup(), 1)
            self.assertEqual(e.to_of(), Env.LISTS["hockey"])
            e.main_run(send_email=True)                                                   # the final email later: everything already emailed -> quiet
            self.assertEqual(len(e.sent), 1)

    def test_watchdog_auto_lock_runs_the_per_product_runner_and_the_catch_up_adds_nothing(self):
        shl = leg("SHL", "SHL A ML", "A", "B")
        with Env(legs=[shl], games=[game("SHL", "A", "B")]) as e:
            e.init()
            self.assertEqual(a._auto_lock(e.page, [{"sport": "SHL"}]), [])               # -> run_hockey_evening_lock: locks the leg and emails the hockey subscribers once
            self.assertEqual(e.products(), ["hockey"])
            self.assertEqual(e.catchup(), 0)
            self.assertEqual(a._auto_lock(e.page, [{"sport": "SHL"}]), [])               # a second sweep finds it locked and emailed
            self.assertEqual(len(e.sent), 1)


# ═════════════════════════════════════════════ 6. FILES / WORKFLOWS ═════════════════════════════════════════════
class Wiring(unittest.TestCase):
    def test_every_live_process_ends_with_the_catch_up_and_starts_tracking(self):
        src = (ROOT / "scripts" / "auto_lock_settle.py").read_text()
        self.assertIn("init_email_tracking(bool(args.live))", src)
        i, j = src.index("run_email_catchup(page, bool(args.live))"), src.index("browser.close()")
        self.assertLess(i, j)                                                              # inside the browser session, before it closes (the in-memory ledger)
        self.assertIn("\nEMAILED_PERSIST = False ", src)                                   # default for importers: tests / tools never touch the repo

    def test_catch_up_is_reached_by_settle_lock_and_watchdog_processes(self):
        src = (ROOT / "scripts" / "auto_lock_settle.py").read_text()
        main = src[src.index("def main()"):]
        catch = main.index("run_email_catchup")
        for needle in ("if args.watchdog:", "elif do_lock:", "if do_settle:"):
            self.assertLess(main.index(needle), catch)

    def test_watchdog_workflow_has_the_publish_catch_all_and_no_empty_blocks(self):
        wf = (ROOT / ".github" / "workflows" / "lock-watchdog.yml").read_text()
        self.assertIn("uses: ./.github/actions/publish", wf)
        tail = wf[wf.index("uses: ./.github/actions/publish"):]
        self.assertIn("dispatch: never", tail)                                             # keeps the 2026-10-03 decision: no Pages deploy per watchdog slot
        for m in re.finditer(r"(?m)^(\s*)(with|env):[ \t]*$", wf):                          # no empty `with:` / `env:` block (that mistake broke 7 workflows on 2026-10-10)
            nxt = wf[m.end():].split("\n", 2)[1]
            self.assertTrue(nxt.strip() and len(nxt) - len(nxt.lstrip()) > len(m.group(1)), f"empty {m.group(2)}: block in lock-watchdog.yml")

    def test_state_file_holds_no_addresses(self):
        with Env(ledger=[pick("m1")]) as e:
            e.init()
            e.catchup()
            self.assertTrue(e.remote_text)
            self.assertNotIn("@", e.remote_text)

    def test_tracking_module_is_pure(self):
        src = (ROOT / "scripts" / "email_tracking.py").read_text()
        for bad in ("subprocess", "requests", "smtplib", "import os"):
            self.assertNotIn(bad, src)


class WinProbInEmails(unittest.TestCase):
    """owner 2026-10-10: SKIP-graded manual locks may be emailed, but EVERY pick line must state its win probability"""

    def html_for(self, p):
        leg = et.pick_to_leg(p)
        self.assertIsNotNone(leg, "pick should convert to an email leg")
        return a.build_locks_email_html([leg], True)

    def test_a_skip_graded_manual_lock_shows_its_win_probability(self):
        p = pick("skip1", reasoning="PICK: TOR ML — SKIP (53.0% win prob, EV -4.0%)", winProb=0.53)
        h = self.html_for(p)
        self.assertIn("53% win prob", h)
        self.assertIn("SKIP", h)
        self.assertNotIn("0% win prob", h.replace("53% win prob", ""))

    def test_skip_picks_explain_skip_and_flag_a_high_win_probability(self):
        """owner 2026-10-11: SKIP / LEAN manual picks stay in the emails; a SKIP also says what SKIP means and notes the (high) win probability"""
        hi = self.html_for(pick("skiphi", reasoning="PICK: TOR ML — SKIP (78.0% win prob, EV -3.0%)", winProb=0.78))
        self.assertIn("Graded SKIP on value", hi)
        self.assertIn("78% win prob", hi)
        self.assertIn("HIGH win probability", hi)
        self.assertIn("no price edge", hi)
        lo = self.html_for(pick("skiplo", reasoning="PICK: TOR ML — SKIP (54.0% win prob, EV -6.0%)", winProb=0.54))
        self.assertIn("Graded SKIP on value", lo)
        self.assertIn("54%", lo)
        self.assertNotIn("HIGH win probability", lo)
        self.assertIn("Win probability not recorded", a._skip_note_html("SKIP", 0.0, False))        # a SKIP with truly no stored probability says so (never a made-up 0%)
        self.assertEqual(a._skip_note_html("LEAN", 0.7, True), "")                                   # only SKIP gets the explanation
        lean = self.html_for(pick("lean1", reasoning="PICK: TOR ML — LEAN (58.0% win prob, EV +1.0%)", winProb=0.58))
        self.assertIn("LEAN", lean)
        self.assertNotIn("Graded SKIP", lean)                                       # only SKIP gets the explanation; LEAN stays in the email as before
        prop = {"kind": "PROP", "leg": {"player": "A. Davis", "line": 24.5, "stat": "PTS", "over": True, "prob": 0.52, "grade": "SKIP"}, "sport": "NBA"}
        self.assertIn("Graded SKIP on value", a._leg_html(prop))

    def test_every_pick_line_in_the_standard_template_says_win_prob(self):
        h = self.html_for(pick("p1", winProb=0.7))
        self.assertIn("70% win prob", h)

    def test_missing_probability_is_never_printed_as_zero(self):
        p = pick("np1", reasoning="", winProb=None)
        p.pop("winProb", None)
        h = self.html_for(p)
        self.assertIn("win prob n/a", h)
        self.assertNotIn("— 0%", h)

    def test_probability_falls_back_to_other_fields_and_the_reasoning_text(self):
        self.assertAlmostEqual(et.win_prob({"prob": 0.61}), 0.61)
        self.assertAlmostEqual(et.win_prob({"modelProb": 64}), 0.64)
        self.assertAlmostEqual(et.win_prob({"conf": 58}), 0.58)
        self.assertAlmostEqual(et.win_prob({"reasoning": "PICK: X — LEAN (57.5% win prob, EV +1%)"}), 0.575)
        self.assertIsNone(et.win_prob({}))
        self.assertIsNone(et.win_prob({"winProb": 0}))

    def test_adjusted_line_and_prop_lines_state_it_too(self):
        leg = et.pick_to_leg(pick("adj1", label="OVER 5.5", bt="OU", altLine={"posted": "6.5", "postedLabel": "OVER 6.5", "flip": False}, winProb=0.66))
        self.assertIn("66% win prob", a.build_locks_email_html([leg], True))
        prop = {"kind": "PROP", "leg": {"player": "A. Davis", "line": 24.5, "stat": "PTS", "over": True, "prob": 0.61, "grade": "OPTIMAL"}, "sport": "NBA"}
        self.assertIn("61% win prob", a._leg_html(prop))


if __name__ == "__main__":
    unittest.main(verbosity=2)
