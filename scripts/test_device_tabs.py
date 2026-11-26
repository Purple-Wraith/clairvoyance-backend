#!/usr/bin/env python3
"""MAILING LIST + PROFIT on the owner's iPad / phone (2026-11-25).

Admin server: the Mac itself needs no password; anything else (another device, or Tailscale's https proxy) must send it, and is refused while none is set.
App: the Mailing List tab can be pointed at the Mac with an address + password kept on that device; Profit syncs again whenever its tab is shown, not only once per page load.

    python3 scripts/test_device_tabs.py
"""
from __future__ import annotations

import functools, http.server, sys, tempfile, threading, unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import subscribers_admin as A  # noqa: E402
from test_mailing_list import APP_ORIGIN, ROOT, Server, iso  # noqa: E402
from test_profit_tracker import WORKER, FakeWorker, Quiet  # noqa: E402

PASSWORD = "correct horse 9 é"
FAR = {"X-Forwarded-For": "100.64.0.9"}          # how a request through `tailscale serve` looks: from 127.0.0.1, but marked as forwarded


def key(p):
    from urllib.parse import quote
    return dict(FAR, **{"X-CV-Admin-Key": quote(p)})


class WhoIsLocal(unittest.TestCase):
    def test_only_a_direct_loopback_request_counts_as_this_mac(self):
        self.assertTrue(A.is_this_mac("127.0.0.1", {"Host": "127.0.0.1:8899"}))
        self.assertTrue(A.is_this_mac("::1", {"Host": "localhost:8899"}))
        self.assertFalse(A.is_this_mac("10.0.0.5", {"Host": "127.0.0.1:8899"}))                                  # another device on the network
        self.assertFalse(A.is_this_mac("127.0.0.1", {"Host": "mac.tail.ts.net:8443"}))                           # proxied / rebound name
        for h in ("X-Forwarded-For", "X-Forwarded-Host", "Forwarded", "Tailscale-User-Login"):
            self.assertFalse(A.is_this_mac("127.0.0.1", {"Host": "127.0.0.1:8899", h: "x"}), h)


class ServerPassword(unittest.TestCase):
    def setUp(self):
        self.s = Server()
        self.file = Path(tempfile.mkdtemp()) / "pw"
        self.patches = [mock.patch.object(A, "PASSWORD_FILE", self.file), mock.patch.object(A, "BAD_GUESS_DELAY", 0)]
        for p in self.patches:
            p.start()
        self.s.seed({"nba": [{"email": "sam@example.com", "added": iso(3)}]})

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.s.close()

    def test_with_no_password_set_other_devices_get_nothing_and_the_mac_still_works(self):
        for path in ("/api/overview", "/api/subscribers", "/api/analytics", "/api/check?email=sam@example.com"):
            code, _, body = self.s.req(path, headers=FAR)
            self.assertEqual((code, body["code"]), (403, "no_password"), path)
            self.assertNotIn("sam@example.com", str(body))
        self.assertEqual(self.s.req("/api/overview")[0], 200)

    def test_the_password_opens_it_and_a_wrong_or_missing_one_does_not(self):
        A.save_password(PASSWORD)
        self.assertNotIn(PASSWORD, self.file.read_text())
        self.assertEqual(self.file.stat().st_mode & 0o777, 0o600)
        for headers in (FAR, key("wrong password"), key(PASSWORD + " ")):
            code, _, body = self.s.req("/api/overview", headers=headers)
            self.assertEqual((code, body["code"]), (401, "password"))
            self.assertNotIn("sam@example.com", str(body))
        code, _, body = self.s.req("/api/overview", headers=key(PASSWORD))
        self.assertEqual((code, body["subscribers"][0]["email"]), (200, "sam@example.com"))
        self.assertEqual(self.s.req("/api/overview")[0], 200)                                                    # the Mac never needs it

    def test_changes_are_refused_without_the_password(self):
        A.save_password(PASSWORD)
        before = self.s.subs.read_text()
        for path in ("/api/add", "/api/remove", "/api/sync", "/api/receipt", "/api/referral/grant"):
            code, _, _ = self.s.req(path, "POST", {"product": "nba", "email": "new@example.com"}, origin=APP_ORIGIN, headers=FAR)
            self.assertEqual(code, 401, path)
        self.assertEqual(self.s.subs.read_text(), before)
        code, _, _ = self.s.req("/api/add", "POST", {"product": "nba", "email": "new@example.com"}, origin=APP_ORIGIN, headers=key(PASSWORD))
        self.assertEqual(code, 200)
        self.assertIn("new@example.com", self.s.subs.read_text())

    def test_the_browser_may_send_the_password_header(self):
        code, headers, _ = self.s.req("/api/overview", "OPTIONS", origin=APP_ORIGIN, headers=FAR)
        self.assertEqual(code, 204)
        self.assertIn("X-CV-Admin-Key", headers["Access-Control-Allow-Headers"])

    def test_a_short_password_is_not_saved(self):
        with self.assertRaises(ValueError):
            A.save_password("short")
        self.assertFalse(self.file.exists())


class Tabs(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        cls.web = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=str(ROOT / "docs")))
        cls.web.daemon_threads = True
        threading.Thread(target=cls.web.serve_forever, daemon=True).start()
        cls.pw = sync_playwright().start()
        cls.b = cls.pw.chromium.launch()

    @classmethod
    def tearDownClass(cls):
        cls.b.close(); cls.pw.stop(); cls.web.shutdown()

    def page(self, width=390):
        ctx = self.b.new_context(viewport={"width": width, "height": 900}, service_workers="block")
        self.addCleanup(ctx.close)
        pg = ctx.new_page()
        self.errors = []
        pg.on("pageerror", lambda e: self.errors.append(str(e)))
        pg.route("**/*espn.com/**", lambda r: r.abort())
        pg.goto("http://127.0.0.1:%d/app.html?nosb=1&nosw=1" % self.web.server_address[1], timeout=90000)
        pg.wait_for_function("typeof mlLoad==='function'&&typeof renderProfit==='function'&&typeof SS==='function'", timeout=90000)
        pg.wait_for_timeout(500)
        return pg

    def test_a_device_connects_with_the_address_and_password_and_remembers_them(self):
        s = Server()
        file = Path(tempfile.mkdtemp()) / "pw"
        with mock.patch.object(A, "PASSWORD_FILE", file), mock.patch.object(A, "BAD_GUESS_DELAY", 0):
            try:
                A.save_password(PASSWORD)
                s.seed({"nba": [{"email": "sam@example.com", "added": iso(3)}]})
                pg = self.page()
                # make the browser's requests look like they came through the https proxy, and keep the real local admin server out of the test
                pg.route("http://127.0.0.1:8899/**", lambda r: r.abort())
                pg.route(s.base + "/**", lambda r: r.continue_(headers=dict(r.request.headers, **FAR)))
                pg.evaluate("SS('mail')")
                pg.wait_for_selector("#ml-subs .ml-addr")
                pg.fill("#ml-subs .ml-addr", s.base)
                pg.click("#ml-subs button:has-text('SAVE & CONNECT')")
                pg.wait_for_function("document.querySelector('#ml-subs').innerText.includes('PASSWORD NEEDED')")
                pg.fill("#ml-subs .ml-key", PASSWORD)
                pg.click("#ml-subs button:has-text('SAVE & CONNECT')")
                pg.wait_for_selector("#ml-subs .ml-sub")
                self.assertIn("sam@example.com", pg.inner_text("#ml-subs"))
                self.assertEqual(pg.evaluate("[localStorage.getItem('cv_ml_base'),localStorage.getItem('cv_ml_key')]"), [s.base, PASSWORD])
                self.assertNotIn("sam@example.com", pg.evaluate("JSON.stringify(localStorage)"))                   # the list itself is never stored
                pg.reload(); pg.wait_for_function("typeof mlLoad==='function'&&typeof SS==='function'"); pg.wait_for_timeout(500)
                self.assertEqual(pg.evaluate("window.ML_BASE"), s.base)
                self.assertEqual(self.errors, [])
            finally:
                s.close()

    def test_the_admin_page_itself_asks_once_for_the_password(self):
        s = Server()
        file = Path(tempfile.mkdtemp()) / "pw"
        with mock.patch.object(A, "PASSWORD_FILE", file), mock.patch.object(A, "BAD_GUESS_DELAY", 0):
            try:
                A.save_password(PASSWORD)
                s.seed({"nba": [{"email": "sam@example.com", "added": iso(3)}]})
                ctx = self.b.new_context(viewport={"width": 390, "height": 900})
                self.addCleanup(ctx.close)
                pg = ctx.new_page()
                errors, asked = [], []
                pg.on("pageerror", lambda e: errors.append(str(e)))
                pg.on("dialog", lambda d: (asked.append(d.message), d.accept(PASSWORD)))
                pg.route(s.base + "/api/**", lambda r: r.continue_(headers=dict(r.request.headers, **FAR)))
                pg.goto(s.base + "/")
                pg.wait_for_function("document.body.innerText.includes('sam@example.com')", timeout=15000)
                self.assertEqual(len(asked), 1)
                pg.reload()
                pg.wait_for_function("document.body.innerText.includes('sam@example.com')", timeout=15000)
                self.assertEqual((len(asked), errors), (1, []))
            finally:
                s.close()

    def test_the_address_must_be_https(self):
        pg = self.page()
        self.assertEqual(pg.evaluate("['mac.tail.ts.net:8443','https://mac.tail.ts.net:8443/','http://192.168.1.4:8899','',  'http://127.0.0.1:9'].map(mlCleanBase)"),
                         ["https://mac.tail.ts.net:8443", "https://mac.tail.ts.net:8443", None, "", "http://127.0.0.1:9"])

    def test_profit_pulls_again_each_time_its_tab_is_shown_but_not_more_than_once_a_minute(self):
        pg = self.page(width=1300)
        fw = FakeWorker()
        pg.route(WORKER + "/**", fw.handle)
        gets = lambda: sum(1 for m, _, _ in fw.log if m == "GET")
        settle = lambda: (pg.wait_for_timeout(600), pg.wait_for_function("!_ptSyncing"))
        pg.evaluate("localStorage.setItem('cv_trigger_key','K-owner');SS('profit')"); settle()
        self.assertEqual(gets(), 1)
        pg.evaluate("SS('home');SS('profit')"); settle()
        self.assertEqual(gets(), 1)                                                                                # within the minute
        fw.entries = [{"id": "other1", "ts": 1790000000000, "date": "2026-11-20", "type": "DEPOSIT", "book": "DraftKings", "amount": 25}]; fw.rev += 1
        pg.evaluate("_ptAutoAt-=61000;document.dispatchEvent(new Event('visibilitychange'))"); settle()          # app reopened later
        self.assertEqual(gets(), 2)
        self.assertEqual(pg.evaluate("_ptState.entries.map(e=>e.id)"), ["other1"])
        self.assertEqual(self.errors, [])

    def test_a_device_without_the_key_says_so_and_sends_nothing(self):
        pg = self.page(width=1300)
        fw = FakeWorker()
        pg.route(WORKER + "/**", fw.handle)
        pg.evaluate("SS('profit')"); pg.wait_for_timeout(600)
        self.assertIn("Tap SYNC NOW to share entries", pg.inner_text("#sp-profit"))
        self.assertEqual(fw.log, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
