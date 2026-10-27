#!/usr/bin/env python3
"""Auto-refresh scroll guard (owner report 2026-10-27: a background refresh sent the reader back to the top, mobile and desktop).

Renderers refresh a list by writing a short placeholder and filling the real content later; the placeholder collapses the pane and the browser clamps scrollTop to 0.  docs/app.html now holds the
position across innerHTML writes inside a scrolled `.sa` pane -- unless the user caused the change (tap / click / key in the last 6 s, wheel / touch in the last 1.2 s), and any user input drops the hold.
Synthetic panes (no network); one real-app case with the NBA tab's own render path.

    python3 scripts/test_scroll_guard.py
"""
import functools, http.server, socketserver, threading, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a, **k):
        pass


TALL = "'<div style=\"height:4000px\">x</div>'"
LOAD = "'<div class=\"loading\">FETCHING</div>'"

# one refresh cycle on a synthetic host inside a real .sa pane: scroll to 1500, optional user input, placeholder, wait, real content, wait -> final scrollTop
CYCLE = """async ({pre, between}) => {
  const sa = document.querySelector('.sa'); const host = document.createElement('div'); sa.appendChild(host);
  const wait = ms => new Promise(r => setTimeout(r, ms));
  host.innerHTML = %s; await wait(80); sa.scrollTop = 1500; await wait(150);
  if (pre) { eval(pre); }
  host.innerHTML = %s; await wait(150);
  if (between) { eval(between); }
  host.innerHTML = %s; await wait(500);
  const top = sa.scrollTop; host.remove(); return top;
}""" % (TALL, LOAD, TALL)


class Guard(unittest.TestCase):
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

    def page(self, w=1300, h=900):
        pg = self.b.new_page(viewport={"width": w, "height": h})
        pg.route("https://**/*", lambda r: r.abort())
        pg.goto(f"http://127.0.0.1:{self.srv.server_address[1]}/app.html?nosb=1&nosw=1")
        pg.wait_for_function("window._cvScrollGuard===1")
        pg.wait_for_timeout(1500)
        # an isolated scroll pane built exactly like the app's (class "sa" inside a flex column), so a placeholder really collapses it
        pg.evaluate("""() => { const wrap=document.createElement('div'); wrap.id='sg-wrap'; wrap.style.cssText='position:fixed;left:0;top:0;width:320px;height:600px;z-index:999999;display:flex;flex-direction:column;background:#000';
          const sa=document.createElement('div'); sa.className='sa'; sa.id='sg-sa'; const host=document.createElement('div'); sa.appendChild(host); wrap.appendChild(sa); document.body.appendChild(wrap); window.__c={sa,host}; }""")
        self.assertEqual(pg.evaluate("document.getElementById('sg-sa').clientHeight"), 600)
        return pg

    def fill(self, pg):
        pg.evaluate("window.__c.host.innerHTML=%s" % TALL)

    def placeholder(self, pg):
        pg.evaluate("window.__c.host.innerHTML=%s" % LOAD)

    def top(self, pg):
        return pg.evaluate("window.__c.sa.scrollTop")

    def cycle(self, pg, pre=None, between=None):
        """One refresh: content, scroll to 1500, [pre input], placeholder, [between input], real content -> final scrollTop.  pre / between are callables (real Playwright input = trusted events)."""
        self.fill(pg)
        pg.wait_for_timeout(100)
        pg.evaluate("window.__c.sa.scrollTop=1500")
        pg.wait_for_timeout(200)
        self.assertEqual(self.top(pg), 1500)
        if pre:
            pre(pg)
        self.placeholder(pg)
        pg.wait_for_timeout(150)
        if between:
            between(pg)
        self.fill(pg)
        pg.wait_for_timeout(500)
        return self.top(pg)

    @staticmethod
    def click(pg):
        pg.mouse.click(5, 5)
        pg.wait_for_timeout(100)

    @staticmethod
    def wheel(pg):
        pg.mouse.move(200, 300)
        pg.mouse.wheel(0, 5)
        pg.wait_for_timeout(150)                                   # let the input event reach the page before the next write

    def test_background_refresh_keeps_the_reading_position_desktop(self):
        pg = self.page()
        self.assertEqual(self.cycle(pg), 1500)

    def test_background_refresh_keeps_the_reading_position_mobile(self):
        pg = self.page(375, 812)
        self.assertEqual(self.cycle(pg), 1500)

    def test_without_the_guard_the_placeholder_would_reset_to_top(self):
        """Control: the same cycle written through a path that bypasses the guard's setter (insertAdjacentHTML) really does end at 0 -- the synthetic pane reproduces the bug."""
        pg = self.page()
        top = pg.evaluate("""async () => { const {sa,host}=window.__c; const w=ms=>new Promise(r=>setTimeout(r,ms));
          const raw=v=>{host.replaceChildren();host.insertAdjacentHTML('afterbegin',v);};
          raw(%s); await w(80); sa.scrollTop=1500; await w(150); raw(%s); await w(150); raw(%s); await w(300); return sa.scrollTop; }""" % (TALL, LOAD, TALL))
        self.assertEqual(top, 0)

    def test_a_tap_or_click_just_before_means_the_user_caused_it_so_nothing_is_held(self):
        pg = self.page()
        self.assertEqual(self.cycle(pg, pre=self.click), 0)

    def test_a_click_long_ago_does_not_stop_the_guard(self):
        pg = self.page()
        self.click(pg)
        pg.wait_for_timeout(6400)
        self.assertEqual(self.cycle(pg), 1500)

    def test_a_fresh_wheel_or_touch_means_the_user_is_scrolling_so_nothing_is_held(self):
        pg = self.page()
        self.assertEqual(self.cycle(pg, pre=self.wheel), 0)

    def test_user_scroll_input_during_a_hold_cancels_it(self):
        pg = self.page()
        self.assertEqual(self.cycle(pg, between=self.wheel), 0)

    def test_a_deliberate_scroll_reset_after_the_position_was_restored_is_not_undone(self):
        pg = self.page()
        self.assertEqual(self.cycle(pg), 1500)                       # restored
        pg.evaluate("window.__c.host.innerHTML=%s" % TALL)
        pg.evaluate("window.__c.sa.scrollTop=1500")
        pg.wait_for_timeout(100)
        self.placeholder(pg); pg.wait_for_timeout(100); self.fill(pg); pg.wait_for_timeout(300)
        self.assertEqual(self.top(pg), 1500)
        pg.evaluate("window.__c.sa.scrollTop=0")                      # code (not the user) scrolls to the top on purpose
        pg.wait_for_timeout(2500)
        self.assertEqual(self.top(pg), 0)

    def test_unscrolled_panes_and_writes_outside_any_pane_are_untouched(self):
        pg = self.page()
        r = pg.evaluate("""() => { const d=document.createElement('div'); document.body.appendChild(d); d.innerHTML='<b>hi</b>'; const ok=d.innerHTML==='<b>hi</b>'; d.remove();
          const sa=window.__c.sa; sa.scrollTop=0; const h=document.createElement('div'); sa.appendChild(h); h.innerHTML='<i>x</i>'; const ok2=h.innerHTML==='<i>x</i>'&&sa.scrollTop===0; h.remove(); return [ok, ok2]; }""")
        self.assertEqual(r, [True, True])

    def test_real_nba_refresh_path_keeps_position(self):
        """The reproduced bug: loadRemoteData(true) -> patchNBATonight -> renderNBAUpcoming wrote a placeholder, then the cards.  ESPN is canned here (same fixture the NBA tab test uses)."""
        import json
        fix = json.loads((ROOT / "scripts" / "fixtures" / "nba_scoreboard_20261021.json").read_text())
        pg = self.b.new_page(viewport={"width": 1300, "height": 900})
        hdr = {"access-control-allow-origin": "*", "content-type": "application/json"}

        def espn(route):
            u = route.request.url
            if "/basketball/nba/scoreboard" in u:
                return route.fulfill(status=200, headers=hdr, body=json.dumps(fix))
            if "site.api.espn.com" in u:
                return route.fulfill(status=200, headers=hdr, body=json.dumps({"events": [], "injuries": []}))
            return route.abort()
        pg.route("https://**/*", espn)
        pg.goto(f"http://127.0.0.1:{self.srv.server_address[1]}/app.html?nosb=1&nosw=1")
        pg.wait_for_function("typeof renderNBAUpcoming==='function'&&window._NBA_SCHED!==undefined")
        pg.wait_for_timeout(3000)
        pg.evaluate("(()=>{const s=document.getElementById('nba-filter-day');if(s){s.innerHTML='<option value=\"2026-10-21\">x</option>';s.dataset.key='2026-10-21';s.value='2026-10-21';}})()")
        pg.evaluate("navTap(document.querySelector(\"[onclick*=\\\"navTap(this,'nba')\\\"]\"),'nba')")
        pg.wait_for_timeout(500)
        pg.evaluate("renderNBAUpcoming(true)")
        pg.wait_for_function("document.querySelectorAll('#nba-upcoming .gc').length>1")
        pg.wait_for_timeout(500)
        # make sure there is something to scroll: pad the list so the pane is well taller than the screen
        pg.evaluate("(()=>{const u=document.getElementById('nba-upcoming');const pad=document.createElement('div');pad.className='gc';pad.style.height='3000px';u.appendChild(pad);})()")
        pg.evaluate("document.getElementById('nba-sa').scrollTop=800")
        pg.wait_for_timeout(300)
        before = pg.evaluate("document.getElementById('nba-sa').scrollTop")
        self.assertGreater(before, 500)
        pg.evaluate("renderNBAUpcoming(true)")                        # placeholder, then the freshly fetched cards (the padding is gone after the rebuild, 3 cards remain)
        pg.wait_for_function("document.querySelectorAll('#nba-upcoming .gc').length>1&&!document.querySelector('#nba-upcoming .loading')")
        pg.wait_for_timeout(1500)
        after = pg.evaluate("document.getElementById('nba-sa').scrollTop")
        h = pg.evaluate("(()=>{const e=document.getElementById('nba-sa');return e.scrollHeight-e.clientHeight})()")
        self.assertEqual(after, min(before, h))                       # held, or at the bottom when the rebuilt list is shorter than that position


if __name__ == "__main__":
    unittest.main(verbosity=2)
