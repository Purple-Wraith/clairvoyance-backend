#!/usr/bin/env python3
"""Owner 2026-10-31: (1) SUBSCRIBERS get the previous day's results for the products they are signed up for; (2) every pick / result email lists games in CHRONOLOGICAL order.

Hermetic: no network, no real mail, no repo writes -- the subscriber file is a temp file, every send is a fake.

    python3 scripts/test_customer_settlement_and_order.py
"""
from __future__ import annotations

import json
import re
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import _subscribers as S  # noqa: E402
import auto_lock_settle as A  # noqa: E402

HOUR = 3600e3
BASE = datetime(2026, 11, 1, 18, 0, tzinfo=timezone.utc).timestamp() * 1000      # an arbitrary game day
DAY = "2026-11-01"


def iso(days_ago=0.0):
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


def row(sport, league, bet, outcome="win", start=BASE, **kw):
    r = {"id": f"{DAY}_{bet}", "betOn": bet, "sport": sport, "league": league, "outcome": outcome, "date": DAY, "hA": "HOM", "awA": "AWY", "hScore": 3, "aScore": 1, "startMs": start}
    r.update(kw)
    return r


class Subscribers(unittest.TestCase):
    """the real _subscribers loader over a temp file"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.f = self.tmp / "subscribers.json"
        self.patch = [mock.patch.object(S, "SUBSCRIBERS_FILE", self.f), mock.patch.object(A, "OWNER_EMAIL", "owner@example.com")]
        for p in self.patch:
            p.start()

    def tearDown(self):
        for p in self.patch:
            p.stop()

    def seed(self, data):
        full = {p: [] for p in S.PRODUCTS}
        full.update(data)
        self.f.write_text(json.dumps(full))


class CustomerGroups(Subscribers):
    def test_each_product_maps_from_its_ledger_codes_and_every_sold_league_is_covered(self):
        sold = {"nfl": {"NFL"}, "cfb": {"CFB"}, "nba": {"NBA"}, "hockey": {"NHL", "SHL", "LIIGA", "NLA", "EXTRALIGA"}, "soccer": {"PL", "LIGA", "SERIEA", "CL"}}
        for prod, codes in sold.items():
            self.assertEqual({c for c, p in A.LEDGER_TAG_PRODUCT.items() if p == prod}, codes)
        # PRODUCT_SPORTS (lock tags) and the ledger codes agree on every league that is sold
        lock_to_ledger = {"SOC_PL": "PL", "SOC_LIGA": "LIGA", "SOC_ITA": "SERIEA", "SOC_CL": "CL"}
        for prod, tags in A.PRODUCT_SPORTS.items():
            for t in tags:
                self.assertEqual(A.LEDGER_TAG_PRODUCT[lock_to_ledger.get(t, t)], prod, t)
        self.assertIsNone(A.settled_pick_product(row("KHL", "KHL", "x")))                      # owner-only league: never a customer product
        self.assertEqual(A.settled_pick_product(row("FOOTBALL", "NFL", "x")), "nfl")           # the legacy broad tag still resolves to its league
        self.assertEqual(A.settled_pick_product(row("SOCCER", "SERIEA", "x")), "soccer")

    def test_an_nfl_subscriber_gets_only_nfl_results(self):
        self.seed({"nfl": [{"email": "n@x.com", "added": iso(3)}]})
        settled = [row("NFL", "NFL", "KC ML"), row("NBA", "NBA", "BOS ML"), row("CFB", "CFB", "ALA -3"), row("NHL", "NHL", "TOR ML")]
        g = A.customer_settlement_groups(settled)
        self.assertEqual(len(g), 1)
        self.assertEqual((g[0]["products"], g[0]["emails"]), (("nfl",), ["n@x.com"]))
        self.assertEqual([b["betOn"] for b in g[0]["settled"]], ["KC ML"])

    def test_two_products_share_one_email_and_subscribers_with_the_same_products_share_it_bcc(self):
        self.seed({"nfl": [{"email": "a@x.com", "added": iso(1)}, {"email": "b@x.com", "added": iso(1)}], "hockey": [{"email": "a@x.com", "added": iso(1)}, {"email": "c@x.com", "added": iso(1)}]})
        g = {x["products"]: x for x in A.customer_settlement_groups([row("NFL", "NFL", "KC ML"), row("SHL", "SHL", "FRO ML"), row("NHL", "NHL", "TOR ML")])}
        self.assertEqual(set(g), {("hockey", "nfl"), ("nfl",), ("hockey",)})
        self.assertEqual(g[("hockey", "nfl")]["emails"], ["a@x.com"])
        self.assertEqual(sorted(b["betOn"] for b in g[("hockey", "nfl")]["settled"]), ["FRO ML", "KC ML", "TOR ML"])
        self.assertEqual(g[("hockey",)]["emails"], ["c@x.com"])
        self.assertEqual(g[("nfl",)]["emails"], ["b@x.com"])

    def test_nobody_is_emailed_when_their_products_had_no_picks(self):
        self.seed({"nfl": [{"email": "n@x.com", "added": iso(1)}]})
        self.assertEqual(A.customer_settlement_groups([row("NBA", "NBA", "BOS ML")]), [])
        self.assertEqual(A.customer_settlement_groups([]), [])

    def test_the_owner_expired_and_unknown_products_are_left_out(self):
        self.seed({"nfl": [{"email": "Owner@Example.com", "added": iso(1)}, {"email": "old@x.com", "added": iso(45)}, {"email": "new@x.com", "added": iso(1)}],
                   "mlb": [{"email": "retired@x.com", "added": iso(1)}]})
        g = A.customer_settlement_groups([row("NFL", "NFL", "KC ML")])
        self.assertEqual([x["emails"] for x in g], [["new@x.com"]])

    def test_a_free_promo_month_gets_results_like_anyone_else(self):
        self.seed({"cfb": [{"email": "free@x.com", "added": iso(1), "promo": True}]})
        self.assertEqual([x["emails"] for x in A.customer_settlement_groups([row("CFB", "CFB", "ALA -3")])], [["free@x.com"]])

    def test_removed_picks_and_nba_props_never_reach_customers(self):
        self.seed({"nba": [{"email": "n@x.com", "added": iso(1)}]})
        rows = [row("NBA", "NBA", "BOS ML"), row("NBA", "NBA", "Brunson PTS OVER 27.5", betType="PROP", player="Brunson", playerResult="29"), row("NBA", "NBA", "gone", outcome="_removed")]
        with mock.patch.object(A, "EMAIL_NBA_PROPS", False):
            self.assertEqual([b["betOn"] for b in A.customer_settlement_groups(rows)[0]["settled"]], ["BOS ML"])
        with mock.patch.object(A, "EMAIL_NBA_PROPS", True):
            self.assertEqual(len(A.customer_settlement_groups(rows)[0]["settled"]), 2)           # props come back with the switch; a removed pick never does

    def test_unresolved_picks_are_reported_for_their_products_only(self):
        self.seed({"nfl": [{"email": "n@x.com", "added": iso(1)}]})
        g = A.customer_settlement_groups([row("NFL", "NFL", "KC ML")], [row("NFL", "NFL", "BUF -3", outcome="pending"), row("NBA", "NBA", "BOS ML", outcome="pending")])
        self.assertEqual([b["betOn"] for b in g[0]["unresolved"]], ["BUF -3"])
        g2 = A.customer_settlement_groups([], [row("NFL", "NFL", "BUF -3", outcome="pending")])
        self.assertEqual(len(g2), 1)                                                                # only an unresolved pick still produces an email


class CustomerSend(Subscribers):
    def setUp(self):
        super().setUp()
        self.seed({"nfl": [{"email": "n@x.com", "added": iso(1)}, {"email": "m@x.com", "added": iso(1)}], "soccer": [{"email": "s@x.com", "added": iso(1)}]})
        self.sent, self.marker = [], {"v": ""}
        self.more = [mock.patch.object(A, "_send_gmail", side_effect=lambda subj, to, html: (self.sent.append((subj, to, html)) or (True, "ok"))),
                     mock.patch.object(A, "read_marker_from_origin", side_effect=lambda rel: self.marker["v"]),
                     mock.patch.object(A, "_commit_and_push", side_effect=lambda paths, msg: self.marker.update(v=(A.ROOT / paths[0]).read_text())),
                     mock.patch.object(A, "ROOT", self.tmp)]
        (self.tmp / "data").mkdir(exist_ok=True)
        for p in self.more:
            p.start()

    def tearDown(self):
        for p in self.more:
            p.stop()
        super().tearDown()

    def test_live_sends_one_bcc_email_per_product_set_with_the_right_subject_and_picks(self):
        n = A.send_customer_settlement_emails([row("NFL", "NFL", "KC ML"), row("NFL", "NFL", "BUF -3", outcome="loss"), row("PL", "PL", "ARS ML")], [], True, DAY)
        self.assertEqual(n, 2)
        by_to = {tuple(to): (subj, html) for subj, to, html in self.sent}
        self.assertEqual(set(by_to), {("m@x.com", "n@x.com"), ("s@x.com",)})                     # a list = Gmail BCC, never a visible To of strangers
        subj, html = by_to[("m@x.com", "n@x.com")]
        self.assertEqual(subj, f"Clairvoyance — Your results for {DAY}: 1W-1L (2)")
        self.assertIn("KC ML", html); self.assertNotIn("ARS ML", html)
        self.assertIn("ARS ML", by_to[("s@x.com",)][1]); self.assertNotIn("KC ML", by_to[("s@x.com",)][1])
        self.assertEqual(self.marker["v"], DAY)

    def test_a_dry_run_sends_nothing_and_writes_nothing(self):
        self.assertEqual(A.send_customer_settlement_emails([row("NFL", "NFL", "KC ML")], [], False, DAY), 0)
        self.assertEqual((self.sent, self.marker["v"]), ([], ""))

    def test_a_second_run_for_the_same_date_does_not_resend(self):
        A.send_customer_settlement_emails([row("NFL", "NFL", "KC ML")], [], True, DAY)
        before = len(self.sent)
        self.assertEqual(A.send_customer_settlement_emails([row("NFL", "NFL", "KC ML")], [], True, DAY), 0)
        self.assertEqual(len(self.sent), before)
        self.assertGreater(A.send_customer_settlement_emails([row("NFL", "NFL", "KC ML")], [], True, "2026-11-02"), 0)     # the next date is due again

    def test_the_switch_turns_it_off_and_a_failed_send_is_recorded_not_raised(self):
        with mock.patch.object(A, "EMAIL_CUSTOMER_SETTLEMENT", False):
            self.assertEqual(A.send_customer_settlement_emails([row("NFL", "NFL", "KC ML")], [], True, DAY), 0)
        self.assertEqual(self.sent, [])
        with mock.patch.object(A, "_send_gmail", return_value=(False, "smtp down")), mock.patch.object(A, "EMAIL_FAILURES", []) as fails:
            A.send_customer_settlement_emails([row("NFL", "NFL", "KC ML")], [], True, DAY)
            self.assertTrue(any("smtp down" in f for f in fails))

    def test_the_digest_sends_the_customer_emails_after_the_owner_copy(self):
        src = (HERE / "auto_lock_settle.py").read_text()
        i = src.index("def send_daily_settlement_digest")
        body = src[i:src.index("# LOCK", i)]
        self.assertLess(body.index("send_settlement_email(settled"), body.index("send_customer_settlement_emails("))


class SettlementHtml(unittest.TestCase):
    def test_results_inside_a_league_are_in_start_order_and_unknown_starts_go_last(self):
        rows = [row("NFL", "NFL", "LATE ML", start=BASE + 7 * HOUR), row("NFL", "NFL", "UNKNOWN ML", start=None), row("NFL", "NFL", "EARLY ML", start=BASE + 1 * HOUR), row("NFL", "NFL", "MID ML", start=BASE + 4 * HOUR)]
        for customer in (False, True):
            html = A.build_settlement_email_html(rows, customer=customer)
            self.assertEqual(sorted(["EARLY ML", "MID ML", "LATE ML", "UNKNOWN ML"], key=html.index), ["EARLY ML", "MID ML", "LATE ML", "UNKNOWN ML"])

    def test_leagues_are_ordered_by_their_earliest_game(self):
        rows = [row("NHL", "NHL", "N ML", start=BASE + 6 * HOUR), row("SHL", "SHL", "S ML", start=BASE), row("NLA", "NLA", "L ML", start=BASE + 2 * HOUR)]
        html = A.build_settlement_email_html(rows, customer=True)
        self.assertEqual(re.findall(r"(NHL|NLA|SHL) \(\d+W", html), ["SHL", "NLA", "NHL"])           # alphabetical would be NHL, NLA, SHL

    def test_the_customer_version_has_the_banner_and_footer_and_none_of_the_owner_notes(self):
        rows = [row("NFL", "NFL", "KC ML")]
        c = A.build_settlement_email_html(rows, unresolved=[row("NFL", "NFL", "BUF -3", outcome="pending")], late_excluded=3, customer=True)
        o = A.build_settlement_email_html(rows, unresolved=[row("NFL", "NFL", "BUF -3", outcome="pending")], late_excluded=3)
        self.assertIn("email_banner.jpg", c); self.assertIn("clairvoyanceengine.info", c); self.assertIn("still awaiting a final result", c)
        self.assertNotIn("Pre-start locks only", c); self.assertNotIn("Will keep retrying", c)
        self.assertIn("Pre-start locks only", o); self.assertIn("Will keep retrying", o); self.assertNotIn("email_banner.jpg", o)       # the owner's email is unchanged


def pick(sport, label, hA, awA, start, kind="GAME", **kw):
    q = {"kind": kind, "sport": sport, "hA": hA, "awA": awA, "side": "mlFav", "label": label, "prob": .7, "ml": "-110", "dec": 1.91, "tierN": 3, "evVal": .05,
         "lane": False, "mcSummary": None, "best": None, "startMs": start}
    q.update(kw)
    return q


class LocksEmailOrder(unittest.TestCase):
    def build(self, legs):
        return A.build_locks_email_html(legs, True, len(legs))

    def test_games_in_a_league_run_earliest_to_latest_whatever_order_they_were_found_in(self):
        legs = [pick("NBA", "G7 ML", "G7H", "G7A", BASE + 7 * HOUR), pick("NBA", "G1 ML", "G1H", "G1A", BASE + 1 * HOUR), pick("NBA", "G5 ML", "G5H", "G5A", BASE + 5 * HOUR)]
        html = self.build(legs)
        self.assertEqual(sorted(["G1A @ G1H", "G5A @ G5H", "G7A @ G7H"], key=html.index), ["G1A @ G1H", "G5A @ G5H", "G7A @ G7H"])

    def test_leagues_are_ordered_by_their_earliest_game_not_alphabetically(self):
        legs = [pick("NHL", "NHL ML", "TOR", "MTL", BASE + 6 * HOUR), pick("LIIGA", "LIIGA ML", "TPS", "HIFK", BASE - 3 * HOUR), pick("SHL", "SHL ML", "FRO", "LIN", BASE + 1 * HOUR)]
        html = self.build(legs)
        heads = [h for h in re.findall(r"(Liiga|SHL|NHL) \(\d\)", html)]
        self.assertEqual(heads, ["Liiga", "SHL", "NHL"])                                            # alphabetical would be Liiga, NHL, SHL

    def test_a_prop_sorts_with_its_game_and_a_game_with_no_start_goes_last(self):
        legs = [pick("NBA", "LATE ML", "LH", "LA", BASE + 8 * HOUR), pick("NBA", "NOTIME ML", "NH", "NA", None),
                pick("NBA", "EARLY ML", "EH", "EA", BASE + 2 * HOUR),
                {"kind": "PROP", "sport": "NBA", "leg": {"hA": "LH", "awA": "LA", "team": "LH", "opp": "LA", "player": "Late Guy", "statAbbr": "PTS", "line": 20.5, "over": True, "prob": .66, "grade": "OPTIMAL"}}]
        html = self.build(legs)
        pos = {k: html.index(k) for k in ("EA @ EH", "LA @ LH", "LH vs LA", "NA @ NH")}
        self.assertLess(pos["EA @ EH"], pos["LA @ LH"]); self.assertLess(pos["LA @ LH"], pos["NA @ NH"])
        self.assertLess(pos["EA @ EH"], pos["LH vs LA"]); self.assertLess(pos["LH vs LA"], pos["NA @ NH"])      # the late game's prop is with the late game, ahead of the untimed one

    def test_an_email_with_no_start_times_at_all_keeps_a_stable_name_order(self):
        legs = [pick("NBA", "B", "BH", "BA", None), pick("NBA", "A", "AH", "AA", None)]
        html = self.build(legs)
        self.assertLess(html.index("AA @ AH"), html.index("BA @ BH"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
