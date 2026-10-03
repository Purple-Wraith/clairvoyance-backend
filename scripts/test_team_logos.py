#!/usr/bin/env python3
"""Checks for the in-app team-logo feature (docs/app.html _teamLogoHTML).  Localhost only -- no Supabase, no email, no real network
(every non-localhost request is ABORTED and recorded, so a logo <img> that tries to load is observable but never actually fetches).

    python3 scripts/test_team_logos.py

What it proves
  1. STATIC / PUBLIC-REPO: no image file or data: URI was added for logos; docs/team_logos.json is URL strings on ESPN's CDN only; the
     schedule JSONs' teams[id].logo are Flashscore static-CDN URLs only.
  2. PUBLIC-LEAK GUARD: logos never reach exported/public output.  _teamLogoHTML is called ONLY from the in-app game-card functions
     (allow-list below) -- not from renderTrackRecord, the sport/league/event cards, any _gen* graphic; no email builder / social
     generator / landing-page writer in scripts/ references a logo URL or the helper.
  3. AUTOMATION NO-OP: in a headless-driven browser (navigator.webdriver, exactly what the lock/settle pass and social generators run)
     and with the pipeline's explicit ?nologo=1, real hockey / CFB cards render with ZERO logo markup, zero <img>, and not one
     request to a logo host or team_logos.json.  Positive control: a plain ?logos=1 load of the same cards does render logos.
  4. FAIL SOFT: unknown team -> 3-letter badge, no <img>; a logo that fails to load leaves the badge (no broken image, no hole).
  5. MOBILE: at 375px the rendered hockey card has no horizontal overflow and every logo box has a fixed size.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import auto_lock_settle as A  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
APP = (DOCS / "app.html").read_text()

# The ONLY functions allowed to render a logo: interactive in-app game cards.
ALLOWED_CALLERS = {
    "renderNHLTonight", "renderNHLGamesOffline", "_nhlGameCard", "_nhlUpcomingCard", "_nbaGameCard", "_cfbGameCard", "_nflGameCard2",
    "_renderSocMatchCard", "_liigaMatchCard", "_shlMatchCard", "_nlaMatchCard", "_extraligaMatchCard",
}
LOGO_HOSTS = ("a.espncdn.com", "static.flashscore.com")
PNG_1X1 = bytes.fromhex("89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000d49444154789c6360f8cfc0f00f0002c60180e5e7b4b30000000049454e44ae426082")
IMG_EXT = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico")

NAV = {
    "liiga": "navTap(document.querySelector('#sbar *[onclick*=\"\\'hk\\'\"]'),'hk');setSub('hk','liiga');T('liiga','matches');",
    "nhl": "navTap(document.querySelector('#sbar *[onclick*=\"\\'hk\\'\"]'),'hk');setSub('hk','nhl');T('nhl','games');",
    "cfb": "navTap(document.querySelector('#sbar *[onclick*=\"\\'fb\\'\"]'),'fb');setSub('fb','cfb');T('cfb','games');",
}


def callers_of_helper() -> dict:
    cur, out = None, {}
    for line in APP.split("\n"):
        m = re.match(r"^(?:async )?function\s+([A-Za-z0-9_$]+)", line)
        if m:
            cur = m.group(1)
        if "_teamLogoHTML(" in line and not line.lstrip().startswith("//") and not line.startswith("function _teamLogoHTML"):
            out[cur] = out.get(cur, 0) + 1
    return out


class StaticChecks(unittest.TestCase):
    def test_no_logo_binaries_or_data_uris_committed(self):
        tracked = subprocess.run(["git", "ls-files", "docs"], cwd=ROOT, capture_output=True, text=True).stdout.split()
        # Images that already shipped before the logo feature (brand/social assets) are fine; a NEW image name that smells like a
        # team logo is not.
        bad = [f for f in tracked if f.lower().endswith(IMG_EXT) and re.search(r"logo|team|crest|badge", Path(f).name, re.I)
               and Path(f).name not in {"clairvoyance-logo.svg", "text_logo_icon.png"}]
        self.assertEqual(bad, [], f"team-logo image files must not be committed: {bad}")
        for f in ("team_logos.json", "liiga_schedule.json", "shl_schedule.json", "nla_schedule.json", "extraliga_schedule.json"):
            self.assertNotIn("data:image", (DOCS / f).read_text(), f)

    def test_team_logos_json_is_espn_url_strings_only(self):
        d = json.loads((DOCS / "team_logos.json").read_text())
        n = 0
        for sport in ("nhl", "nba", "nfl", "cfb", "soccer"):
            self.assertTrue(d.get(sport), sport)
            for k, u in d[sport].items():
                n += 1
                self.assertRegex(u, r"^https://a\.espncdn\.com/i/teamlogos/[A-Za-z0-9_/.\-]+\.png$", f"{sport}/{k}")
        self.assertGreater(n, 700)

    def test_schedule_logo_urls_are_flashscore_static_cdn(self):
        for lg in ("liiga", "shl", "nla", "extraliga"):
            d = json.loads((DOCS / f"{lg}_schedule.json").read_text())
            logos = [t.get("logo") for t in d["teams"].values()]
            self.assertTrue(all(logos), f"{lg}: every team should carry a logo URL ({sum(1 for x in logos if x)}/{len(logos)})")
            for u in logos:
                self.assertRegex(u, r"^https://static\.flashscore\.com/res/image/data/[A-Za-z0-9_-]+\.(png|jpg|jpeg|webp|svg)$")

    def test_helper_only_called_from_in_app_game_cards(self):
        used = set(callers_of_helper())
        self.assertTrue(used, "helper is not used anywhere")
        self.assertLessEqual(used, ALLOWED_CALLERS, f"_teamLogoHTML called from non-card code: {used - ALLOWED_CALLERS}")

    def test_exports_and_graphics_have_no_logo_path(self):
        # every exported/public renderer: none may mention the helper, an ESPN/Flashscore logo URL or the logo CSS class
        names = re.findall(r"^(?:async )?function\s+(renderTrackRecord|_gen[A-Za-z0-9_]*|_perfCard[A-Za-z0-9_]*|[A-Za-z0-9_]*(?:Graphic|SocialCard|ExportCard)[A-Za-z0-9_]*)\(", APP, re.M)
        self.assertIn("renderTrackRecord", names)
        for nm in set(names):
            i = APP.index(f"function {nm}(")
            j = APP.find("\nfunction ", i + 10)
            body = APP[i:j if j > 0 else len(APP)]
            for needle in ("_teamLogoHTML", "espncdn", "flashscore.com/res/image", 'class="tlg', "team_logos"):
                self.assertNotIn(needle, body, f"{nm} references {needle}")

    def test_no_script_or_email_builder_references_logos(self):
        allowed = {"build_team_logos.py", "_flashscore_logos.py", "test_team_logos.py", "fetch_liiga.py", "fetch_shl.py", "fetch_nla.py",
                   "fetch_extraliga.py", "auto_lock_settle.py", "generate_pick_of_day_social.py", "generate_social_cards.py",
                   "test_settle_pass_sim.py",
                   # PRE-EXISTING, unrelated to the card logos: stores ESPN's logo URL strings in docs/soccer_standings.json (URLs only,
                   # never rendered by the app, not an export/email/landing figure).
                   "scrape_soccer_standings.py",
                   # PRE-EXISTING too: NCAA-baseball rankings data row carries ESPN's logo href (data field, not an email/graphic).
                   "clairvoyance_update.py"}  # (the four before it only mention the nologo flag)
        pat = re.compile(r"espncdn|static\.flashscore|_teamLogoHTML|team_logos|[\"']logo[\"']\s*:|teamlogos", re.I)
        for f in (ROOT / "scripts").glob("*.py"):
            if f.name in allowed:
                continue
            for i, line in enumerate(f.read_text().split("\n"), 1):
                if pat.search(line):
                    self.fail(f"{f.name}:{i} references team logos: {line.strip()[:120]}")
        # the allow-listed pipeline/generator files may only mention the nologo flag
        for nm in ("auto_lock_settle.py", "generate_pick_of_day_social.py", "generate_social_cards.py", "test_settle_pass_sim.py"):
            txt = (ROOT / "scripts" / nm).read_text()
            self.assertIn("nologo", txt, nm)
            self.assertIsNone(re.search(r"espncdn|static\.flashscore", txt), f"{nm} must not reference a logo host")
        # the landing-page JSONs (published figures) carry no logo
        for f in DOCS.glob("*performance*.json"):
            self.assertNotRegex(f.read_text(), r"espncdn|static\.flashscore|\"logo\"", f.name)
        for f in ("engine_performance.json", "sport_performance.json", "social_copy.json"):
            if (DOCS / f).exists():
                self.assertNotRegex((DOCS / f).read_text(), r"espncdn|static\.flashscore|\"logo\"", f)


try:
    from playwright.sync_api import sync_playwright
except Exception:  # pragma: no cover
    sync_playwright = None


@unittest.skipIf(sync_playwright is None, "playwright not installed")
class BrowserChecks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.url, cls.srv = A.start_local_site(DOCS)
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch()  # a Playwright browser has navigator.webdriver === true, like the pipeline's

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()
        cls.srv.shutdown()
        cls.srv.server_close()

    def open(self, qs="", width=1280, hide_webdriver=False):
        ctx = self.browser.new_context(viewport={"width": width, "height": 900})
        self.addCleanup(ctx.close)
        if hide_webdriver:
            ctx.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>false})")
        page = ctx.new_page()
        page.logo_requests, page.aborted, page.fail_logos = [], [], False

        def gate(route):
            u = route.request.url
            if any(h in u for h in LOGO_HOSTS) or "team_logos.json" in u:
                page.logo_requests.append(u)
            if u.startswith("http://127.0.0.1") or u.startswith("data:") or u.startswith("blob:"):
                route.continue_()
            elif any(h in u for h in LOGO_HOSTS) and not page.fail_logos:
                route.fulfill(status=200, content_type="image/png", body=PNG_1X1)  # logo host stub: no real network
            else:
                page.aborted.append(u)
                route.abort()

        page.route("**/*", gate)
        page.goto(self.url + qs, wait_until="load", timeout=60000)
        page.wait_for_timeout(1500)
        return page

    def render(self, page, name):
        # the nav helpers are idempotent; a fresh page needs the second call for the sub-tab to be visible
        page.evaluate("()=>{" + NAV[name] + "}")
        page.wait_for_timeout(2500)
        page.evaluate("()=>{" + NAV[name] + "}")
        try:
            page.wait_for_function("[...document.querySelectorAll('.gc')].some(e=>e.offsetParent!==null)", timeout=30000)
        except Exception:
            pass
        page.wait_for_timeout(1500)

    def cards(self, page):
        return page.evaluate("()=>[...document.querySelectorAll('.gc')].filter(e=>e.offsetParent!==null).length")

    # ---- 3. automation no-op ----
    def _assert_noop(self, page, label):
        self.assertTrue(page.evaluate("_tlOff()"), label)
        self.assertEqual(page.evaluate("_teamLogoHTML('nhl','BOS','h',44,{url:'https://a.espncdn.com/i/teamlogos/nhl/500/bos.png'})"), "")
        for sport in ("liiga", "nhl", "cfb"):
            self.render(page, sport)
            self.assertGreater(self.cards(page), 0, f"{label}/{sport}: no real cards rendered, test would be vacuous")
            self.assertEqual(page.evaluate("document.querySelectorAll('.tlg, .gc img').length"), 0, f"{label}/{sport}: logo markup/img present")
        self.assertEqual(page.logo_requests, [], f"{label}: logo/team_logos.json requests made: {page.logo_requests[:5]}")

    def test_webdriver_context_renders_no_logos_and_requests_nothing(self):
        page = self.open()  # no flag at all: navigator.webdriver is true here
        self.assertTrue(page.evaluate("navigator.webdriver"))
        self._assert_noop(page, "webdriver")

    def test_pipeline_flag_wins_even_when_webdriver_is_hidden(self):
        page = self.open("?nologo=1", hide_webdriver=True)
        self.assertFalse(page.evaluate("navigator.webdriver"))
        self._assert_noop(page, "nologo=1")

    def test_pipeline_url_helper_adds_flag(self):
        self.assertTrue(A.with_nologo("https://x/app.html").endswith("?nologo=1"))
        self.assertTrue(A.with_nologo("http://127.0.0.1:1/app.html?a=1").endswith("&nologo=1"))
        self.assertEqual(A.with_nologo("http://h/app.html?nologo=1"), "http://h/app.html?nologo=1")

    # ---- positive control + 4. fail soft ----
    def test_logos_render_when_not_automated(self):
        page = self.open("?logos=1")
        self.assertFalse(page.evaluate("_tlOff()"))
        self.render(page, "liiga")
        self.assertGreater(self.cards(page), 0)
        info = page.evaluate("""()=>{const t=[...document.querySelectorAll('.gc')].filter(g=>g.offsetParent!==null).flatMap(g=>[...g.querySelectorAll('.tlg')]);
          return {n:t.length, imgs:t.filter(e=>e.querySelector('img')).length,
                  hosts:[...new Set(t.map(e=>e.querySelector('img')).filter(Boolean).map(i=>new URL(i.src).host))],
                  lazy:t.every(e=>!e.querySelector('img')||(e.querySelector('img').loading==='lazy'&&e.querySelector('img').decoding==='async')),
                  fixed:t.filter(e=>e.offsetParent!==null).every(e=>{const r=e.getBoundingClientRect();return r.width>=22&&r.height>=22&&Math.abs(r.width-r.height)<.6;})}}""")
        self.assertGreater(info["n"], 0)
        self.assertEqual(info["imgs"], info["n"], "every Liiga team has a logo URL in the schedule JSON")
        self.assertEqual(info["hosts"], ["static.flashscore.com"])
        self.assertTrue(info["lazy"] and info["fixed"])

    def test_unknown_team_gets_badge_and_failed_load_falls_back(self):
        page = self.open("?logos=1")
        page.fail_logos = True
        html = page.evaluate("_teamLogoHTML('nhl','ZZZ','a',44,{name:'Zed Team'})")
        self.assertIn('class="tlg a s44"', html)
        self.assertIn(">ZZZ<", html)
        self.assertNotIn("<img", html)
        # Flashscore-id key (not an abbreviation) + no logo URL -> badge from the first 3 letters of the name
        long_key = page.evaluate("_teamLogoHTML('liiga','Oh5uGzDT','a',44,{name:'Zed Team'})")
        self.assertIn(">ZED<", long_key)
        self.assertNotIn("<img", long_key)
        # a hint URL on any other host is rejected (no injection through a data file) -> badge only
        self.assertNotIn("<img", page.evaluate("_teamLogoHTML('nhl','QQQ','h',44,{url:'https://evil.example/x.png'})"))
        # failed load: the gate aborts every non-localhost request, so the <img> errors -> retry raw -> removed -> badge remains
        page.evaluate("""()=>{const d=document.createElement('div');d.id='tlt';document.body.appendChild(d);
          d.innerHTML=_teamLogoHTML('nhl','BOS','h',44,{url:'https://a.espncdn.com/i/teamlogos/nhl/500/bos.png'})}""")
        page.wait_for_timeout(2500)
        st = page.evaluate("""()=>{const e=document.querySelector('#tlt .tlg');return {img:!!e.querySelector('img'),ok:e.classList.contains('ok'),
           txt:getComputedStyle(e.querySelector('.tlt')).visibility, text:e.querySelector('.tlt').textContent, w:e.getBoundingClientRect().width}}""")
        self.assertEqual(st, {"img": False, "ok": False, "txt": "visible", "text": "BOS", "w": 44})

    # ---- 5. mobile ----
    def test_mobile_375_no_horizontal_overflow(self):
        page = self.open("?logos=1", width=375)
        for sport in ("liiga", "nhl", "cfb"):
            self.render(page, sport)
            self.assertGreater(self.cards(page), 0, sport)
            m = page.evaluate("()=>({sw:document.documentElement.scrollWidth,cw:document.documentElement.clientWidth})")
            self.assertLessEqual(m["sw"], m["cw"], f"{sport}: horizontal overflow {m}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
