#!/usr/bin/env python3
"""The MOBILE site (Purple-Wraith/Clairvoyance-backend-mobile = docs/app.html run through scripts/mobile_transform.py, which injects !important phone overrides) carries and runs the new features:
adjusted-line options (both sides, magenta best line, BEST ADJUSTED LINE strip), tap-to-lock on a touch device, the game tracker overlay, and the Top Adjusted Lines tab -- at 390px and 320px wide, with
no sideways overflow and thumb-sized tap targets.  The real transform is applied to the real app.html and served over docs/'s data files (Playwright route), so this fails if a change in app.html
breaks under the mobile overrides.

    python3 scripts/test_mobile_site.py
"""
import functools, http.server, os, re, socketserver, subprocess, sys, tempfile, threading, unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = Path(os.environ.get("CV_ROOT") or HERE.parent)
sys.path.insert(0, str(HERE))
import test_game_tracker as TG  # noqa: E402
import test_nba_alt_both_sides as TB  # noqa: E402

UA = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 Mobile/15E148"
OVERFLOW = """(sel)=>{const root=document.querySelector(sel)||document.body,vw=document.documentElement.clientWidth,bad=[];
 root.querySelectorAll('*').forEach(e=>{const r=e.getBoundingClientRect();if(r.width&&r.right>vw+1&&getComputedStyle(e).position!=='fixed'){let p=e.parentElement,sc=false;while(p&&p!==root.parentElement){const o=getComputedStyle(p).overflowX;if(o==='auto'||o==='scroll'||o==='hidden'){sc=true;break}p=p.parentElement}if(!sc)bad.push((e.className||e.tagName)+':'+Math.round(r.right))}});
 return{docW:document.documentElement.scrollWidth,vw,bad:bad.slice(0,5)}}"""


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a, **k):
        pass


class Mobile(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        cls.tmp = tempfile.mkdtemp()
        out = Path(cls.tmp) / "app.html"
        subprocess.run([sys.executable, str(ROOT / "scripts" / "mobile_transform.py"), str(ROOT / "docs" / "app.html"), str(out)], check=True, capture_output=True)
        cls.html = out.read_text()
        socketserver.TCPServer.allow_reuse_address = True
        cls.srv = socketserver.TCPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=str(ROOT / "docs")))
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.pw = sync_playwright().start(); cls.b = cls.pw.chromium.launch()
        cls.errs = []

    @classmethod
    def tearDownClass(cls):
        cls.b.close(); cls.pw.stop(); cls.srv.shutdown()

    def page(self, w):
        ctx = self.b.new_context(viewport={"width": w, "height": 740}, device_scale_factor=2, is_mobile=True, has_touch=True, user_agent=UA)
        pg = ctx.new_page()
        pg.on("pageerror", lambda e: self.errs.append(str(e)[:200]))
        pg.route("**/*espn.com/**", lambda r: r.abort()); pg.route("**/*workers.dev/**", lambda r: r.abort())
        pg.route(re.compile(r".*/(app|index)\.html(\?.*)?$"), lambda r: r.fulfill(status=200, content_type="text/html", body=self.html))
        pg.goto(f"http://127.0.0.1:{self.srv.server_address[1]}/app.html?nosb=1")
        pg.wait_for_function("typeof _altRowFor==='function'&&typeof _gtOpen==='function'"); pg.wait_for_timeout(1200)
        return pg

    def test_the_transform_really_is_in_effect(self):
        self.assertIn("MOBILE-ONLY OVERRIDES", self.html); self.assertIn("CLAIRVOYANCE MOBILE", self.html); self.assertIn("user-scalable=no", self.html)

    def chips(self, pg):
        mk = [dict(m, label=m["label"].replace("236.5", "45.5")) for m in TB.MKTS]
        pg.evaluate("""(m)=>{saveP([]);window._altReg={};document.getElementById('tst')?.remove();SS('home');const d=document.createElement('div');d.id='tst';d.style.cssText='padding:8px';
          d.innerHTML='<div class=gc>'+_altRowFor('CFB','PHI','CLE','2026-12-30T01:00Z',{mkts:m},false)+'</div>';document.querySelector('.spane.act').prepend(d);window.scrollTo(0,0)}""", mk)
        pg.wait_for_timeout(200)

    def test_adjusted_line_options_fit_glow_and_lock_by_tap_at_phone_widths(self):
        for w in (390, 320):
            pg = self.page(w)
            self.chips(pg)
            o = pg.evaluate(OVERFLOW, "#tst")
            self.assertEqual((o["docW"], o["bad"]), (w, []), (w, o))
            self.assertEqual(pg.evaluate("document.querySelectorAll('#tst .altc').length"), 4)
            self.assertEqual(pg.evaluate("document.querySelectorAll('#tst .altc.alt-top').length"), 1)
            self.assertIn("rgba(240, 0, 255", pg.evaluate("getComputedStyle(document.querySelector('#tst .altc.alt-top')).boxShadow"))      # the glow survives the !important overrides
            self.assertEqual(pg.evaluate("document.querySelectorAll('#tst .bmk-adj').length"), 1)
            for sel in ("#tst .altc >> nth=1", "#tst .bmk-adj"):
                h = pg.evaluate("(s)=>{const e=document.querySelector(s.split(' >> ')[0]);return Math.round(e.getBoundingClientRect().height)}", sel)
                self.assertGreaterEqual(h, 44, (w, sel))                                               # thumb-sized
            pg.locator("#tst .altc").nth(1).tap(); pg.wait_for_timeout(500)
            self.assertEqual(pg.evaluate("getP().filter(p=>p.outcome==='pending').length"), 1, w)
            pg.evaluate("saveP([])"); self.chips(pg)
            pg.locator("#tst .bmk-adj").tap(); pg.wait_for_timeout(500)
            self.assertEqual(pg.evaluate("getP().filter(p=>p.outcome==='pending').map(p=>p.betOn)"), [pg.evaluate("document.querySelector('#tst .altc.alt-top .altp').innerText")], w)
            pg.context.close()

    def test_the_game_tracker_fits_on_the_mobile_build(self):
        lv = TG.live(TG.fx("nba"), 3, "6:00", 52, 58)
        picks = [TG.pick("OVER 215.5", betType="OU", winProb=.55), TG.pick("Joel Embiid OVER PTS 22.5", betType="PROP", team="PHI", winProb=.6)]
        for w in (390, 320):
            pg = self.page(w)
            pg.evaluate("""([sum,picks])=>{window.__sum=sum;window.__f0=window.fetch;window.fetch=async(u,o)=>{u=String(u);if(u.includes('/summary?event='))return new Response(JSON.stringify(window.__sum),{status:200});return window.__f0(u,o)};saveP(picks);_gtOpen('nba','E1')}""", [lv, picks])
            pg.wait_for_selector("#gt-ov .gt-pk", timeout=8000)
            for tab in ("picks", "plays", "box", "stats"):
                pg.evaluate("(t)=>_gtSet('tab',t)", tab)
                o = pg.evaluate(OVERFLOW, "#gt-ov")
                self.assertEqual((o["docW"], o["bad"]), (w, []), (w, tab, o))
            self.assertGreaterEqual(pg.evaluate("document.querySelector('#gt-ov .gt-x').getBoundingClientRect().height"), 38)
            pg.context.close()

    def test_top_adjusted_lines_fits_and_its_lock_buttons_are_thumb_sized(self):
        import test_top_adj_lines as TA
        for w in (390, 320):
            pg = self.page(w)
            ents = [TA.e("CFB", f"H{i}", f"A{i}", f"OVER {40+i}.5", .9 - i * .01) for i in range(12)]
            pg.evaluate(TA.SEED, [ents, [], []]); pg.evaluate("SS('toppicks');tpSub('adj')"); pg.wait_for_timeout(400)
            o = pg.evaluate(OVERFLOW, "#toppicks-adj")
            self.assertEqual((o["docW"], o["bad"]), (w, []), (w, o))
            self.assertGreaterEqual(pg.evaluate("document.querySelector('#toppicks-adj .tp-adj-lock').getBoundingClientRect().height"), 38)
            pg.context.close()

    def test_no_script_errors(self):
        self.assertEqual(self.errs, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
