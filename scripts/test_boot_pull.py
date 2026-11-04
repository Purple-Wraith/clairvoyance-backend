"""The app's own boot-time ledger pull (docs/app.html), the single biggest source of Supabase egress found on 2026-11-04.

Measured: a headless page (navigator.webdriver is true, exactly how every CI lock / watchdog / settle session opens the app) ran a COMPLETE raw pull when it booted -- ~5 MB, 15 requests --
on top of the explicit ledger load the pipeline does anyway.  These tests pin the fix, with Supabase stubbed (nothing here reaches the real project):

  * ?nobootpull=1 skips the boot pull and ONLY that: zero requests to the bets table
  * a new / empty device bootstraps from the committed backup (served by the page's own host, no Supabase cost) and then asks Supabase for the last 10 days only -- never the whole table
  * a device that holds the ledger and pulled recently does the scoped 10-day pull; a complete raw pull comes back only after a WEEK (was a day)
  * the scoped pull also lists every PENDING pick whatever its date, so a pick still pending after 10 days that is settled elsewhere is not left waiting for the weekly pull
"""
from __future__ import annotations

import functools
import http.server
import json
import socketserver
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a, **k):
        pass


class BootPull(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        cls.web = socketserver.TCPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=str(ROOT / "docs")))
        threading.Thread(target=cls.web.serve_forever, daemon=True).start()
        cls.pw = sync_playwright().start()
        cls.b = cls.pw.chromium.launch()

    @classmethod
    def tearDownClass(cls):
        cls.b.close(); cls.pw.stop(); cls.web.shutdown()

    def open(self, qs, webdriver=True):
        ctx = self.b.new_context(viewport={"width": 1000, "height": 800})
        pg = ctx.new_page()
        calls: list[str] = []

        def sb(route):
            req = route.request
            cors = {"access-control-allow-origin": "*", "access-control-allow-headers": "*", "access-control-allow-methods": "*"}
            if req.method == "OPTIONS":
                return route.fulfill(status=204, headers=cors)
            if req.method == "GET" and "/rest/v1/bets" in req.url:
                calls.append(req.url)
            return route.fulfill(status=200, content_type="application/json", body="[]", headers=cors)
        pg.route("**/*supabase.co/**", sb)
        pg.route("**/*espn.com/**", lambda r: r.abort()); pg.route("**/*workers.dev/**", lambda r: r.abort())
        if not webdriver:
            pg.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>false})")
        pg.goto(f"http://127.0.0.1:{self.web.server_address[1]}/app.html?{qs}", wait_until="load", timeout=60000)
        pg.wait_for_function("typeof syncPicksFromSupabase==='function'&&typeof getP==='function'", timeout=20000)
        pg.wait_for_timeout(4000)
        return ctx, pg, calls

    @staticmethod
    def filtered(url):
        return "date.gte." in url        # the scoped pull is `or=(date.gte.<cut>,outcome.eq.pending)`: a recent window plus every pending pick, never the whole table

    def test_the_boot_flag_skips_the_boot_pull_and_nothing_else(self):
        ctx, pg, calls = self.open("nologo=1&nobootpull=1&nosw=1")
        self.assertEqual(calls, [], "the page booted and still pulled the bets table")
        # Supabase is NOT switched off (unlike nosb): the explicit pipeline pull still works on this page
        self.assertTrue(pg.evaluate("typeof _fetchSupabaseBetRows==='function'&&!_sbOff()"))
        pg.evaluate("_fetchSupabaseBetRows(10)")
        ctx.close()

    def test_without_the_flag_a_headless_boot_still_syncs(self):
        ctx, pg, calls = self.open("nologo=1&nosw=1")
        self.assertGreaterEqual(len(calls), 1)
        ctx.close()

    def test_a_new_device_bootstraps_from_the_backup_and_never_pulls_the_whole_table(self):
        ctx, pg, calls = self.open("nologo=1&nosw=1")                        # fresh storage = a new device (and webdriver = 'wants a full pull')
        self.assertGreaterEqual(pg.evaluate("getP().length"), 200)            # the committed backup filled it
        self.assertTrue(calls and all(self.filtered(u) for u in calls), calls)      # Supabase was only asked for a recent window, never the unfiltered table
        ctx.close()

    def test_the_scoped_pull_also_asks_for_every_pending_pick(self):
        ctx, pg, calls = self.open("nologo=1&nosw=1")
        self.assertTrue(calls and all("outcome.eq.pending" in u for u in calls), calls)      # not just the recent window
        # and it really does reach an old pending pick: a stale local 'pending' is settled by the remote row even though its date is 3 weeks old
        pg.evaluate("""(()=>{const p=getP().find(x=>x.outcome!=='pending'&&x.outcome);const old=Object.assign({},p,{id:'zz_old_pending',date:'2026-10-01',outcome:'pending',settledAt:null});saveP(getP().concat([old]))})()""")
        pg.evaluate("""_mergeRemoteBetRows([{id:'zz_old_pending',raw:Object.assign({},getP().find(x=>x.id==='zz_old_pending'),{outcome:'win',settledAt:5}),outcome:'win',settled_at:new Date(5).toISOString(),locked_at:null}])""")
        self.assertEqual(pg.evaluate("getP().find(x=>x.id==='zz_old_pending').outcome"), "win")
        ctx.close()

    def test_a_device_that_pulled_recently_does_the_scoped_pull_and_a_week_old_one_the_full_pull(self):
        ctx, pg, calls = self.open("nologo=1&nosw=1", webdriver=False)       # a normal browser
        self.assertGreaterEqual(pg.evaluate("getP().length"), 200)
        pg.evaluate(f"localStorage.setItem('cv_last_full_pull_ts',String({int((time.time() - 2 * 86400) * 1000)}))")    # last full pull: 2 days ago
        calls.clear(); pg.reload(wait_until="load"); pg.wait_for_timeout(4000)
        self.assertTrue(calls and all(self.filtered(u) for u in calls), calls)       # the old rule (24 h) would have pulled the whole table here
        pg.evaluate(f"localStorage.setItem('cv_last_full_pull_ts',String({int((time.time() - 8 * 86400) * 1000)}))")    # 8 days ago
        calls.clear(); pg.reload(wait_until="load"); pg.wait_for_timeout(4000)
        self.assertTrue(any(not self.filtered(u) for u in calls), calls)             # the weekly safety net: one complete pull
        ctx.close()


if __name__ == "__main__":
    unittest.main(verbosity=1)
