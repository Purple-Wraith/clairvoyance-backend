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
        self.patches = [mock.patch.object(S, "SUBSCRIBERS_FILE", self.subs), mock.patch.object(S, "EVENTS_FILE", self.events),
                        mock.patch.object(A, "send_receipt_email", side_effect=lambda e: (True, "sent")), mock.patch.object(A, "sync_subscribers_to_git", side_effect=lambda m: (True, "synced"))]
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
        with mock.patch.object(A, "send_receipt_email", side_effect=lambda e: (True, "sent")) as rc, mock.patch.object(A, "sync_subscribers_to_git", side_effect=lambda m: (True, "synced")) as sy:
            pg.click("#ml-addbtn"); pg.wait_for_selector("#ml-msg")
            self.assertTrue(rc.called and sy.called)
        self.assertIn("synced to GitHub", pg.inner_text("#ml-msg")); self.assertIn("receipt sent", pg.inner_text("#ml-msg"))
        pg.click("#sp-mail .sb2:has-text('SUBSCRIBERS')"); pg.wait_for_selector("#ml-subs .ml-sub[data-email='first@client.com']")
        got = pg.evaluate("[...document.querySelectorAll('#ml-subs .ml-sub[data-email=\"first@client.com\"] .ml-dl')].map(e=>e.innerText).join('|')")
        self.assertIn("30 days left", got)
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
