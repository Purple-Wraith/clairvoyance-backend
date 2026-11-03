"""Estimated money-in for the mailing list (_subscribers.payments_ledger / weekly_collected / analytics_summary).

Nothing records the real Venmo amounts, so a payment is INFERRED from the add / renew log: its worth is the marginal price on the ladder in force that day
(PRICE[paid lists after] - PRICE[paid lists already running]).  The owner's case (2026-11-03): someone paid $20 for NFL, then $15 to add NBA -- this week must read $15
(money in), while the active value reads $35 (what the two lists are worth per 30 days).  Free months are never payments; a repricing never rewrites old payments.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _subscribers as S  # noqa: E402

U = timezone.utc


def at(y, m, d, hh=12, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=U)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.events, self.subs = self.tmp / "events.json", self.tmp / "subs.json"
        self.subs.write_text(json.dumps({p: [] for p in S.PRODUCTS}))
        self.patches = [mock.patch.object(S, "EVENTS_FILE", self.events), mock.patch.object(S, "SUBSCRIBERS_FILE", self.subs)]
        for p in self.patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self.patches])

    def log(self, *evs):
        rows = [{"ts": ts.isoformat(), "action": action, "product": product, "email": email, **({"promo": True} if promo else {})} for ts, action, product, email, promo in evs]
        self.events.write_text(json.dumps(rows))
        return rows

    def ledger(self):
        return S.payments_ledger()


class Amounts(Base):
    def test_first_sport_is_twenty(self):
        self.log((at(2026, 11, 10), "add", "nfl", "a@x.com", False))
        (p,) = self.ledger()
        self.assertEqual((p["amount"], p["kind"], p["products"]), (20, "new", ["nfl"]))

    def test_the_owners_case_adding_a_second_sport_to_a_running_plan_is_fifteen(self):
        self.log((at(2026, 11, 10), "add", "nfl", "a@x.com", False), (at(2026, 11, 20), "add", "nba", "a@x.com", False))
        a, b = self.ledger()
        self.assertEqual((a["amount"], a["kind"]), (20, "new"))
        self.assertEqual((b["amount"], b["kind"], b["others"]), (15, "addon", ["nfl"]))          # $35 plan - $20 already running

    def test_each_payment_is_priced_with_the_ladder_of_its_own_day(self):
        self.log((at(2026, 10, 20), "add", "nfl", "a@x.com", False), (at(2026, 10, 25), "add", "nba", "a@x.com", False), (at(2026, 11, 4), "add", "cfb", "a@x.com", False))
        # 2nd sport on the old ladder: $30 - $20 = $10; the 3rd after the Nov 3 repricing: $50 - $35 = $15 (both earlier lists are still running on Nov 4)
        self.assertEqual([x["amount"] for x in self.ledger()], [20, 10, 15])
        self.assertEqual(S.pricing_at(at(2026, 11, 2, 12)), S.PRICING_BY_COUNT_BEFORE_20261103)
        self.assertEqual(S.pricing_at(at(2026, 11, 3, 8)), S.PRICING_BY_COUNT)

    def test_renewing_both_sports_at_once_is_one_thirty_five_payment_not_two(self):
        self.log((at(2026, 11, 10), "add", "nfl", "a@x.com", False), (at(2026, 11, 10, 12, 1), "add", "nba", "a@x.com", False),
                 (at(2026, 12, 5), "renew", "nfl", "a@x.com", False), (at(2026, 12, 5, 12, 2), "renew", "nba", "a@x.com", False))
        p = self.ledger()
        self.assertEqual([(x["amount"], x["kind"]) for x in p], [(35, "new"), (35, "renewal")])   # each pair is ONE payment
        self.assertEqual(p[1]["products"], ["nfl", "nba"])

    def test_renewing_one_sport_while_another_keeps_running_is_the_marginal_fifteen(self):
        self.log((at(2026, 11, 10), "add", "nfl", "a@x.com", False), (at(2026, 11, 20), "add", "nba", "a@x.com", False), (at(2026, 12, 8), "renew", "nfl", "a@x.com", False))
        self.assertEqual([x["amount"] for x in self.ledger()], [20, 15, 15])

    def test_a_free_month_is_never_a_payment_and_paying_after_it_is(self):
        self.log((at(2026, 11, 10), "add", "cfb", "a@x.com", True), (at(2026, 11, 15), "add", "cfb", "a@x.com", False))
        p = self.ledger()
        self.assertEqual([(x["amount"], x["kind"]) for x in p], [(20, "converted")])

    def test_a_free_month_on_one_list_does_not_discount_a_paid_one(self):
        self.log((at(2026, 11, 10), "add", "cfb", "a@x.com", True), (at(2026, 11, 12), "add", "nfl", "a@x.com", False))
        (p,) = self.ledger()
        self.assertEqual((p["amount"], p["kind"], p["others"]), (20, "new", []))                  # the free list is not "already running and paid"

    def test_coming_back_after_a_lapse_is_a_winback_at_full_first_sport_price(self):
        self.log((at(2026, 11, 10), "add", "nfl", "a@x.com", False), (at(2026, 12, 25), "renew", "nfl", "a@x.com", False))      # 45 days later: lapsed in between
        self.assertEqual([(x["amount"], x["kind"]) for x in self.ledger()], [(20, "new"), (20, "winback")])

    def test_a_returning_customer_adding_a_new_sport_with_nothing_running_is_a_winback(self):
        self.log((at(2026, 11, 10), "add", "nfl", "a@x.com", False), (at(2026, 12, 25), "add", "nba", "a@x.com", False))
        self.assertEqual([(x["amount"], x["kind"]) for x in self.ledger()], [(20, "new"), (20, "winback")])

    def test_removed_lists_no_longer_count_as_running(self):
        self.log((at(2026, 11, 10), "add", "nfl", "a@x.com", False), (at(2026, 11, 12), "remove", "nfl", "a@x.com", False), (at(2026, 11, 14), "add", "nba", "a@x.com", False))
        self.assertEqual([x["amount"] for x in self.ledger()], [20, 20])

    def test_emails_do_not_affect_each_other(self):
        self.log((at(2026, 11, 10), "add", "nfl", "a@x.com", False), (at(2026, 11, 11), "add", "nba", "b@x.com", False))
        self.assertEqual([(x["email"], x["amount"]) for x in self.ledger()], [("a@x.com", 20), ("b@x.com", 20)])

    def test_unknown_products_and_junk_rows_are_ignored(self):
        self.events.write_text(json.dumps([{"ts": "garbage", "action": "add", "product": "nfl", "email": "a@x.com"}, {"ts": at(2026, 11, 10).isoformat(), "action": "add", "product": "mlb", "email": "a@x.com"},
                                           {"ts": at(2026, 11, 10).isoformat(), "action": "referral_pending", "product": "", "email": "a@x.com"}]))
        self.assertEqual(self.ledger(), [])


class Weeks(Base):
    def test_weeks_run_monday_to_sunday_in_mountain_time(self):
        # Sunday 23:30 MT = Monday 06:30 UTC (MST is UTC-7): still the OLD week; Monday 00:10 MT = Monday 07:10 UTC: the new one
        self.log((at(2026, 11, 9, 6, 30), "add", "nfl", "a@x.com", False), (at(2026, 11, 9, 7, 10), "add", "nba", "b@x.com", False))
        a, b = self.ledger()
        self.assertEqual((a["week"], b["week"]), ("2026-11-02", "2026-11-09"))

    def test_the_owners_week_reads_fifteen_collected_and_thirty_five_active(self):
        self.log((at(2026, 10, 26, 2, 22), "add", "nfl", "a@x.com", False), (at(2026, 11, 3, 20, 0), "add", "nba", "a@x.com", False))
        self.subs.write_text(json.dumps({**{p: [] for p in S.PRODUCTS}, "nfl": [{"email": "a@x.com", "added": at(2026, 10, 26, 2, 22).isoformat()}], "nba": [{"email": "a@x.com", "added": at(2026, 11, 3, 20).isoformat()}]}))
        with mock.patch.object(S, "_now", return_value=at(2026, 11, 4, 18)):
            a = S.analytics_summary()
        wk = {w["week_start"]: w for w in a["weekly_collected"]}
        self.assertEqual((wk["2026-10-19"]["collected"], wk["2026-11-02"]["collected"]), (20, 15))      # Oct 25 evening MT lands in the Oct 19 week
        self.assertEqual((wk["2026-11-02"]["active_value"], wk["2026-11-02"]["current"]), (35, True))
        self.assertEqual((a["collected_this_week"], a["collected_last_week"], a["collected_total"], a["estimated_mrr"]), (15, 0, 35, 35))
        self.assertEqual(wk["2026-10-26"]["collected"], 0)                                                # a quiet week is a zero bar, not a gap
        self.assertEqual(wk["2026-11-02"]["by_kind"], {"addon": 15})

    def test_collected_windows(self):
        self.log((at(2026, 11, 1), "add", "nfl", "a@x.com", False), (at(2026, 11, 20), "add", "nba", "b@x.com", False))
        with mock.patch.object(S, "_now", return_value=at(2026, 11, 22)):
            a = S.analytics_summary()
        self.assertEqual((a["collected_7d"], a["collected_30d"], a["collected_total"], a["payments_30d"], a["payers_30d"]), (20, 40, 40, 2, 2))
        self.assertEqual([p["email"] for p in a["payments"]], ["b@x.com", "a@x.com"])                    # newest first

    def test_a_list_payment_split_across_lists_adds_up_to_the_payment(self):
        self.log((at(2026, 11, 10), "add", "nfl", "a@x.com", False), (at(2026, 11, 10, 12, 1), "add", "nba", "a@x.com", False))
        with mock.patch.object(S, "_now", return_value=at(2026, 11, 11)):
            w = S.weekly_collected(S.payments_ledger())
        self.assertEqual(w[-1]["collected"], 35)
        self.assertAlmostEqual(sum(w[-1]["by_product"].values()), 35)

    def test_no_events_gives_an_empty_but_valid_series(self):
        self.log()
        with mock.patch.object(S, "_now", return_value=at(2026, 11, 11)):
            a = S.analytics_summary()
        self.assertEqual((a["collected_this_week"], a["collected_total"], a["payments"]), (0, 0, []))


class AtRisk(Base):
    def test_expiring_30d_prices_what_lapsing_would_cost_and_free_months_cost_nothing(self):
        now = at(2026, 11, 20)
        self.subs.write_text(json.dumps({**{p: [] for p in S.PRODUCTS}, "nfl": [{"email": "a@x.com", "added": (now - timedelta(days=27)).isoformat()}],
                                         "nba": [{"email": "a@x.com", "added": (now - timedelta(days=5)).isoformat()}], "cfb": [{"email": "a@x.com", "added": (now - timedelta(days=2)).isoformat(), "promo": True}]}))
        with mock.patch.object(S, "_now", return_value=now):
            a = S.analytics_summary()
        by = {r["product"]: r for r in a["expiring_30d"]}
        self.assertEqual((by["nfl"]["at_risk"], by["nfl"]["days_left"]), (15, 3))                      # losing one of two paid lists: $35 -> $20
        self.assertEqual((by["cfb"]["at_risk"], by["cfb"]["promo"]), (0, True))
        self.assertEqual([r["days_left"] for r in a["expiring_30d"]], sorted(r["days_left"] for r in a["expiring_30d"]))


if __name__ == "__main__":
    unittest.main(verbosity=1)
