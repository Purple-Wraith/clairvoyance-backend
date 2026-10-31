#!/usr/bin/env python3
"""MAILING LIST tab (owner 2026-10-25): the engine app manages the paid subscriber list through the LOCAL admin server (scripts/subscribers_admin.py) -- the subscriber data never goes into the public app.

Server: /api/overview (grouped by email, days left, expired flagged, analytics, activity), CORS for the engine app + local pages only, POSTs from any other origin refused, preflight answers Private-Network.
Tab: lists subscribers with their lists and time left, adds / renews / removes (and syncs), shows analytics + activity, says plainly when the server is not reachable, stores nothing in the browser.

    python3 scripts/test_mailing_list.py
"""
from __future__ import annotations

import functools, http.server, json, os, socketserver, sys, tempfile, threading, unittest, urllib.request, urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
ROOT = Path(os.environ.get("CV_ROOT") or HERE.parent)
sys.path.insert(0, str(HERE))
import _subscribers as S  # noqa: E402
import subscribers_admin as A  # noqa: E402

APP_ORIGIN = "https://purple-wraith.github.io"


def iso(days_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


class Server:
    """the real admin Handler on an ephemeral port, over temp data files, with the receipt email + git sync stubbed"""

    def __init__(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.subs, self.events = self.tmp / "subscribers.json", self.tmp / "subscriber_events.json"
        self.subs.write_text(json.dumps({p: [] for p in S.PRODUCTS}))
        self.events.write_text("[]")
        self.notice = mock.Mock(side_effect=lambda e: (True, "referral email queued"))          # the 'you earned a free month' email: recorded, never sent
        self.patches = [mock.patch.object(S, "SUBSCRIBERS_FILE", self.subs), mock.patch.object(S, "EVENTS_FILE", self.events),
                        mock.patch.object(A, "deliver_referral_notice", self.notice),
                        mock.patch.object(A, "deliver_receipt", side_effect=lambda e: (True, "receipt queued")), mock.patch.object(A, "sync_subscribers_to_git", side_effect=lambda m: (True, "synced"))]
        for p in self.patches:
            p.start()
        socketserver.TCPServer.allow_reuse_address = True
        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), A.Handler)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def close(self):
        self.srv.shutdown()
        for p in self.patches:
            p.stop()

    def seed(self, data: dict):
        full = {p: [] for p in S.PRODUCTS}; full.update(data)
        self.subs.write_text(json.dumps(full))

    def req(self, path, method="GET", body=None, origin=None, headers=None):
        h = dict(headers or {})
        if origin:
            h["Origin"] = origin
        data = None
        if body is not None:
            data = json.dumps(body).encode(); h.setdefault("Content-Type", "application/json")
        r = urllib.request.Request(self.base + path, data=data, method=method, headers=h)
        try:
            with urllib.request.urlopen(r) as resp:
                raw = resp.read(); return resp.status, dict(resp.headers), (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as e:
            raw = e.read(); return e.code, dict(e.headers), (json.loads(raw) if raw else None)


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.s = Server()

    @classmethod
    def tearDownClass(cls):
        cls.s.close()

    def setUp(self):
        self.s.seed({}); self.s.events.write_text("[]")

    def test_overview_groups_by_email_with_days_left_and_flags_expired(self):
        self.s.seed({"nba": [{"email": "A@x.com", "added": iso(2)}], "nfl": [{"email": "a@x.com", "added": iso(28)}, {"email": "old@x.com", "added": iso(40)}], "soccer": [{"email": "junk"}]})
        code, _, o = self.s.req("/api/overview")
        self.assertEqual(code, 200)
        by = {r["email"]: r for r in o["subscribers"]}
        self.assertEqual(set(by), {"a@x.com", "old@x.com"})                         # lower-cased + merged; the malformed row is skipped
        a = by["a@x.com"]
        self.assertEqual({p["product"]: p["days_left"] for p in a["products"]}, {"nba": 28, "nfl": 2})
        self.assertEqual((a["status"], a["active_count"], a["price"]), ("expiring", 2, 30))      # soonest list ends in 2 days; 2 lists -> $30
        self.assertEqual((by["old@x.com"]["status"], by["old@x.com"]["active_count"]), ("expired", 0))
        self.assertFalse(by["old@x.com"]["products"][0]["active"])
        self.assertEqual(o["subscribers"][0]["email"], "a@x.com")                    # expiring first, expired last
        self.assertIn("analytics", o); self.assertEqual(o["expiry_days"], 30); self.assertEqual([p["key"] for p in o["products"]], list(S.PRODUCTS))

    def test_add_renew_remove_and_the_activity_log(self):
        self.s.req("/api/add", "POST", {"product": "nba", "email": "New@Example.com"})
        self.s.req("/api/add", "POST", {"product": "hockey", "email": "new@example.com"})
        o = self.s.req("/api/overview")[2]
        self.assertEqual([p["product"] for p in o["subscribers"][0]["products"]], ["hockey", "nba"])
        self.assertTrue(all(p["days_left"] == 30 for p in o["subscribers"][0]["products"]))
        self.s.req("/api/add", "POST", {"product": "nba", "email": "new@example.com"})       # renew
        self.s.req("/api/remove", "POST", {"product": "hockey", "email": "new@example.com"})
        o = self.s.req("/api/overview")[2]
        self.assertEqual([p["product"] for p in o["subscribers"][0]["products"]], ["nba"])
        self.assertEqual([e["action"] for e in o["events"]], ["remove", "renew", "add", "add"])   # newest first

    def test_a_free_month_is_emailed_but_never_counted_as_revenue(self):
        self.s.req("/api/add", "POST", {"product": "cfb", "email": "Free@Example.com", "promo": True})
        self.s.req("/api/add", "POST", {"product": "nba", "email": "paid@example.com"})
        o = self.s.req("/api/overview")[2]
        by = {r["email"]: r for r in o["subscribers"]}
        f = by["free@example.com"]
        self.assertEqual((f["products"][0]["promo"], f["products"][0]["days_left"], f["promo_count"], f["price"], f["status"]), (True, 30, 1, 0, "active"))
        self.assertEqual((by["paid@example.com"]["promo_count"], by["paid@example.com"]["price"]), (0, 20))
        a = o["analytics"]
        self.assertEqual((a["active_emails"], a["active_subscriptions"], a["estimated_mrr"], a["promo_memberships"], a["promo_emails"]), (2, 2, 20, 1, 1))   # $20, not $40
        self.assertEqual(a["per_product_revenue"]["cfb"], 0)
        self.assertEqual(a["revenue_timeline"][-1]["estimated_revenue"], 20)
        self.assertIn("free@example.com", S.recipients_for("cfb"))                         # they still get every CFB email
        self.assertTrue(o["events"][1]["promo"] and "promo" not in o["events"][0])           # newest first: the paid add carries no flag
        self.assertTrue(next(r for r in S.products_for_email("free@example.com"))["promo"])

    def test_a_promo_month_mixed_with_a_paid_list_prices_only_the_paid_list(self):
        self.s.req("/api/add", "POST", {"product": "nba", "email": "mix@example.com"})
        self.s.req("/api/add", "POST", {"product": "cfb", "email": "mix@example.com", "promo": True})
        o = self.s.req("/api/overview")[2]
        r = o["subscribers"][0]
        self.assertEqual((r["active_count"], r["promo_count"], r["price"]), (2, 1, 20))
        self.assertEqual(o["analytics"]["estimated_mrr"], 20)

    def test_paying_after_a_free_month_turns_it_into_a_paid_month(self):
        self.s.req("/api/add", "POST", {"product": "cfb", "email": "conv@example.com", "promo": True})
        self.s.req("/api/add", "POST", {"product": "cfb", "email": "conv@example.com"})        # RENEW AS PAID: a normal add of the same entry
        o = self.s.req("/api/overview")[2]
        self.assertFalse(o["subscribers"][0]["products"][0]["promo"])
        self.assertEqual((o["analytics"]["estimated_mrr"], o["analytics"]["promo_memberships"]), (20, 0))
        self.assertEqual(json.loads(self.s.subs.read_text())["cfb"][0].get("promo"), None)

    def test_a_non_boolean_promo_value_is_not_a_free_month(self):
        self.s.req("/api/add", "POST", {"product": "cfb", "email": "x@example.com", "promo": "false"})        # a truthy string must never silently make someone free
        self.assertFalse(self.s.req("/api/overview")[2]["subscribers"][0]["products"][0]["promo"])

    def test_the_free_month_period_is_excluded_from_the_revenue_history_only_while_it_lasts(self):
        base = datetime.now(timezone.utc)
        ev = [{"ts": (base - timedelta(days=40)).isoformat(), "action": "add", "product": "cfb", "email": "c@x.com", "promo": True},
              {"ts": (base - timedelta(days=10)).isoformat(), "action": "renew", "product": "cfb", "email": "c@x.com"}]       # free month, then they paid
        self.s.events.write_text(json.dumps(ev))
        periods = S._reconstruct_periods()[0][("c@x.com", "cfb")]
        self.assertEqual(len(periods), 1)
        p = periods[0]
        self.assertEqual((p["free_from"], p["free_until"]), (datetime.fromisoformat(ev[0]["ts"]), datetime.fromisoformat(ev[0]["ts"]) + timedelta(days=30)))
        free_at, paid_at = base - timedelta(days=30), base - timedelta(days=5)
        isfree = lambda cp: bool(p["free_from"] and p["free_from"] <= cp < p["free_until"])
        self.assertTrue(isfree(free_at)); self.assertFalse(isfree(paid_at))

    def test_the_receipt_for_a_free_month_says_free_not_a_price(self):
        rows = [{"product": "cfb", "added": iso(0), "promo": True, "active": True, "days_left": 30}]
        _, html = S.build_receipt_email(rows)
        self.assertIn("FREE", html); self.assertIn("complimentary", html); self.assertNotIn("standard price", html)
        _, html2 = S.build_receipt_email([{"product": "nba", "added": iso(0), "promo": False}, rows[0]])
        self.assertIn("$20", html2); self.assertIn("1 complimentary", html2)
        _, html3 = S.build_receipt_email([{"product": "nba", "added": iso(0)}])
        self.assertIn("standard price for 1 product", html3); self.assertNotIn("complimentary", html3)

    # ── referrals (owner 2026-10-31): the REFERRER (an existing subscriber) earns a free month of a sport when a NEW subscriber names them ─────────────────────────────────────────
    def _seed_referrer(self, **entries):
        self.s.seed(entries or {"nba": [{"email": "ref@example.com", "added": iso(10)}]})
        self.s.events.write_text(json.dumps([{"ts": iso(10), "action": "add", "product": "nba", "email": "ref@example.com"}]))

    def test_referring_a_new_subscriber_adds_a_month_after_the_referrers_current_end(self):
        self._seed_referrer()                                                                    # ref pays for NBA, 20 days left
        code, _, o = self.s.req("/api/add", "POST", {"product": "cfb", "email": "New@Example.com", "referred_by": "REF@example.com", "reward": "nba"})
        self.assertEqual(code, 200); self.assertIn("referral reward", o["message"])
        ov = self.s.req("/api/overview")[2]
        by = {r["email"]: r for r in ov["subscribers"]}
        nba = next(p for p in by["ref@example.com"]["products"] if p["product"] == "nba")
        self.assertGreaterEqual(nba["days_left"], 49); self.assertLessEqual(nba["days_left"], 50)       # 20 left + 30 added, never shortened
        self.assertFalse(nba["promo"])                                                            # still a paid entry: the bonus does not mark the paid time free
        self.assertEqual([p["product"] for p in by["new@example.com"]["products"]], ["cfb"])      # the referred person was added normally (paid)
        self.assertFalse(by["new@example.com"]["products"][0]["promo"])
        a = ov["analytics"]
        self.assertEqual((a["estimated_mrr"], a["referral_rewards_30d"], a["referral_rewards_total"]), (40, 1, 1))     # ref $20 + new $20: the reward earns nothing
        ev = next(e for e in ov["events"] if e["action"] == "referral")
        self.assertEqual((ev["email"], ev["referred"], ev["product"], ev["extended"]), ("ref@example.com", "new@example.com", "nba", True))

    def test_a_sport_the_referrer_does_not_have_starts_as_a_free_month(self):
        self._seed_referrer()
        self.s.req("/api/add", "POST", {"product": "nfl", "email": "new@example.com", "referred_by": "ref@example.com", "reward": "soccer"})
        by = {r["email"]: r for r in self.s.req("/api/overview")[2]["subscribers"]}
        soc = next(p for p in by["ref@example.com"]["products"] if p["product"] == "soccer")
        self.assertTrue(soc["promo"]); self.assertEqual(soc["days_left"], 30)
        self.assertEqual(self.s.req("/api/overview")[2]["analytics"]["estimated_mrr"], 40)        # ref's paid NBA + new's NFL; the free soccer month is not counted

    def test_a_lapsed_referrer_gets_a_fresh_free_month_on_that_sport(self):
        self.s.seed({"nba": [{"email": "ref@example.com", "added": iso(50)}]})                    # expired 20 days ago
        self.s.req("/api/add", "POST", {"product": "cfb", "email": "new@example.com", "referred_by": "ref@example.com", "reward": "nba"})
        nba = next(p for p in {r["email"]: r for r in self.s.req("/api/overview")[2]["subscribers"]}["ref@example.com"]["products"] if p["product"] == "nba")
        self.assertEqual((nba["active"], nba["promo"], nba["days_left"]), (True, True, 30))

    def test_invalid_referrals_are_refused_and_add_nobody(self):
        self._seed_referrer()
        bad = [({"referred_by": "stranger@example.com", "reward": "nba"}, "not a subscriber"),
               ({"referred_by": "ref@example.com", "reward": "nba", "email": "ref@example.com"}, "cannot refer themselves"),
               ({"referred_by": "ref@example.com", "reward": "bogus"}, "pick which sport"),
               ({"referred_by": "not-an-email", "reward": "nba"}, "referrer's email")]
        for extra, why in bad:
            body = {"product": "cfb", "email": "new@example.com", **extra}
            code, _, o = self.s.req("/api/add", "POST", body)
            self.assertEqual(code, 400, why); self.assertIn(why, o["error"])
        by = {r["email"] for r in self.s.req("/api/overview")[2]["subscribers"]}
        self.assertEqual(by, {"ref@example.com"})                                                 # nobody was added, nobody rewarded
        self.assertFalse([e for e in self.s.req("/api/overview")[2]["events"] if e["action"] == "referral"])

    def test_an_existing_or_lapsed_subscriber_is_not_a_new_signup_and_one_reward_per_person(self):
        self._seed_referrer(**{"nba": [{"email": "ref@example.com", "added": iso(10)}], "nfl": [{"email": "old@example.com", "added": iso(60)}]})
        code, _, o = self.s.req("/api/add", "POST", {"product": "cfb", "email": "old@example.com", "referred_by": "ref@example.com", "reward": "nba"})
        self.assertEqual(code, 400); self.assertIn("already on a list", o["error"])             # a lapsed customer coming back is not a referral
        self.assertEqual(self.s.req("/api/add", "POST", {"product": "cfb", "email": "fresh@example.com", "referred_by": "ref@example.com", "reward": "nba"})[0], 200)
        self.s.req("/api/remove", "POST", {"product": "cfb", "email": "fresh@example.com"})
        s = json.loads(self.s.subs.read_text()); s["cfb"] = []; self.s.subs.write_text(json.dumps(s))   # even if they are cleared off every list and sign up again ...
        code, _, o = self.s.req("/api/add", "POST", {"product": "cfb", "email": "fresh@example.com", "referred_by": "ref@example.com", "reward": "nba"})
        self.assertEqual(code, 400); self.assertIn("already earned", o["error"])                  # ... they never earn a second reward

    def test_a_multi_product_signup_rewards_once(self):
        self._seed_referrer()
        self.s.req("/api/add", "POST", {"product": "cfb", "email": "new@example.com", "referred_by": "ref@example.com", "reward": "nba"})
        self.s.req("/api/add", "POST", {"product": "nfl", "email": "new@example.com"})              # the UI sends the referral on the first add only
        refs = [e for e in self.s.req("/api/overview")[2]["events"] if e["action"] == "referral"]
        self.assertEqual(len(refs), 1)

    def test_the_revenue_history_treats_the_referral_month_as_free(self):
        base = datetime.now(timezone.utc)
        ev = [{"ts": (base - timedelta(days=20)).isoformat(), "action": "add", "product": "nba", "email": "r@x.com"},
              {"ts": (base - timedelta(days=5)).isoformat(), "action": "referral", "product": "nba", "email": "r@x.com", "referred": "n@x.com", "extended": True}]
        self.s.events.write_text(json.dumps(ev))
        p = S._reconstruct_periods()[0][("r@x.com", "nba")][0]
        self.assertEqual(p["end"], p["start"] + timedelta(days=60))                              # the period now runs 60 days ...
        self.assertEqual((p["free_from"], p["free_until"]), (p["start"] + timedelta(days=30), p["start"] + timedelta(days=60)))      # ... and only the added month is free

    # ── the referrer chooses their sport: the reward waits, they are emailed, the owner gives it when they reply ──────────────────────────────
    def _pending(self):
        return self.s.req("/api/overview")[2]["pending_referrals"]

    def test_a_referral_without_a_sport_waits_and_emails_the_referrer(self):
        self._seed_referrer()
        self.s.notice.reset_mock()
        code, _, o = self.s.req("/api/add", "POST", {"product": "cfb", "email": "New@Example.com", "referred_by": "REF@example.com"})
        self.assertEqual(code, 200); self.assertIn("earned a free month", o["message"]); self.assertNotIn("NOT emailed", o["message"])
        self.s.notice.assert_called_once_with("REF@example.com")                                  # the REFERRER is emailed (never the new person)
        pend = self._pending()
        self.assertEqual([(p["referrer"], p["referred"]) for p in pend], [("ref@example.com", "new@example.com")])
        ov = self.s.req("/api/overview")[2]
        by = {r["email"]: r for r in ov["subscribers"]}
        self.assertEqual([p["product"] for p in by["new@example.com"]["products"]], ["cfb"])      # the new person is added normally
        self.assertRegex(str([p["days_left"] for p in by["ref@example.com"]["products"]]), r"\[(19|20)\]")      # the referrer's access is unchanged until they choose
        self.assertEqual(ov["analytics"]["referral_rewards_total"], 0)                            # nothing has been given yet
        self.assertIn("referral_pending", [e["action"] for e in ov["events"]])
        self.assertEqual(ov["analytics"]["estimated_mrr"], 40)                                    # a waiting reward never counts as revenue

    def test_one_waiting_reward_per_new_person_and_a_failed_email_is_reported_not_fatal(self):
        self._seed_referrer()
        self.s.notice.side_effect = lambda e: (False, "could not start the referral email on GitHub (HTTP 404)")
        try:
            code, _, o = self.s.req("/api/add", "POST", {"product": "cfb", "email": "new@example.com", "referred_by": "ref@example.com"})
            self.assertEqual(code, 200); self.assertIn("NOT emailed", o["message"]); self.assertIn("could not start", o["message"])
            self.assertEqual(len(self._pending()), 1)                                             # the sign-up and the waiting reward stand; the owner is told to ask them by hand
        finally:
            self.s.notice.side_effect = lambda e: (True, "referral email queued")
        code, _, o = self.s.req("/api/add", "POST", {"product": "nfl", "email": "new@example.com", "referred_by": "ref@example.com"})
        self.assertEqual(code, 400)                                                              # (new@ is also on a list now) -- either way a second reward is refused
        self.assertEqual(len(self._pending()), 1)

    def test_giving_the_waiting_reward_once_the_referrer_replies(self):
        self._seed_referrer()
        self.s.req("/api/add", "POST", {"product": "cfb", "email": "new@example.com", "referred_by": "ref@example.com"})
        bad = [({"referrer": "ref@example.com", "referred": "new@example.com", "reward": "bogus"}, "pick a sport"),
               ({"referrer": "ref@example.com", "referred": "someone-else@example.com", "reward": "nba"}, "not waiting"),
               ({"referrer": "other@example.com", "referred": "new@example.com", "reward": "nba"}, "not waiting")]
        for body, why in bad:
            code, _, o = self.s.req("/api/referral/grant", "POST", body)
            self.assertEqual(code, 400, body); self.assertIn(why, o["error"])
        self.assertEqual(len(self._pending()), 1)
        code, _, o = self.s.req("/api/referral/grant", "POST", {"referrer": "REF@example.com", "referred": "new@example.com", "reward": "soccer"})
        self.assertEqual(code, 200); self.assertIn("referral reward", o["message"])
        self.assertEqual(self._pending(), [])
        ov = self.s.req("/api/overview")[2]
        ref = next(r for r in ov["subscribers"] if r["email"] == "ref@example.com")
        self.assertEqual(sorted(p["product"] for p in ref["products"]), ["nba", "soccer"]); self.assertTrue(next(p for p in ref["products"] if p["product"] == "soccer")["promo"])
        self.assertEqual(ov["analytics"]["referral_rewards_total"], 1)
        self.assertEqual(self.s.req("/api/referral/grant", "POST", {"referrer": "ref@example.com", "referred": "new@example.com", "reward": "soccer"})[0], 400)      # never twice
        self.assertEqual(self.s.req("/api/add", "POST", {"product": "nfl", "email": "new@example.com", "referred_by": "ref@example.com"})[0], 400)

    def test_granting_to_a_sport_the_referrer_already_has_extends_it(self):
        self._seed_referrer()
        self.s.req("/api/add", "POST", {"product": "cfb", "email": "new@example.com", "referred_by": "ref@example.com"})
        self.s.req("/api/referral/grant", "POST", {"referrer": "ref@example.com", "referred": "new@example.com", "reward": "nba"})
        ref = next(r for r in self.s.req("/api/overview")[2]["subscribers"] if r["email"] == "ref@example.com")
        self.assertRegex(str(ref["products"][0]["days_left"]), r"^(49|50)$")                     # 20 left + 30; still the paid entry
        self.assertFalse(ref["products"][0]["promo"])

    def test_the_referrer_email_names_the_criteria_and_never_the_friend(self):
        subject, html = S.build_referral_notice_email()
        self.assertIn("free month", subject)
        for phrase in ("Reply to this email with the sport", "30 days free of any one sport", "NFL", "College Football", "NBA", "Hockey", "European Soccer"):
            self.assertIn(phrase, html)
        for rule in S.REFERRAL_CRITERIA:
            self.assertIn(rule, html)
        self.assertNotIn("@", html.replace("noreply", ""))                                       # no address of anyone ends up in the email
        self.assertIn("brand-new subscriber", html); self.assertIn("already have that sport", html.lower())

    def test_a_waiting_reward_does_not_disturb_revenue_history(self):
        self._seed_referrer()
        self.s.req("/api/add", "POST", {"product": "cfb", "email": "new@example.com", "referred_by": "ref@example.com"})
        periods, _ = S._reconstruct_periods()
        self.assertNotIn(("ref@example.com", ""), periods)                                       # the product-less event is skipped
        self.assertEqual(S.analytics_summary()["referral_rewards_total"], 0)

    def test_the_cli_waits_for_the_referrers_sport_then_grants_it(self):
        import manage_subscribers as M
        self._seed_referrer()
        base = dict(send_receipt_email=mock.Mock(return_value=(True, "")), sync_subscribers_to_git=mock.Mock(return_value=(True, "synced")))
        with mock.patch.object(sys, "argv", ["m", "add", "--referred-by", "ref@example.com", "cfb", "cli@example.com"]), mock.patch.multiple(M, send_referral_notice_email=mock.Mock(return_value=(True, "")), **base) as _:
            M.main()
        self.assertEqual([p["referrer"] for p in S.pending_referral_rewards()], ["ref@example.com"])
        self.assertLess(abs(S._parse(json.loads(self.s.subs.read_text())["nba"][0]["added"]) - S._parse(iso(10))), timedelta(seconds=5))      # still unchanged
        with mock.patch.object(sys, "argv", ["m", "grant-referral", "ref@example.com", "cli@example.com", "bogus"]), mock.patch.multiple(M, **base):
            with self.assertRaises(SystemExit):
                M.main()
        with mock.patch.object(sys, "argv", ["m", "grant-referral", "ref@example.com", "cli@example.com", "nfl"]), mock.patch.multiple(M, **base):
            M.main()
        self.assertEqual(S.pending_referral_rewards(), [])
        self.assertEqual([e["email"] for e in json.loads(self.s.subs.read_text())["nfl"]], ["ref@example.com"])

    def test_the_landing_page_says_the_referrer_earns_it_and_lists_the_criteria(self):
        page = ROOT.parent / "clairvoyance-landing" / "docs" / "index.html"
        if not page.exists():
            self.skipTest("landing repo not checked out here")
        html = page.read_text()
        for phrase in ("you</b> get 30 days free of any one sport", "must already be a Clairvoyance subscriber", "brand-new subscriber", "never on any of our lists before",
                       "One free month for each new friend", "You choose the sport", "added after your current access ends", "Did a Clairvoyance subscriber refer you?", "referrer&rsquo;s email address"):
            self.assertIn(phrase, html)
        self.assertNotIn('placeholder="their email address"', html)                              # the ambiguous box wording is gone
        self.assertIn("Referral note:", html)                                                    # the sign-up email carries the criteria too
        self.assertIn("Referred by: ' + refBy", html)
        for rule_start in ("You must already be a Clairvoyance subscriber", "brand-new subscriber"):     # the email and the page state the same rules
            self.assertTrue(any(rule_start in c for c in S.REFERRAL_CRITERIA))

    def test_the_cli_applies_a_referral(self):
        import manage_subscribers as M
        self._seed_referrer()
        with mock.patch.object(sys, "argv", ["m", "add", "--referred-by", "ref@example.com", "--reward", "nba", "cfb", "cli@example.com"]), \
                mock.patch.object(M, "send_receipt_email", return_value=(True, "")), mock.patch.object(M, "sync_subscribers_to_git", return_value=(True, "synced")):
            M.main()
        data = json.loads(self.s.subs.read_text())
        self.assertEqual([e["email"] for e in data["cfb"]], ["cli@example.com"])
        self.assertEqual(S._parse(data["nba"][0]["added"]) - S._parse(iso(10)) > timedelta(days=29), True)       # the referrer's NBA window moved out by a month
        with mock.patch.object(sys, "argv", ["m", "add", "--referred-by", "ghost@example.com", "--reward", "nba", "cfb", "cli2@example.com"]), \
                mock.patch.object(M, "send_receipt_email", return_value=(True, "")), mock.patch.object(M, "sync_subscribers_to_git", return_value=(True, "synced")):
            with self.assertRaises(SystemExit):
                M.main()
        self.assertNotIn("cli2@example.com", [e["email"] for e in json.loads(self.s.subs.read_text())["cfb"]])

    def test_the_cli_marks_a_promo_month(self):
        import manage_subscribers as M
        with mock.patch.object(sys, "argv", ["m", "add", "--promo", "cfb", "cli@example.com"]), mock.patch.object(M, "send_receipt_email", return_value=(True, "")), \
                mock.patch.object(M, "sync_subscribers_to_git", return_value=(True, "synced")):
            M.main()
        self.assertTrue(json.loads(self.s.subs.read_text())["cfb"][0]["promo"])

    def test_cors_only_for_the_engine_app_and_local_pages(self):
        for ok in (APP_ORIGIN, "http://127.0.0.1:5173", "http://localhost:8000"):
            _, h, _ = self.s.req("/api/overview", origin=ok)
            self.assertEqual(h.get("Access-Control-Allow-Origin"), ok, ok)
        for bad in ("https://evil.example", "https://purple-wraith.github.io.evil.example", "http://127.0.0.1.evil.example", "https://clairvoyanceengine.info"):
            _, h, _ = self.s.req("/api/overview", origin=bad)
            self.assertNotIn("Access-Control-Allow-Origin", h, bad)

    def test_a_post_from_another_website_is_refused_and_changes_nothing(self):
        code, _, j = self.s.req("/api/add", "POST", {"product": "nba", "email": "x@y.com"}, origin="https://evil.example")
        self.assertEqual(code, 403)
        code, _, _ = self.s.req("/api/remove", "POST", {"product": "nba", "email": "x@y.com"}, origin="https://evil.example", headers={"Content-Type": "text/plain"})
        self.assertEqual(code, 403)
        self.assertEqual(self.s.req("/api/overview")[2]["subscribers"], [])
        code, _, _ = self.s.req("/api/add", "POST", {"product": "nba", "email": "x@y.com"}, origin=APP_ORIGIN)       # the engine app may
        self.assertEqual(code, 200)
        code, _, _ = self.s.req("/api/add", "POST", {"product": "soccer", "email": "z@y.com"}, origin=self.s.base)    # the admin server's own page may
        self.assertEqual(code, 200)

    def test_preflight_answers_private_network_for_allowed_origins_only(self):
        code, h, _ = self.s.req("/api/add", "OPTIONS", origin=APP_ORIGIN, headers={"Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "content-type,x-cv-admin", "Access-Control-Request-Private-Network": "true"})
        self.assertEqual(code, 204)
        self.assertEqual(h.get("Access-Control-Allow-Origin"), APP_ORIGIN); self.assertEqual(h.get("Access-Control-Allow-Private-Network"), "true")
        self.assertIn("X-CV-Admin", h.get("Access-Control-Allow-Headers", ""))
        self.assertEqual(self.s.req("/api/add", "OPTIONS", origin="https://evil.example")[0], 403)

    def test_responses_are_never_cached(self):
        self.assertEqual(self.s.req("/api/overview")[1].get("Cache-Control"), "no-store")


class ReceiptDelivery(unittest.TestCase):
    """the Mac has no Gmail password, so a receipt is sent from GitHub: sync the list first, then dispatch send-receipt.yml"""

    def test_without_a_local_gmail_password_it_syncs_then_dispatches_the_workflow(self):
        calls = []
        with mock.patch.object(A, "_gmail_ready", return_value=False), \
             mock.patch.object(A, "sync_subscribers_to_git", side_effect=lambda m: calls.append("sync") or (True, "synced")), \
             mock.patch.object(A, "_run_gh", side_effect=lambda a: calls.append(("gh", a)) or (True, "")):
            ok, msg = A.deliver_receipt("  New@Client.com ")
        self.assertTrue(ok); self.assertIn("queued", msg)
        self.assertEqual(calls[0], "sync")                                                  # the workflow reads the private repo: it must be current first
        self.assertEqual(calls[1][1][:3], ["workflow", "run", "send-receipt.yml"]); self.assertIn("email=new@client.com", calls[1][1])
        self.assertIn(A.BACKEND_REPO, calls[1][1])

    def test_a_failed_sync_or_dispatch_is_reported_not_hidden(self):
        with mock.patch.object(A, "_gmail_ready", return_value=False), mock.patch.object(A, "sync_subscribers_to_git", return_value=(False, "push failed")), mock.patch.object(A, "_run_gh") as gh:
            ok, msg = A.deliver_receipt("a@b.com")
        self.assertFalse(ok); self.assertIn("could not sync", msg); gh.assert_not_called()
        with mock.patch.object(A, "_gmail_ready", return_value=False), mock.patch.object(A, "sync_subscribers_to_git", return_value=(True, "ok")), mock.patch.object(A, "_run_gh", return_value=(False, "HTTP 404")):
            ok, msg = A.deliver_receipt("a@b.com")
        self.assertFalse(ok); self.assertIn("could not start", msg)

    def test_with_a_local_gmail_password_it_sends_directly(self):
        with mock.patch.object(A, "_gmail_ready", return_value=True), mock.patch.object(A, "send_receipt_email", return_value=(True, "sent")) as send, mock.patch.object(A, "_run_gh") as gh:
            self.assertEqual(A.deliver_receipt("a@b.com"), (True, "sent"))
        send.assert_called_once_with("a@b.com"); gh.assert_not_called()

    def test_referral_notice_delivery_routes(self):
        with mock.patch.object(A, "_gmail_ready", return_value=False), mock.patch.object(A, "sync_subscribers_to_git") as sy, mock.patch.object(A, "_run_gh", return_value=(True, "")) as gh:
            ok, msg = A.deliver_referral_notice(" Ref@Example.com ")
        self.assertTrue(ok); self.assertIn("queued", msg); sy.assert_not_called()                  # the note is the same for everyone: nothing to sync first
        args = gh.call_args[0][0]
        self.assertEqual(args[:3], ["workflow", "run", "send-receipt.yml"]); self.assertIn("email=ref@example.com", args); self.assertIn("kind=referral", args)
        with mock.patch.object(A, "_gmail_ready", return_value=True), mock.patch.object(A, "send_referral_notice_email", return_value=(True, "sent")) as send, mock.patch.object(A, "_run_gh") as gh:
            self.assertEqual(A.deliver_referral_notice("a@b.com"), (True, "sent"))
        send.assert_called_once_with("a@b.com"); gh.assert_not_called()
        for bad in ("", "nope", "a@b.com;rm -rf /", "<a@b.com>"):
            with mock.patch.object(A, "_run_gh") as gh:
                self.assertFalse(A.deliver_referral_notice(bad)[0], bad); gh.assert_not_called()

    def test_junk_addresses_never_reach_a_command(self):
        for bad in ("", "nope", "a b@c.com", "a@b.com;rm -rf /", "x@y", "<a@b.com>"):
            with mock.patch.object(A, "_run_gh") as gh:
                self.assertFalse(A.deliver_receipt(bad)[0], bad); gh.assert_not_called()

    def test_a_recent_renewal_makes_the_receipt_a_renewal_receipt(self):
        srv = Server()
        try:
            S.add_subscriber("nba", "ren@x.com"); S.add_subscriber("nba", "ren@x.com")          # first add, then a renewal (event log: add, renew)
            self.assertTrue(S.recently_renewed("REN@x.com")); self.assertFalse(S.recently_renewed("other@x.com"))
            old = json.loads(srv.events.read_text()); old[-1]["ts"] = iso(2)                       # the renewal was two days ago: this is no longer 'a renewal just recorded'
            srv.events.write_text(json.dumps(old)); self.assertFalse(S.recently_renewed("ren@x.com"))
            rows = [{"product": "nba", "added": iso(0)}]
            sub, body = S.build_receipt_email(rows, renewal=True)
            self.assertIn("Renewal confirmation", sub); self.assertIn("Thanks for renewing", body); self.assertIn("Renewal Confirmation", body)
            sub, body = S.build_receipt_email(rows)
            self.assertIn("Access confirmation", sub); self.assertIn("Thanks for subscribing", body)
        finally:
            srv.close()

    def test_the_workflow_and_script_exist_and_pass_the_email_through_the_environment(self):
        wf = (ROOT / ".github" / "workflows" / "send-receipt.yml").read_text()
        self.assertIn("workflow_dispatch", wf); self.assertIn('RECEIPT_TO: ${{ inputs.email }}', wf); self.assertIn('--email "$RECEIPT_TO"', wf)
        self.assertNotIn('--email "${{', wf)                                             # never interpolated into the shell line
        self.assertIn("private-data", wf)
        self.assertIn("kind:", wf); self.assertIn("RECEIPT_KIND: ${{ inputs.kind }}", wf); self.assertIn('--kind "${RECEIPT_KIND:-receipt}"', wf)      # referral note vs access receipt; empty means receipt
        import send_receipt as R
        self.assertFalse(R.EMAIL_RE.match("a@b.com;x")); self.assertTrue(R.EMAIL_RE.match("a@b.com"))
        with mock.patch.object(R, "send_referral_notice_email", return_value=(True, "sent")) as note, mock.patch.object(R, "send_receipt_email", return_value=(True, "sent")) as rec, \
                mock.patch.object(sys, "argv", ["send_receipt.py", "--email", "A@b.com", "--kind", "referral"]):
            self.assertEqual(R.main(), 0)
        note.assert_called_once_with("a@b.com"); rec.assert_not_called()
        with mock.patch.object(R, "send_referral_notice_email") as note, mock.patch.object(R, "send_receipt_email", return_value=(True, "sent")) as rec, mock.patch.object(sys, "argv", ["send_receipt.py", "--email", "a@b.com"]):
            self.assertEqual(R.main(), 0)
        rec.assert_called_once_with("a@b.com"); note.assert_not_called()                          # default stays the access receipt


class TabTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        cls.s = Server()
        cls.s.seed({"nba": [{"email": "sam@example.com", "added": iso(3)}], "soccer": [{"email": "sam@example.com", "added": iso(26)}, {"email": "kim@example.com", "added": iso(1)}],
                    "hockey": [{"email": "lapsed@example.com", "added": iso(45)}]})
        class Quiet(http.server.SimpleHTTPRequestHandler):
            def log_message(self, *a, **k):
                pass
        cls.web = socketserver.TCPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=str(ROOT / "docs")))
        threading.Thread(target=cls.web.serve_forever, daemon=True).start()
        cls.pw = sync_playwright().start(); cls.b = cls.pw.chromium.launch(); cls.errs = []

    @classmethod
    def tearDownClass(cls):
        cls.b.close(); cls.pw.stop(); cls.web.shutdown(); cls.s.close()

    def setUp(self):
        self.s.seed({"nba": [{"email": "sam@example.com", "added": iso(3)}], "soccer": [{"email": "sam@example.com", "added": iso(26)}, {"email": "kim@example.com", "added": iso(1)}],
                     "hockey": [{"email": "lapsed@example.com", "added": iso(45)}]})
        self.s.events.write_text("[]")

    def page(self, w=1000, base=None, mobile=False):
        ctx = self.b.new_context(viewport={"width": w, "height": 900}, is_mobile=mobile, has_touch=mobile)
        pg = ctx.new_page()
        pg.on("pageerror", lambda e: self.errs.append(str(e)[:200]))
        pg.route("**/*espn.com/**", lambda r: r.abort()); pg.route("**/*workers.dev/**", lambda r: r.abort())
        pg.add_init_script(f"window.ML_BASE={json.dumps(base or self.s.base)};window.confirm=()=>true")
        pg.goto(f"http://127.0.0.1:{self.web.server_address[1]}/app.html?nosb=1&nosw=1")
        pg.wait_for_function("typeof SS==='function'&&typeof mlLoad==='function'"); pg.wait_for_timeout(800)
        return pg

    def open_tab(self, pg):
        pg.evaluate("SS('mail')"); pg.wait_for_selector("#ml-subs .ml-sub", timeout=8000)

    def test_the_nav_has_the_tab_and_it_lists_every_subscriber_with_time_left(self):
        pg = self.page()
        self.assertEqual(pg.evaluate("[...document.querySelectorAll('#sbar .sp')].map(b=>b.innerText.trim()).includes('MAILING LIST')"), True)
        pg.click("#sbar .sp:has-text('MAILING LIST')"); pg.wait_for_selector("#ml-subs .ml-sub")
        rows = pg.evaluate("[...document.querySelectorAll('#ml-subs .ml-sub')].map(e=>({email:e.dataset.email,cls:e.className,prods:[...e.querySelectorAll('.ml-prod')].map(p=>p.dataset.product+':'+p.querySelector('.ml-dl').innerText)}))")
        by = {r["email"]: r for r in rows}
        self.assertEqual(set(by), {"sam@example.com", "kim@example.com", "lapsed@example.com"})
        self.assertIn("expiring", by["sam@example.com"]["cls"]); self.assertIn("expired", by["lapsed@example.com"]["cls"])
        self.assertTrue(any(x.startswith("soccer:4 days left") for x in by["sam@example.com"]["prods"]), by["sam@example.com"])
        self.assertTrue(any("expired" in x for x in by["lapsed@example.com"]["prods"]))
        stats = pg.evaluate("[...document.querySelectorAll('#ml-subs .ml-sv')].map(e=>e.innerText)")
        self.assertEqual(stats[0], "2"); self.assertEqual(stats[1], "3")                                # 2 active people, 3 active memberships
        pg.context.close()

    def test_filters_by_list_and_status_and_search(self):
        pg = self.page(); self.open_tab(pg)
        pg.click("#ml-subs .ml-chip:has-text('HOCKEY')"); pg.wait_for_timeout(150)
        self.assertEqual(pg.evaluate("[...document.querySelectorAll('#ml-subs .ml-sub')].length"), 0)       # the only hockey entry has lapsed
        pg.click("#ml-subs .ml-chip:has-text('NBA')"); pg.wait_for_timeout(150)
        self.assertEqual(pg.evaluate("[...document.querySelectorAll('#ml-subs .ml-sub')].map(e=>e.dataset.email)"), ["sam@example.com"])
        pg.click("#ml-subs .ml-chip:has-text('ALL LISTS')"); pg.click("#ml-subs .ml-chip:text-is('EXPIRED')"); pg.wait_for_timeout(150)
        self.assertEqual(pg.evaluate("[...document.querySelectorAll('#ml-subs .ml-sub')].map(e=>e.dataset.email)"), ["lapsed@example.com"])
        pg.click("#ml-subs .ml-chip:text-is('ALL')"); pg.fill("#ml-q", "kim"); pg.wait_for_timeout(150)
        self.assertEqual(pg.evaluate("[...document.querySelectorAll('#ml-subs .ml-sub')].map(e=>e.dataset.email)"), ["kim@example.com"])
        pg.context.close()

    def test_add_a_new_subscriber_sends_a_receipt_syncs_and_shows_up(self):
        pg = self.page(); self.open_tab(pg)
        pg.click("#sp-mail .sb2:has-text('ADD / RENEW')"); pg.wait_for_selector("#ml-email")
        pg.fill("#ml-email", "first@client.com")
        pg.click("#ml-add .ml-chip:has-text('NBA')"); pg.click("#ml-add .ml-chip:has-text('HOCKEY')")
        self.assertIn("$30", pg.inner_text("#ml-add"))                                                   # two lists -> standard $30
        with mock.patch.object(A, "deliver_receipt", side_effect=lambda e: (True, "receipt queued")) as rc, mock.patch.object(A, "sync_subscribers_to_git", side_effect=lambda m: (True, "synced")) as sy:
            pg.click("#ml-addbtn"); pg.wait_for_selector("#ml-msg")
            self.assertTrue(rc.called and sy.called)
        self.assertIn("synced to GitHub", pg.inner_text("#ml-msg")); self.assertIn("receipt queued", pg.inner_text("#ml-msg"))
        pg.click("#sp-mail .sb2:has-text('SUBSCRIBERS')"); pg.wait_for_selector("#ml-subs .ml-sub[data-email='first@client.com']")
        got = pg.evaluate("[...document.querySelectorAll('#ml-subs .ml-sub[data-email=\"first@client.com\"] .ml-dl')].map(e=>e.innerText).join('|')")
        self.assertIn("30 days left", got)
        pg.context.close()

    def test_giving_a_free_month_from_the_add_tab(self):
        pg = self.page(); self.open_tab(pg)
        pg.click("#sp-mail .sb2:has-text('ADD / RENEW')"); pg.wait_for_selector("#ml-email")
        pg.fill("#ml-email", "gift@client.com"); pg.click("#ml-add .ml-chip:has-text('COLLEGE FOOTBALL')")
        self.assertIn("$20", pg.inner_text("#ml-add"))
        pg.check("#ml-promo"); pg.wait_for_selector("#ml-add:has-text('FREE (no charge')")
        self.assertIn("GIVE A FREE MONTH", pg.inner_text("#ml-addbtn")); self.assertNotIn("$20", pg.inner_text("#ml-add .ml-bar + .ml-dl"))
        self.assertEqual(pg.input_value("#ml-email"), "gift@client.com")                      # toggling must not wipe what was typed
        with mock.patch.object(A, "deliver_receipt", side_effect=lambda e: (True, "receipt queued")), mock.patch.object(A, "sync_subscribers_to_git", side_effect=lambda m: (True, "synced")) as sy:
            pg.click("#ml-addbtn"); pg.wait_for_selector("#ml-msg")
            self.assertIn("Free month", sy.call_args[0][0])
        self.assertIn("Gave a free month to gift@client.com", pg.inner_text("#ml-msg"))
        self.assertFalse(pg.is_checked("#ml-promo")); self.assertEqual(pg.input_value("#ml-email"), "")      # back to the normal, empty add form
        self.assertIn("ADD / RENEW", pg.inner_text("#ml-addbtn"))
        pg.click("#sp-mail .sb2:has-text('SUBSCRIBERS')"); pg.wait_for_selector("#ml-subs .ml-sub[data-email='gift@client.com']")
        card = pg.inner_text("#ml-subs .ml-sub[data-email='gift@client.com']")
        self.assertIn("FREE MONTH", card); self.assertIn("free month", card); self.assertIn("RENEW AS PAID", card); self.assertNotIn("$20", card)
        stats = pg.evaluate("[...document.querySelectorAll('#ml-subs .ml-stat')].map(e=>e.innerText.replace(/\\n/g,' '))")
        self.assertTrue(any("1 free / promo" in x for x in stats), stats)
        self.assertTrue(any(x.startswith("$50") for x in stats), stats)                        # seeded paid: sam on 2 lists ($30) + kim ($20); the free month adds nothing
        pg.context.close()

    def test_referring_a_friend_from_the_add_tab(self):
        self.s.seed({"nba": [{"email": "ref@example.com", "added": iso(10)}]})
        pg = self.page(); self.open_tab(pg)
        pg.click("#sp-mail .sb2:has-text('ADD / RENEW')"); pg.wait_for_selector("#ml-email")
        pg.fill("#ml-email", "newbie@client.com"); pg.click("#ml-add .ml-chip:has-text('COLLEGE FOOTBALL')")
        self.assertEqual(pg.query_selector("#ml-refbox"), None)
        pg.check("#ml-ref"); pg.wait_for_selector("#ml-refbox")
        self.assertEqual(pg.input_value("#ml-email"), "newbie@client.com")
        pg.check("#ml-promo"); pg.wait_for_timeout(150)                                           # the two options are exclusive
        self.assertFalse(pg.is_checked("#ml-ref")); pg.check("#ml-ref"); pg.wait_for_selector("#ml-refbox"); self.assertFalse(pg.is_checked("#ml-promo"))
        with mock.patch.object(A, "deliver_receipt", side_effect=lambda e: (True, "receipt queued")), mock.patch.object(A, "sync_subscribers_to_git", side_effect=lambda m: (True, "synced")) as sy:
            pg.click("#ml-addbtn"); pg.wait_for_selector("#ml-msg")                               # no friend / sport yet
            self.assertIn("who referred them", pg.inner_text("#ml-msg")); self.assertFalse(sy.called)
            pg.fill("#ml-refby", "stranger@example.com"); pg.click("#ml-refbox .ml-chip:has-text('NBA')")
            pg.click("#ml-addbtn"); pg.wait_for_function("document.querySelector('#ml-msg')&&/not a subscriber/.test(document.querySelector('#ml-msg').innerText)")
            self.assertFalse(sy.called)                                                           # refused: nothing added, nothing synced
            self.assertEqual(pg.input_value("#ml-email"), "newbie@client.com")                    # the form keeps what was typed so it can be fixed
            pg.fill("#ml-refby", "ref@example.com")
            pg.click("#ml-addbtn"); pg.wait_for_function("document.querySelector('#ml-msg')&&/referral reward/.test(document.querySelector('#ml-msg').innerText)")
            self.assertIn("Referred sign-up", sy.call_args[0][0])
        self.assertFalse(pg.is_checked("#ml-ref")); self.assertEqual(pg.input_value("#ml-email"), "")
        pg.click("#sp-mail .sb2:has-text('SUBSCRIBERS')"); pg.wait_for_selector("#ml-subs .ml-sub[data-email='newbie@client.com']")
        card = pg.inner_text("#ml-subs .ml-sub[data-email='ref@example.com']")
        self.assertRegex(card, r"(49|50) days left")
        pg.click("#sp-mail .sb2:has-text('ACTIVITY')"); pg.wait_for_selector("#ml-act table")
        self.assertIn("REFERRAL REWARD", pg.inner_text("#ml-act")); self.assertIn("for referring newbie@client.com", pg.inner_text("#ml-act"))
        pg.context.close()

    def test_a_waiting_referral_reward_is_given_from_the_subscribers_tab(self):
        self.s.seed({"nba": [{"email": "ref@example.com", "added": iso(10)}]})
        self.s.events.write_text("[]")
        pg = self.page(); self.open_tab(pg)
        pg.click("#sp-mail .sb2:has-text('ADD / RENEW')"); pg.wait_for_selector("#ml-email")
        pg.fill("#ml-email", "newbie@client.com"); pg.click("#ml-add .ml-chip:has-text('COLLEGE FOOTBALL')")
        pg.check("#ml-ref"); pg.wait_for_selector("#ml-refbox")
        self.assertIn("REFERRER", pg.inner_text("#ml-refbox")); self.assertIn("emailed to choose", pg.inner_text("#ml-refbox"))      # no sport picked: the referrer will be asked
        pg.click("#ml-refbox .ml-chip:has-text('NBA')"); self.assertIn("right away", pg.inner_text("#ml-refbox"))
        pg.click("#ml-refbox .ml-chip:has-text('NBA')"); self.assertIn("emailed to choose", pg.inner_text("#ml-refbox"))            # a second tap clears it
        pg.fill("#ml-refby", "ref@example.com")
        with mock.patch.object(A, "deliver_receipt", side_effect=lambda e: (True, "receipt queued")), mock.patch.object(A, "sync_subscribers_to_git", side_effect=lambda m: (True, "synced")):
            pg.click("#ml-addbtn"); pg.wait_for_function("document.querySelector('#ml-msg')&&/earned a free month/.test(document.querySelector('#ml-msg').innerText)")
        self.s.notice.assert_called_with("ref@example.com")
        pg.click("#sp-mail .sb2:has-text('SUBSCRIBERS')"); pg.wait_for_selector("#ml-pending")
        self.assertIn("ref@example.com", pg.inner_text("#ml-pending")); self.assertIn("newbie@client.com", pg.inner_text("#ml-pending"))
        pg.click("#ml-pending .ml-btn.go"); pg.wait_for_selector("#ml-msg")
        self.assertIn("Pick the sport", pg.inner_text("#ml-msg"))                                   # no sport chosen yet
        pg.click("#ml-pending .ml-chip:has-text('HOCKEY')")
        with mock.patch.object(A, "deliver_receipt", side_effect=lambda e: (True, "receipt queued")) as rc, mock.patch.object(A, "sync_subscribers_to_git", side_effect=lambda m: (True, "synced")) as sy:
            pg.click("#ml-pending .ml-btn.go"); pg.wait_for_function("document.querySelector('#ml-msg')&&/Gave ref@example.com a free month/.test(document.querySelector('#ml-msg').innerText)")
            rc.assert_called_with("ref@example.com"); self.assertIn("Referral reward", sy.call_args[0][0])
        self.assertEqual(pg.query_selector("#ml-pending"), None)                                    # nothing left waiting
        self.assertEqual(pg.evaluate("[...document.querySelectorAll('#ml-subs .ml-sub[data-email=\"ref@example.com\"] .ml-pill')].some(e=>/FREE MONTH/.test(e.innerText))"), True)
        pg.click("#sp-mail .sb2:has-text('ACTIVITY')"); pg.wait_for_selector("#ml-act table")
        self.assertIn("REFERRAL EARNED", pg.inner_text("#ml-act")); self.assertIn("REFERRAL REWARD", pg.inner_text("#ml-act"))
        pg.context.close()

    def test_a_failed_sync_is_reported_loudly(self):
        pg = self.page(); self.open_tab(pg)
        with mock.patch.object(A, "sync_subscribers_to_git", side_effect=lambda m: (False, "push failed")):
            pg.evaluate("mlRenew('kim@example.com','soccer')"); pg.wait_for_selector("#ml-msg.err")
        self.assertIn("NOT synced", pg.inner_text("#ml-msg")); self.assertIn("will not get emails", pg.inner_text("#ml-msg"))
        pg.context.close()

    def test_remove_and_renew_buttons(self):
        pg = self.page(); self.open_tab(pg)
        pg.click("#ml-subs .ml-sub[data-email='sam@example.com'] .ml-prod[data-product='soccer'] .ml-btn.rm"); pg.wait_for_selector("#ml-msg")
        prods = pg.evaluate("[...document.querySelectorAll('#ml-subs .ml-sub[data-email=\"sam@example.com\"] .ml-prod')].map(p=>p.dataset.product)")
        self.assertEqual(prods, ["nba"])
        pg.click("#ml-subs .ml-sub[data-email='sam@example.com'] .ml-btn.go:has-text('RENEW ALL')"); pg.wait_for_selector("#ml-subs .ml-sub[data-email='sam@example.com']:has-text('30 days left')")
        pg.context.close()

    def test_renewing_emails_a_renewal_receipt_unless_the_box_is_off(self):
        pg = self.page(); self.open_tab(pg)
        with mock.patch.object(A, "deliver_receipt", side_effect=lambda e: (True, "receipt queued")) as rc:
            pg.evaluate("mlRenew('kim@example.com','soccer')"); pg.wait_for_selector("#ml-msg")
            self.assertEqual([c.args[0] for c in rc.call_args_list], ["kim@example.com"])
            self.assertIn("receipt queued", pg.inner_text("#ml-msg"))
            pg.uncheck("#ml-subs input[type=checkbox]"); pg.wait_for_timeout(100)
            pg.evaluate("mlRenew('kim@example.com','cfb')"); pg.wait_for_timeout(1500)
            self.assertEqual(rc.call_count, 1)                                               # box off: no second receipt
            pg.check("#ml-subs input[type=checkbox]")
            pg.evaluate("mlRenewAll('sam@example.com')"); pg.wait_for_timeout(2000)
            self.assertEqual([c.args[0] for c in rc.call_args_list], ["kim@example.com", "sam@example.com"])   # one receipt for the whole renew-all
        pg.context.close()

    def test_tracked_since_is_a_mountain_time_date(self):
        # an event at 02:22 UTC on Oct 26 is the evening of Oct 25 in Mountain time
        self.s.events.write_text(json.dumps([{"ts": "2026-10-26T02:22:45+00:00", "action": "add", "product": "nfl", "email": "ste@x.com"}]))
        pg = self.page(); self.open_tab(pg)
        pg.click("#sp-mail .sb2:has-text('ANALYTICS')"); pg.wait_for_selector("#ml-ana .ml-stat")
        self.assertIn("TRACKED SINCE 2026-10-25", pg.inner_text("#ml-ana"))
        pg.context.close()

    def test_analytics_and_activity_tabs_render(self):
        pg = self.page(); self.open_tab(pg)
        pg.click("#sp-mail .sb2:has-text('ANALYTICS')"); pg.wait_for_selector("#ml-ana .ml-stat")
        t = pg.inner_text("#ml-ana")
        for s in ("ACTIVE SUBSCRIBERS", "EST. 30-DAY REVENUE", "ESTIMATED REVENUE BY WEEK", "BY LIST", "EXPIRING SOON"):
            self.assertIn(s, t)
        pg.click("#sp-mail .sb2:has-text('ACTIVITY')"); pg.wait_for_selector("#ml-act .ml-card")
        self.assertIn("WHEN", pg.inner_text("#ml-act") + "WHEN")
        pg.context.close()

    def test_when_the_server_is_not_running_the_tab_says_so_and_shows_no_data(self):
        pg = self.page(base="http://127.0.0.1:9")
        pg.evaluate("SS('mail')"); pg.wait_for_selector("#ml-subs .ml-card", timeout=8000)
        t = pg.inner_text("#ml-subs")
        self.assertIn("NOT REACHABLE", t); self.assertNotIn("@", t.replace("their@email.com", ""))
        self.assertIn("python3 scripts/subscribers_admin.py", t)
        pg.context.close()

    def test_nothing_is_stored_in_the_browser(self):
        pg = self.page(); self.open_tab(pg)
        dump = pg.evaluate("JSON.stringify([Object.entries(localStorage),Object.entries(sessionStorage)])")
        self.assertNotIn("example.com", dump)
        pg.context.close()

    def test_phone_layout_fits_and_buttons_are_thumb_sized(self):
        for w in (390, 320):
            pg = self.page(w=w, mobile=True); self.open_tab(pg)
            self.assertFalse(pg.evaluate("document.documentElement.scrollWidth>document.documentElement.clientWidth"), w)
            self.assertGreaterEqual(pg.evaluate("Math.min(...[...document.querySelectorAll('#ml-subs .ml-btn,#ml-subs .ml-chip')].map(b=>b.getBoundingClientRect().height))"), 38)
            pg.context.close()

    def test_no_script_errors(self):
        self.assertEqual([e for e in self.errs if "ml" in e.lower() or "mail" in e.lower()], [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
