#!/usr/bin/env python3
"""SCORES tab (owner request 2026-10-09): a grid of every game on one day across every league the live layer covers -- LIVE, UPCOMING and FINAL together -- with a day selector (prev / today / next / date
input), league chips, status chips (with counts), engine-pick markers, and an auto-refresh that only runs while the tab is on screen, the document is visible and the day is today.

ESPN is stubbed per league and per `?dates=` day (fixtures below); the European hockey schedule files are stubbed too; Date.now() is pinned to 2026-10-14 12:00 America/Denver.  Nothing else leaves
the machine (Google Fonts and every other host are aborted) and the page runs with ?nosb=1, so Supabase is never touched.

    python3 scripts/test_scores_tab.py
"""
import datetime as dt, functools, http.server, json, re, socketserver, threading, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NOW = dt.datetime(2026, 10, 14, 18, 0, tzinfo=dt.timezone.utc)          # 12:00 MDT, Wed Oct 14 2026
TODAY = "2026-10-14"


def iso(s):
    return s + ("Z" if len(s) == 16 else "")


def team(abbr, name=None):
    return {"abbreviation": abbr, "displayName": name or abbr, "logo": f"https://a.espncdn.com/i/teamlogos/x/500/{abbr.lower()}.png"}


def ev(eid, away, home, start, state, a=0, h=0, detail="", period=1, clock="0:00", stype=2):
    return {"id": eid, "date": iso(start), "season": {"type": stype},
            "competitions": [{"competitors": [{"homeAway": "home", "score": str(h), "team": team(*home) if isinstance(home, tuple) else team(home)},
                                              {"homeAway": "away", "score": str(a), "team": team(*away) if isinstance(away, tuple) else team(away)}],
                              "status": {"period": period, "displayClock": clock, "type": {"state": state, "shortDetail": detail}}}]}


# the `dates=` map: day -> {league path -> [events]}; the no-`dates` request is "today"
DAYS = {
    "20261014": {
        "hockey/nhl": [
            ev("n-final", "EDM", "CGY", "2026-10-14T16:00", "post", 1, 4, "Final/OT"),
            ev("n-live", "NYR", "BOS", "2026-10-14T17:00", "in", 2, 3, "12:34 - 2nd", 2, "12:34"),
            ev("n-pre", "TOR", "MTL", "2026-10-14T23:00", "pre", 0, 0, "7:00 PM EDT"),
        ],
        "basketball/nba": [
            ev("b-pre", "LAL", "BOS", "2026-10-15T01:00", "pre", 0, 0, "9:00 PM EDT"),
            ev("b-preseason", "PRE", "SEA", "2026-10-14T22:00", "pre", 0, 0, "", stype=1),        # NBA preseason is never tracked
        ],
        "soccer/eng.1": [ev("s-final", "x", "y", "2026-10-14T13:00", "post", 2, 1, "FT")],
    },
    "20261015": {
        "hockey/nhl": [ev("n2", "VGK", "SEA", "2026-10-16T02:00", "pre", 0, 0, "10:00 PM EDT")],
        "football/college-football": [ev("c2", "UGA", "AUB", "2026-10-15T23:00", "pre", 0, 0, "7:00 PM EDT")],
    },
    "20261013": {"hockey/nhl": [ev("n0", "SJ", "LA", "2026-10-14T02:00", "post", 3, 5, "Final")]},
}
# soccer teams use displayName
DAYS["20261014"]["soccer/eng.1"] = [ev("s-final", ("ARS", "Arsenal"), ("CHE", "Chelsea"), "2026-10-14T13:00", "post", 2, 1, "FT")]
DAYS["20261015"]["soccer/eng.1"] = [ev("s2", ("MCI", "Man City"), ("LIV", "Liverpool"), "2026-10-15T14:00", "pre", 0, 0, "")]


def euro(logo):
    return {"teams": {"a": {"name": "Ilves", "logo": logo}, "b": {"name": "Tappara", "logo": logo}, "c": {"name": "Lukko", "logo": logo}, "d": {"name": "Kalpa", "logo": logo}},
            "games": [
                {"id": "l1", "date": "2026-10-14T16:00Z", "home": "a", "homeName": "Ilves", "away": "b", "awayName": "Tappara", "state": "post", "homeScore": 4, "awayScore": 2},
                {"id": "l2", "date": "2026-10-15T15:00Z", "home": "c", "homeName": "Lukko", "away": "d", "awayName": "Kalpa", "state": "pre"},
            ]}


SHL = {"teams": {"a": {"name": "Frolunda"}, "b": {"name": "Lulea"}},
       "games": [{"id": "s1", "date": "2026-10-14T17:00Z", "home": "a", "homeName": "Frolunda", "away": "b", "awayName": "Lulea", "state": "pre"}]}   # started an hour ago, no result yet


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a, **k):
        pass


class Scores(unittest.TestCase):
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

    def page(self, width=1300, live_score=3):
        ctx = self.b.new_context(viewport={"width": width, "height": 900}, timezone_id="America/Los_Angeles", locale="en-US")
        pg = ctx.new_page()
        pg.set_default_timeout(60000)
        self.errors, self.espn, self.other = [], [], []
        self.live_score = live_score
        pg.on("pageerror", lambda e: self.errors.append(str(e)))

        def handle(route):
            url = route.request.url
            if url.startswith(f"http://127.0.0.1:{self.srv.server_address[1]}/"):
                if "liiga_schedule.json" in url:
                    return route.fulfill(status=200, content_type="application/json", body=json.dumps(euro("https://static.flashscore.com/res/image/data/x.png")))
                if "shl_schedule.json" in url:
                    return route.fulfill(status=200, content_type="application/json", body=json.dumps(SHL))
                if re.search(r"/(nla|extraliga)_schedule\.json", url):
                    return route.fulfill(status=200, content_type="application/json", body=json.dumps({"teams": {}, "games": []}))
                return route.continue_()
            m = re.match(r"https://site\.api\.espn\.com/apis/site/v2/sports/(.+?)/scoreboard(\?.*)?$", url)
            if m:
                path, q = m.group(1), m.group(2) or ""
                d = re.search(r"dates=(\d{8})", q)
                self.espn.append((path, d.group(1) if d else None))
                day = d.group(1) if d else "20261014"
                evs = json.loads(json.dumps(DAYS.get(day, {}).get(path, [])))
                if day == "20261014" and path == "hockey/nhl":
                    evs[1]["competitions"][0]["competitors"][0]["score"] = str(self.live_score)    # the live game's home score (BOS) moves between polls
                return route.fulfill(status=200, headers={"access-control-allow-origin": "*"}, content_type="application/json", body=json.dumps({"events": evs}))
            self.other.append(url)
            return route.abort()                                                                        # fonts, the live relay, anything else
        pg.route("**/*", handle)
        pg.clock.set_fixed_time(NOW)
        pg.goto(f"http://127.0.0.1:{self.srv.server_address[1]}/app.html?nosb=1", wait_until="domcontentloaded")
        pg.wait_for_function("typeof renderScores==='function'&&typeof saveP==='function'&&!!window.__LV&&__LV.ts>0")          # the live layer has fetched once
        pg.evaluate("Promise.all([loadLiigaScheduleData(),loadShlScheduleData(),loadNlaScheduleData(),loadExtraligaScheduleData()])")
        return pg

    def open_tab(self, pg):
        pg.click("#sbar .sp:has-text('SCORES')")
        pg.wait_for_selector("#sp-scores.spane.act .sc-card")

    def fetched(self, n):
        """ESPN requests since index n, minus the NBA tab's own opening-night preload (dates=20261020) which has nothing to do with SCORES."""
        return [r for r in self.espn[n:] if r != ("basketball/nba", "20261020")]

    def cards(self, pg):
        return pg.evaluate("[...document.querySelectorAll('#sc-root .sc-card')].map(c=>({lg:c.dataset.lg,b:c.dataset.b,t:c.innerText.replace(/\\n/g,' | ')}))")

    def pressed(self, pg):
        return pg.evaluate("[...document.querySelectorAll('#sc-root .sc-btn[aria-pressed=\"true\"]')].map(b=>b.innerText.replace(/\\s+/g,' ').trim())")

    # ── tab + grid ──────────────────────────────────────────────────────────────────────────────
    def test_tab_sits_right_after_home_and_shows_live_upcoming_and_final_together_in_order(self):
        pg = self.page()
        labels = pg.evaluate("[...document.querySelectorAll('#sbar .sp')].map(b=>b.textContent.trim())")
        self.assertEqual(labels[:2], ["HOME", "SCORES"])
        self.open_tab(pg)
        c = self.cards(pg)
        self.assertEqual([x["b"] for x in c], sorted([x["b"] for x in c], key=["in", "pre", "post"].index))        # LIVE, then UPCOMING, then FINAL
        self.assertEqual(c[0]["lg"], "NHL"); self.assertIn("NYR", c[0]["t"]); self.assertIn("P2 12:34", c[0]["t"])   # the live NHL game, with the ticker's period + clock text
        self.assertIn("2", c[0]["t"]); self.assertIn("3", c[0]["t"])
        pre = [x for x in c if x["b"] == "pre"]
        self.assertEqual([x["lg"] for x in pre][:2], ["NHL", "NBA"])                                              # by start time: 5pm MDT NHL puck drop before the 7pm MDT NBA tip
        self.assertEqual(sorted(x["lg"] for x in pre), ["NBA", "NHL"])
        self.assertIn("4:00 PM PDT", pre[0]["t"])                                                                 # the viewer's own time zone (Los Angeles here)
        fin = [x for x in c if x["b"] == "post"]
        self.assertEqual([x["lg"] for x in fin], ["NHL", "LIIGA", "PL"])                                          # FINALS: latest start first (NHL / Liiga tie -> app league order)
        self.assertIn("FINAL/OT", " ".join(x["t"] for x in fin)); self.assertIn("FINAL", " ".join(x["t"] for x in fin))
        self.assertEqual(pg.locator("#sc-root .sc-card.st-post .sc-row.lose").count(), 3)                          # the loser's row is dimmed on every decided final
        self.assertNotIn("PRE ", " ".join(x["t"] for x in c if x["lg"] == "NBA"))                                   # the NBA preseason event is dropped
        self.assertEqual(sum(x["lg"] == "NBA" for x in c), 1)
        pg.close()

    def test_european_hockey_is_included_with_the_helpers_states(self):
        pg = self.page()
        self.open_tab(pg)
        c = self.cards(pg)
        liiga = [x for x in c if x["lg"] == "LIIGA"]
        self.assertEqual(len(liiga), 1); self.assertEqual(liiga[0]["b"], "post"); self.assertIn("Tappara", liiga[0]["t"]); self.assertIn("FINAL", liiga[0]["t"]); self.assertIn("4", liiga[0]["t"])
        shl = [x for x in c if x["lg"] == "SHL"]
        self.assertEqual(len(shl), 1); self.assertEqual(shl[0]["b"], "in"); self.assertIn("IN PROGRESS", shl[0]["t"]); self.assertIn("SCORE PENDING", shl[0]["t"])   # no live score exists for it
        self.assertEqual(pg.locator("#sc-root .sc-card.st-in .sc-pulse").count(), 2)                                 # NHL live + SHL in progress
        pg.close()

    def test_team_logos_use_the_shared_helper_when_logos_are_on(self):
        pg = self.page()
        pg.evaluate("_TL.off=false")                                                                              # webdriver pages default to no logos
        self.open_tab(pg)
        pg.evaluate("renderScores(true)")
        pg.wait_for_function("document.querySelectorAll('#sc-root .tlg').length>0")
        self.assertTrue(pg.evaluate("[...document.querySelectorAll('#sc-root .sc-card')].every(c=>c.querySelectorAll('.tlg').length===2)"))
        pg.wait_for_timeout(500)                                                                                  # the images load from the logos' own hosts (aborted here), so a failed one falls back to its badge
        self.assertTrue(any("a.espncdn.com/combiner/i?img=/i/teamlogos/" in u for u in self.other), self.other)      # ESPN's payload logo, through the shared helper's 96px combiner
        self.assertTrue(any("static.flashscore.com/res/image/data/x.png" in u for u in self.other), self.other)    # Liiga's logo from its schedule file
        self.assertTrue(pg.evaluate("[...document.querySelectorAll('#sc-root .tlg .tlt')].every(t=>t.textContent.trim().length>0)"))   # badge text everywhere
        pg.close()

    def test_engine_picks_locked_on_a_game_are_marked(self):
        pg = self.page()
        pg.evaluate("""saveP([{id:'x1',sport:'NHL',betType:'ML',betOn:'NYR ML',hA:'BOS',awA:'NYR',date:'%s',outcome:'pending'},
                               {id:'x2',sport:'NHL',betType:'ML',betOn:'CGY ML',hA:'CGY',awA:'EDM',date:'%s',outcome:'win'},
                               {id:'x3',sport:'NHL',betType:'ML',betOn:'MTL ML',hA:'MTL',awA:'TOR',date:'2026-10-13',outcome:'pending'}])""" % (TODAY, TODAY))
        self.open_tab(pg)
        picks = pg.evaluate("[...document.querySelectorAll('#sc-root .sc-pick')].map(p=>p.closest('.sc-card').innerText.replace(/\\n/g,' ')+'::'+p.className)")
        self.assertEqual(len(picks), 2, picks)                                                                     # yesterday's pick on TOR @ MTL does not mark today's game
        self.assertTrue(any("NYR" in p and "LOCKED NYR ML" in p for p in picks)); self.assertTrue(any("CGY ML ✓" in p and p.endswith("sc-pick w") for p in picks))
        pg.close()

    # ── filters ─────────────────────────────────────────────────────────────────────────────────
    def test_league_and_status_chips_filter_with_counts_and_aria_pressed(self):
        pg = self.page()
        self.open_tab(pg)
        total = len(self.cards(pg))
        self.assertEqual(total, 7)                                                                                # NHL 3, NBA 1, PL 1, Liiga 1, SHL 1
        leagues = pg.evaluate("[...document.querySelectorAll('#sc-root [data-sc=lg]')].map(b=>b.dataset.v)")
        self.assertEqual(leagues, ["ALL", "NBA", "NHL", "PL", "LIIGA", "SHL"])                                       # only leagues with a game that day, app order
        self.assertEqual(pg.inner_text("#sc-root [data-sc=lg][data-v=ALL] .sc-n"), str(total))
        self.assertEqual([re.sub(r"\s+", "", t) for t in self.pressed(pg)], ["TODAY", f"ALLLEAGUES{total}", f"ALL{total}"])
        pg.click("#sc-root [data-sc=lg][data-v=NHL]")
        c = self.cards(pg)
        self.assertEqual({x["lg"] for x in c}, {"NHL"}); self.assertEqual(len(c), 3)
        self.assertEqual(pg.get_attribute("#sc-root [data-sc=lg][data-v=NHL]", "aria-pressed"), "true")
        self.assertEqual(pg.get_attribute("#sc-root [data-sc=lg][data-v=ALL]", "aria-pressed"), "false")
        self.assertEqual(pg.inner_text("#sc-root [data-sc=st][data-v=in] .sc-n"), "1")                               # status counts follow the league filter
        self.assertEqual(pg.inner_text("#sc-root [data-sc=st][data-v=post] .sc-n"), "1")
        pg.click("#sc-root [data-sc=st][data-v=in]")
        c = self.cards(pg)
        self.assertEqual([(x["lg"], x["b"]) for x in c], [("NHL", "in")])
        pg.click("#sc-root [data-sc=st][data-v=pre]")
        self.assertEqual([x["b"] for x in self.cards(pg)], ["pre"])
        pg.click("#sc-root [data-sc=lg][data-v=PL]")                                                               # PL has only a final: UPCOMING + PL = nothing
        self.assertEqual(len(self.cards(pg)), 0)
        self.assertIn("NO GAMES MATCH", pg.inner_text("#sc-root"))
        pg.click("#sc-root [data-sc=reset]")
        self.assertEqual(len(self.cards(pg)), total)
        self.assertEqual(pg.get_attribute("#sc-root [data-sc=st][data-v=ALL]", "aria-pressed"), "true")
        pg.close()

    # ── day navigation ──────────────────────────────────────────────────────────────────────────
    def test_next_prev_today_and_date_input_fetch_that_days_scoreboards(self):
        pg = self.page()
        self.open_tab(pg)
        self.assertEqual(pg.get_attribute("#sc-root [data-sc=today]", "aria-pressed"), "true")
        self.assertEqual(pg.input_value("#sc-date"), TODAY)
        n0 = len(self.espn)
        pg.click("#sc-root [aria-label='Next day']")
        pg.wait_for_function("document.querySelector('#sc-date').value==='2026-10-15'&&document.querySelectorAll('#sc-root .sc-card').length>0")
        self.assertEqual(pg.get_attribute("#sc-root [data-sc=today]", "aria-pressed"), "false")
        self.assertIn("THU, OCT 15", pg.inner_text("#sc-root .sc-day"))
        self.assertTrue({d for p, d in self.fetched(n0)} == {"20261015"} and len({p for p, d in self.fetched(n0)}) == 8)  # one dated request per ESPN league
        c = self.cards(pg)
        self.assertEqual({x["lg"] for x in c}, {"NHL", "CFB", "PL", "LIIGA"})                                       # ESPN day + the Liiga game from the schedule file
        self.assertTrue(all(x["b"] == "pre" for x in c))
        self.assertTrue(any("VGK" in x["t"] for x in c))
        pg.click("#sc-root [aria-label='Previous day']")
        pg.click("#sc-root [aria-label='Previous day']")                                                           # -> Oct 13
        pg.wait_for_function("document.querySelector('#sc-date').value==='2026-10-13'&&document.querySelectorAll('#sc-root .sc-card').length===1")
        self.assertEqual(self.cards(pg)[0]["b"], "post"); self.assertIn("SJ", self.cards(pg)[0]["t"])
        pg.click("#sc-root [data-sc=today]")
        pg.wait_for_function(f"document.querySelector('#sc-date').value==='{TODAY}'&&document.querySelectorAll('#sc-root .sc-card').length>3")
        self.assertEqual(pg.get_attribute("#sc-root [data-sc=today]", "aria-pressed"), "true")
        n2 = len(self.espn)
        pg.click("#sc-root [aria-label='Next day']"); pg.wait_for_function("document.querySelector('#sc-date').value==='2026-10-15'")
        self.assertEqual(self.fetched(n2), [])                                                                       # Oct 15 is cached (5 min): no second fetch
        pg.fill("#sc-date", "2026-10-20")
        pg.wait_for_function("document.querySelector('#sc-root .sc-empty')")
        self.assertIn("NO GAMES SCHEDULED FOR TUE, OCT 20", pg.inner_text("#sc-root"))
        self.assertEqual(pg.locator("#sc-root [data-sc=lg]").count(), 1)                                            # only ALL when nothing is scheduled
        pg.close()

    def test_a_league_filter_that_the_new_day_does_not_have_resets_to_all(self):
        pg = self.page()
        self.open_tab(pg)
        pg.click("#sc-root [data-sc=lg][data-v=SHL]")
        pg.click("#sc-root [aria-label='Next day']")
        pg.wait_for_function("document.querySelector('#sc-date').value==='2026-10-15'&&document.querySelectorAll('#sc-root .sc-card').length>0")
        self.assertEqual(pg.get_attribute("#sc-root [data-sc=lg][data-v=ALL]", "aria-pressed"), "true")
        pg.close()

    # ── refresh ─────────────────────────────────────────────────────────────────────────────────
    def test_live_layer_updates_repaint_the_open_tab_and_the_timer_only_runs_when_visible_and_today(self):
        pg = self.page()
        self.open_tab(pg)
        self.assertEqual(pg.evaluate("typeof window._scTick"), "function")
        self.live_score = 7
        n = len(self.espn)
        pg.evaluate("window._scTick()")                                                                           # the 90 s tick: a live NHL game -> only NHL is re-fetched, through the live layer
        pg.wait_for_function("[...document.querySelectorAll('#sc-root .sc-card.st-in')].some(c=>c.innerText.includes('7'))")
        self.assertEqual(set(self.fetched(n)), {("hockey/nhl", None)})
        # a different day: nothing is fetched or repainted
        pg.click("#sc-root [aria-label='Next day']"); pg.wait_for_function("document.querySelector('#sc-date').value==='2026-10-15'&&document.querySelectorAll('#sc-root .sc-card').length>0")
        n = len(self.espn); pg.evaluate("window._scTick()"); pg.wait_for_timeout(400)
        self.assertEqual(self.fetched(n), [])
        pg.click("#sc-root [data-sc=today]")
        pg.wait_for_function("document.querySelectorAll('#sc-root .sc-card.st-in').length>0")
        # tab not selected / document hidden: nothing is fetched
        pg.evaluate("SS('home')"); n = len(self.espn); pg.evaluate("window._scTick()"); pg.wait_for_timeout(400)
        self.assertEqual(self.fetched(n), [])
        pg.evaluate("SS('scores')")
        pg.evaluate("Object.defineProperty(document,'hidden',{configurable:true,get:()=>true})")
        n = len(self.espn); pg.evaluate("window._scTick()"); pg.wait_for_timeout(400)
        self.assertEqual(self.fetched(n), [])
        self.assertEqual(self.errors, [])
        pg.close()

    def test_refresh_button_refetches_every_league_for_that_day(self):
        pg = self.page()
        self.open_tab(pg)
        n = len(self.espn)
        pg.click("#sc-root [data-sc=refresh]")
        pg.wait_for_timeout(800)
        self.assertEqual(len({p for p, d in self.espn[n:]}), 8)
        self.assertEqual(pg.evaluate("document.activeElement.dataset.scf"), "refresh0")                              # focus survives the repaint
        pg.close()

    # ── layout / a11y ───────────────────────────────────────────────────────────────────────────
    def test_phone_width_has_no_horizontal_overflow_and_desktop_uses_a_multi_column_grid(self):
        pg = self.page(width=390)
        self.open_tab(pg)
        pg.wait_for_timeout(500)
        r = pg.evaluate("""()=>{const sa=document.querySelector('#sp-scores .sa'),root=document.getElementById('sc-root'),W=document.documentElement.clientWidth;
          const over=[...root.querySelectorAll('*')].filter(e=>e.getBoundingClientRect().right>W+0.5).map(e=>e.className||e.tagName);
          const cols=getComputedStyle(root.querySelector('.sc-grid')).gridTemplateColumns.split(' ').length;
          return {docW:document.documentElement.scrollWidth,W,saW:sa.scrollWidth,saC:sa.clientWidth,over,cols,tap:Math.min(...[...root.querySelectorAll('.sc-btn')].map(b=>b.getBoundingClientRect().height))}}""")
        self.assertLessEqual(r["docW"], r["W"]); self.assertLessEqual(r["saW"], r["saC"]); self.assertEqual(r["over"], [])
        self.assertEqual(r["cols"], 2); self.assertGreaterEqual(r["tap"], 30)
        pg.close()
        pg = self.page(width=1300)
        self.open_tab(pg)
        self.assertGreaterEqual(pg.evaluate("getComputedStyle(document.querySelector('#sc-root .sc-grid')).gridTemplateColumns.split(' ').length"), 5)
        pg.close()

    def test_controls_are_real_buttons_with_focus_styles_and_the_pulse_respects_reduced_motion(self):
        pg = self.page()
        self.open_tab(pg)
        self.assertEqual(pg.evaluate("[...document.querySelectorAll('#sc-root [data-sc]')].filter(e=>e.tagName!=='BUTTON').length"), 0)
        self.assertEqual(pg.evaluate("[...document.querySelectorAll('#sc-root .sc-chips button')].filter(b=>!b.hasAttribute('aria-pressed')).length"), 0)
        pg.keyboard.press("Tab")
        pg.focus("#sc-root [data-sc=lg][data-v=NHL]")
        self.assertNotEqual(pg.evaluate("getComputedStyle(document.activeElement).outlineStyle"), "none")
        pg.keyboard.press("Enter")
        self.assertEqual({x["lg"] for x in self.cards(pg)}, {"NHL"})
        self.assertEqual(pg.evaluate("document.activeElement.dataset.scf"), "lg:NHL")
        self.assertNotEqual(pg.evaluate("getComputedStyle(document.querySelector('.sc-pulse')).animationName"), "none")
        pg.close()
        ctx = self.b.new_context(viewport={"width": 1300, "height": 900}, reduced_motion="reduce")
        pg = ctx.new_page(); pg.set_default_timeout(60000)
        pg.route("**/*", lambda r: r.continue_() if r.request.url.startswith(f"http://127.0.0.1:{self.srv.server_address[1]}/") else r.abort())
        pg.goto(f"http://127.0.0.1:{self.srv.server_address[1]}/app.html?nosb=1", wait_until="domcontentloaded")
        pg.wait_for_function("typeof renderScores==='function'")
        pg.evaluate("document.body.insertAdjacentHTML('beforeend','<span class=\"sc-pulse\" id=\"tp\"></span>')")
        self.assertEqual(pg.evaluate("getComputedStyle(document.getElementById('tp')).animationName"), "none")
        ctx.close()

    def test_no_page_errors_and_no_supabase_requests(self):
        pg = self.page()
        self.open_tab(pg)
        for sel in ["[data-sc=lg][data-v=NBA]", "[data-sc=st][data-v=post]", "[data-sc=reset]"]:
            pg.click("#sc-root " + sel)
        pg.click("#sc-root [aria-label='Next day']")
        self.assertEqual(self.errors, [])
        self.assertFalse([u for u in self.other if "supabase" in u])
        pg.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
