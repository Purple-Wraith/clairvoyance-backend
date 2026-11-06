"""Phone-storage fix (2026-11-06): the ledger outgrew iPhone localStorage (~2.6M chars), setItem threw, the app swallowed it and the phone froze at its last good save.

  * old settled picks lose `reasoning` in what is stored; pending / recent picks keep it; nothing else changes
  * a write that fails for size retries slimmer instead of being dropped (quota simulated)
  * automation (webdriver) is never slimmed, so CI sees the full ledger
  * a slimmed pick is never pushed to Supabase (it would overwrite the full row)
  * opening a slimmed pick's Why-this-pick panel loads its reasoning on demand
"""
from __future__ import annotations

import functools
import http.server
import json
import socketserver
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a, **k):
        pass


class StorageSlim(unittest.TestCase):
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

    def open(self, qs="nosb=1&nologo=1&nosw=1&slim=1", routes=None):
        ctx = self.b.new_context(viewport={"width": 390, "height": 800})
        pg = ctx.new_page()
        pg.route("**/*espn.com/**", lambda r: r.abort()); pg.route("**/*workers.dev/**", lambda r: r.abort())
        if routes:
            routes(pg)
        pg.goto(f"http://127.0.0.1:{self.web.server_address[1]}/app.html?{qs}", wait_until="load", timeout=60000)
        pg.wait_for_function("typeof saveP==='function'&&typeof getP==='function'&&typeof _storePreds==='function'", timeout=20000)
        return ctx, pg

    SEED = """(()=>{const mk=(id,date,outcome)=>({id,date,outcome,sport:'NBA',league:'NBA',hA:'A',awA:'B',betOn:'A ML',betType:'ML',decOdds:1.9,winProb:.7,wager:100,lockedAt:Date.now()-1e6,settledAt:outcome==='pending'?null:Date.now(),reasoning:'WHY '+id+' '+'x'.repeat(800)});
      const t=today(),old=nDaysAgoET(10),mid=nDaysAgoET(1);
      saveP([mk('old_win',old,'win'),mk('old_loss',old,'loss'),mk('new_win',mid,'win'),mk('today_pend',t,'pending'),mk('old_pend',old,'pending')]);})()"""

    def test_old_settled_picks_lose_reasoning_and_nothing_else(self):
        ctx, pg = self.open()
        pg.evaluate(self.SEED)
        d = {p["id"]: p for p in json.loads(pg.evaluate("localStorage.getItem('preds')"))}
        self.assertNotIn("reasoning", d["old_win"]); self.assertEqual(d["old_win"]["_rt"], 1)
        self.assertNotIn("reasoning", d["old_loss"])
        self.assertIn("reasoning", d["new_win"])                        # settled yesterday: still carries it
        self.assertIn("reasoning", d["today_pend"]); self.assertIn("reasoning", d["old_pend"])      # pending is never trimmed
        self.assertEqual(d["old_win"]["decOdds"], 1.9); self.assertEqual(d["old_win"]["outcome"], "win")
        ctx.close()

    def test_a_write_that_does_not_fit_retries_slimmer_instead_of_being_dropped(self):
        ctx, pg = self.open()
        # simulate a small phone quota: refuse any single value over 6000 chars until the picks are slim enough
        pg.evaluate("""(()=>{const o=Storage.prototype.setItem;Storage.prototype.setItem=function(k,v){if(k==='preds'&&String(v).length>2600)throw new DOMException('quota','QuotaExceededError');return o.call(this,k,v);};})()""")
        pg.evaluate(self.SEED.replace("const t=today()", "const t=today()"))
        raw = pg.evaluate("localStorage.getItem('preds')")
        self.assertIsNotNone(raw)
        self.assertEqual(len(json.loads(raw)), 5)                      # all five picks were stored (the fallback level that fits)
        self.assertLessEqual(len(raw), 2600)
        ctx.close()

    def test_automation_is_never_slimmed(self):
        ctx, pg = self.open("nosb=1&nologo=1&nosw=1")                   # headless = navigator.webdriver, no ?slim=1
        pg.evaluate(self.SEED)
        d = {p["id"]: p for p in json.loads(pg.evaluate("localStorage.getItem('preds')"))}
        self.assertIn("reasoning", d["old_win"]); self.assertNotIn("_rt", d["old_win"])
        ctx.close()

    def test_a_slimmed_pick_is_never_pushed_to_supabase(self):
        posted: list = []

        def routes(pg):
            def sb(route):
                req = route.request
                cors = {"access-control-allow-origin": "*", "access-control-allow-headers": "*", "access-control-allow-methods": "*"}
                if req.method == "OPTIONS":
                    return route.fulfill(status=204, headers=cors)
                if req.method == "POST" and "/rest/v1/bets" in req.url:
                    posted.append(json.loads(req.post_data or "[]"))
                return route.fulfill(status=200, content_type="application/json", body="[]", headers=cors)
            pg.route("**/*supabase.co/**", sb)
        ctx, pg = self.open("nologo=1&nobootpull=1&nosw=1&slim=1", routes)
        pg.evaluate(self.SEED)
        pg.evaluate("_syncBetsToSupabaseNow()")
        ids = {r["id"] for batch in posted for r in batch}
        self.assertIn("new_win", ids); self.assertIn("today_pend", ids)  # full copies still go up
        self.assertNotIn("old_win", ids)                                 # 10 days old = inside the 21-day push window, but its copy is trimmed: skipped, never overwrites the cloud row
        ctx.close()

    def test_the_why_panel_loads_a_trimmed_picks_reasoning_on_demand(self):
        def routes(pg):
            def sb(route):
                req = route.request
                cors = {"access-control-allow-origin": "*", "access-control-allow-headers": "*", "access-control-allow-methods": "*"}
                if req.method == "OPTIONS":
                    return route.fulfill(status=204, headers=cors)
                body = '[{"rs":"PICK: from the cloud"}]' if "rs:raw" in req.url else "[]"
                return route.fulfill(status=200, content_type="application/json", body=body, headers=cors)
            pg.route("**/*supabase.co/**", sb)
        ctx, pg = self.open("nologo=1&nobootpull=1&nosw=1&slim=1", routes)
        pg.evaluate(self.SEED)
        pg.evaluate("document.body.insertAdjacentHTML('beforeend','<div id=\"host\">'+_whyThisPickHTML(getP().find(p=>p.id==='old_win'))+'</div>')")
        pg.evaluate("document.querySelector('#host [onclick]').click()")
        pg.wait_for_function("document.querySelector('#host [data-rt]').innerText.includes('from the cloud')", timeout=8000)
        ctx.close()


if __name__ == "__main__":
    unittest.main(verbosity=1)
