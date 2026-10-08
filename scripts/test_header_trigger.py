#!/usr/bin/env python3
"""Header LOCK NOW / SETTLE NOW buttons (owner request 2026-10-08): sit below the LAST LOCK / LAST SETTLE chips, ask for confirmation, send the owner key to the scheduler Worker,
remember a good key, forget a wrong one, and report what the Worker said.  The Worker itself is tested in scheduler/test_scheduler.mjs.

    python3 scripts/test_header_trigger.py
"""
import functools, http.server, json, socketserver, threading, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a, **k):
        pass


class Trigger(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        socketserver.TCPServer.allow_reuse_address = True
        cls.srv = socketserver.TCPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=str(ROOT / "docs")))
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.pw = sync_playwright().start()
        cls.b = cls.pw.chromium.launch()

    @classmethod
    def tearDownClass(cls):
        cls.b.close(); cls.pw.stop(); cls.srv.shutdown()

    def page(self, reply_status=200, reply=None, key_prompt="secret-key", stored_key=None, accept=True):
        pg = self.b.new_page(viewport={"width": 1300, "height": 900})
        self.calls, self.dialogs = [], []
        reply = reply if reply is not None else {"ok": True, "message": "LOCK started"}

        def handle(route):
            req = route.request
            if req.method == "OPTIONS":
                return route.fulfill(status=204, headers={"access-control-allow-origin": "*"})
            self.calls.append(json.loads(req.post_data))
            route.fulfill(status=reply_status, headers={"access-control-allow-origin": "*", "content-type": "application/json"}, body=json.dumps(reply))
        pg.route("**/clairvoyance-scheduler.*/trigger", handle)

        def on_dialog(d):
            self.dialogs.append((d.type, d.message))
            if d.type == "prompt":
                d.accept(key_prompt)
            else:
                d.accept() if accept else d.dismiss()
        pg.on("dialog", on_dialog)
        pg.goto(f"http://127.0.0.1:{self.srv.server_address[1]}/app.html?nosb=1")
        pg.wait_for_function("typeof _hdrTrigger==='function'")
        pg.wait_for_timeout(500)
        if stored_key:
            pg.evaluate("k=>localStorage.setItem('cv_trigger_key',k)", stored_key)
        return pg

    def test_buttons_sit_below_the_two_chips(self):
        pg = self.page()
        r = pg.evaluate("""()=>{const q=id=>document.getElementById(id).getBoundingClientRect();
          return {lockBelow:q('hdr-trig-lock').top>=q('hdr-automation-status-left').bottom-1, settleBelow:q('hdr-trig-settle').top>=q('hdr-automation-status').bottom-1,
                  txt:[document.getElementById('hdr-trig-lock').textContent,document.getElementById('hdr-trig-settle').textContent]}}""")
        self.assertTrue(r["lockBelow"] and r["settleBelow"], r)
        self.assertIn("LOCK NOW", r["txt"][0]); self.assertIn("SETTLE NOW", r["txt"][1])
        pg.close()

    def test_lock_sends_the_key_once_then_remembers_it(self):
        pg = self.page()
        pg.click("#hdr-trig-lock"); pg.wait_for_timeout(800)
        self.assertEqual(self.calls, [{"action": "lock", "key": "secret-key"}])
        self.assertEqual([d[0] for d in self.dialogs], ["confirm", "prompt"])
        self.assertEqual(pg.evaluate("localStorage.getItem('cv_trigger_key')"), "secret-key")
        self.dialogs.clear()
        pg.click("#hdr-trig-settle"); pg.wait_for_timeout(800)                       # key remembered: confirm only, no prompt
        self.assertEqual(self.calls[-1], {"action": "settle", "key": "secret-key"})
        self.assertEqual([d[0] for d in self.dialogs], ["confirm"])
        pg.close()

    def test_declining_the_confirmation_sends_nothing(self):
        pg = self.page(accept=False)
        pg.click("#hdr-trig-lock"); pg.wait_for_timeout(500)
        self.assertEqual(self.calls, [])
        pg.close()

    def test_wrong_key_is_forgotten(self):
        pg = self.page(reply_status=401, reply={"error": "wrong key"}, stored_key="old")
        pg.click("#hdr-trig-settle"); pg.wait_for_timeout(800)
        self.assertEqual(self.calls, [{"action": "settle", "key": "old"}])
        self.assertIsNone(pg.evaluate("localStorage.getItem('cv_trigger_key')"))
        self.assertTrue(pg.evaluate("!document.getElementById('hdr-trig-settle').disabled"))
        pg.close()

    def test_phone_width_keeps_the_header_usable(self):
        pg = self.b.new_page(viewport={"width": 390, "height": 844})
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        fresh = {k: {"tsUTC": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "tsMT": "2026-10-08 07:48 PM MT", "ok": True, "detail": "x"} for k in ("lastLock", "lastSettle")}
        pg.route("**/automation_status.json*", lambda route: route.fulfill(status=200, content_type="application/json", body=json.dumps(fresh)))
        pg.goto(f"http://127.0.0.1:{self.srv.server_address[1]}/app.html?nosb=1"); pg.wait_for_timeout(2500)
        r = pg.evaluate("""()=>{const a=document.getElementById('hdr-trig-lock').getBoundingClientRect(),b=document.getElementById('hdr-trig-settle').getBoundingClientRect();
          return {aRight:a.right,bLeft:b.left,bRight:b.right,vw:innerWidth,aVis:a.width>0,bVis:b.width>0,hdrH:document.getElementById('hdr').getBoundingClientRect().height,sw:document.documentElement.scrollWidth}}""")
        self.assertTrue(r["aVis"] and r["bVis"], r)
        self.assertLess(r["aRight"], r["bLeft"], r)                       # the two buttons never overlap
        self.assertLessEqual(r["bRight"], r["vw"] + 1, r)
        self.assertLessEqual(r["sw"], r["vw"] + 1, r)                     # no horizontal page scroll
        pg.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
