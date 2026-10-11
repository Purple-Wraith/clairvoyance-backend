#!/usr/bin/env python3
"""Splash screen minimum time (owner 2026-10-10: "splashscreen loads too quick on mobile that clairvoyance is not able to fully get typed out"): a normal dismissal -- the end-of-init endSplash()
and the global JS-error handler -- waits until the wordmark has finished typing; a tap on the splash still skips it; automated browsers (navigator.webdriver) are not slowed.

    python3 scripts/test_splash_min.py
"""
import functools, http.server, re, socketserver, threading, time, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NOT_AUTOMATED = "Object.defineProperty(Navigator.prototype,'webdriver',{get:()=>false});"          # what a real phone reports


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a, **k):
        pass


class Splash(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        socketserver.TCPServer.allow_reuse_address = True
        cls.srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=str(ROOT / "docs")))
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.pw = sync_playwright().start()
        cls.b = cls.pw.chromium.launch()

    @classmethod
    def tearDownClass(cls):
        cls.b.close(); cls.pw.stop(); cls.srv.shutdown()

    def open(self, real_phone, width=390):
        ctx = self.b.new_context(viewport={"width": width, "height": 800}, has_touch=True)
        if real_phone:
            ctx.add_init_script(NOT_AUTOMATED)
        pg = ctx.new_page()
        pg.route(re.compile(r"https://fonts\.(googleapis|gstatic)\.com/.*"), lambda r: r.abort())
        t0 = time.time()
        pg.goto("http://127.0.0.1:%d/app.html?nosb=1" % self.srv.server_address[1], wait_until="domcontentloaded", timeout=90000)
        pg.wait_for_function("typeof startSplash==='function'&&typeof _splashT0==='number'&&_splashT0>0", timeout=90000)
        return pg, ctx

    def state(self, pg):
        return pg.evaluate("""()=>{const sp=document.getElementById('splash');const cs=getComputedStyle(sp);
          const L=[...document.querySelectorAll('#splash-wordmark span')];
          return {shown:cs.display!=='none'&&parseFloat(cs.opacity)>0.5,letters:L.length,typed:L.filter(l=>parseFloat(getComputedStyle(l).opacity)>0.95).length,done:splashDone,since:Date.now()-_splashT0}}""")

    def test_a_real_phone_sees_the_whole_word_typed_before_the_splash_goes(self):
        pg, ctx = self.open(real_phone=True)
        pg.evaluate("endSplash()")                                    # what the end of init does -- far earlier than the wordmark can finish
        pg.evaluate("window.dispatchEvent(new ErrorEvent('error',{message:'unrelated script error'}))")     # and the global error handler's 100 ms dismissal
        pg.wait_for_timeout(1500)
        st = self.state(pg)
        self.assertTrue(st["shown"], st)                              # still on screen well before the word is typed
        self.assertFalse(st["done"], st)
        pg.wait_for_function("document.querySelectorAll('#splash-wordmark span').length===12", timeout=8000)
        pg.wait_for_function("[...document.querySelectorAll('#splash-wordmark span')].every(l=>parseFloat(getComputedStyle(l).opacity)>0.95)", timeout=8000)
        st = self.state(pg)
        self.assertEqual((st["letters"], st["typed"]), (12, 12), st)  # CLAIRVOYANCE fully typed out
        self.assertTrue(st["shown"], st)                              # and still showing (subtitle + bar after the word)
        pg.wait_for_function("splashDone===true", timeout=12000)
        self.assertGreaterEqual(self.state(pg)["since"], 4400)
        ctx.close()

    def test_a_tap_still_skips_the_splash(self):
        pg, ctx = self.open(real_phone=True)
        pg.wait_for_timeout(600)
        pg.evaluate("document.getElementById('splash').click()")
        pg.wait_for_function("splashDone===true", timeout=3000)
        ctx.close()

    def test_automated_browsers_are_not_delayed(self):
        pg, ctx = self.open(real_phone=False)
        pg.evaluate("endSplash()")
        self.assertTrue(pg.evaluate("splashDone"))
        ctx.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
