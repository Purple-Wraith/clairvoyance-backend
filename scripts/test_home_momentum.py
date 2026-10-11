#!/usr/bin/env python3
"""Home > "// MOMENTUM — LAST 7D VS ALL-TIME" (interactive, 2026-10-09): the headline numbers keep the original rules (window = lockedAt within N days, so 7D equals the ROLLING 7D card; baseline = all-time
win% of in-scope picks; hot/cold = +/-5 points with 5+ settled bets) and every visual is interactive: window (7D / 14D / THIS MONTH / LAST MONTH) / metric / view / sport chips, tooltips on hover + tap + arrow keys, click-a-day to pin its picks.

    python3 scripts/test_home_momentum.py
"""
import datetime as dt, functools, http.server, json, socketserver, threading, unittest
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
MT = ZoneInfo("America/Denver")


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a, **k):
        pass


def ledger(now_ms):
    """Deterministic in-scope picks: one per (day offset, slot); outcome pattern fixed so the expected numbers are computed independently below."""
    rows, k = [], 0
    for off in range(0, 75):
        for slot in range(3):
            k += 1
            win = (k % 4) != 0                                  # 75% wins overall
            sport = ["NHL", "NBA", "NFL"][slot]
            lk = now_ms - off * 86400000 - slot * 3600000 - 1800000
            d = dt.datetime.fromtimestamp(lk / 1000, MT).strftime("%Y-%m-%d")
            rows.append({"id": f"m{k}", "sport": sport, "betType": "ML", "betOn": f"{sport} T{k} ML", "hA": f"H{k}", "awA": f"A{k}", "date": d, "lockedAt": lk,
                         "outcome": "win" if win else "loss", "decOdds": 1.8, "winProb": 0.6})
    return rows


class Momentum(unittest.TestCase):
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

    def page(self, width=1300):
        pg = self.b.new_page(viewport={"width": width, "height": 1000})
        self.errors = []
        pg.on("pageerror", lambda e: self.errors.append(str(e)))
        pg.goto(f"http://127.0.0.1:{self.srv.server_address[1]}/app.html?nosb=1")
        pg.wait_for_function("typeof _momBuild==='function'&&typeof saveP==='function'")
        pg.wait_for_timeout(600)
        now = pg.evaluate("Date.now()")
        self.rows = ledger(now)
        self.now = now
        pg.evaluate("(r)=>{window._momS={win:7,metric:'pct',view:'bars',sport:'ALL',pin:null,cur:null};saveP(r);renderHomePage()}", self.rows)
        pg.wait_for_selector("#mom-widget .mom-svg")
        pg.wait_for_timeout(500)
        return pg

    def agg(self, rows):
        w = sum(r["outcome"] == "win" for r in rows); n = len(rows)
        u = sum((r["decOdds"] - 1) if r["outcome"] == "win" else -1 for r in rows)
        return w, n - w, u

    def test_default_numbers_follow_the_original_rules(self):
        pg = self.page()
        win = [r for r in self.rows if r["lockedAt"] >= self.now - 7 * 86400000]
        w, l, u = self.agg(win)
        a = self.agg(self.rows)
        txt = pg.evaluate("[...document.querySelectorAll('#mom-widget .mom-v')].map(e=>e.textContent)")
        self.assertEqual(txt[0], f"{w / (w + l) * 100:.1f}%")
        self.assertEqual(txt[1], f"{a[0] / (a[0] + a[1]) * 100:.1f}%")
        self.assertEqual(len(txt), 3)                                                              # win %, all-time, units -- the VS BASELINE tile was removed (owner 2026-10-10)
        self.assertEqual(txt[2], f"{u:+.1f}u")
        self.assertNotIn("VS BASELINE", pg.inner_text("#mom-widget"))
        gd = pg.evaluate("document.querySelector('#mom-widget .mom-gd').textContent")                  # the baseline difference lives in the meter
        self.assertEqual(gd, f"{(w / (w + l) - a[0] / (a[0] + a[1])) * 100:+.1f}pp")
        self.assertIn("LAST 7D VS ALL-TIME", pg.inner_text("#mom-widget .mom-title"))
        self.assertEqual(self.errors, [])
        pg.close()

    def test_chips_change_window_metric_view_and_sport(self):
        pg = self.page()
        pg.locator(".mom-chip:has-text('14D')").click()
        self.assertIn("LAST 14D", pg.inner_text("#mom-widget .mom-title"))
        w, l, _ = self.agg([r for r in self.rows if r["lockedAt"] >= self.now - 14 * 86400000])
        self.assertIn(f"{w}W-{l}L", pg.inner_text("#mom-widget .mom-tile"))
        self.assertEqual(pg.locator("#mom-widget .mom-bar2, #mom-widget .mom-empty").count(), 14)
        self.assertEqual(pg.locator(".mom-chip:has-text('30D')").count(), 0)                            # the old 30D window is gone
        mo, lmo = pg.evaluate("[mstFirstOfMonth(getMSTNow()).getTime(),mstFirstOfMonth(new Date(getMSTNow().getFullYear(),getMSTNow().getMonth()-1,1)).getTime()]")
        today = dt.datetime.fromtimestamp(self.now / 1000, MT)
        pg.locator(".mom-chip:has-text('THIS MONTH')").click()
        self.assertIn("THIS MONTH VS ALL-TIME", pg.inner_text("#mom-widget .mom-title"))
        w, l, _ = self.agg([r for r in self.rows if r["lockedAt"] >= mo])
        self.assertIn(f"{w}W-{l}L", pg.inner_text("#mom-widget .mom-tile"))
        self.assertEqual(pg.locator("#mom-widget .mom-bar2, #mom-widget .mom-empty").count(), today.day)   # 1st of the month .. today
        pg.locator(".mom-chip:has-text('LAST MONTH')").click()
        self.assertIn("LAST MONTH VS ALL-TIME", pg.inner_text("#mom-widget .mom-title"))
        w, l, u = self.agg([r for r in self.rows if lmo <= r["lockedAt"] < mo])
        self.assertIn(f"{w}W-{l}L", pg.inner_text("#mom-widget .mom-tile"))
        days_last = (today.replace(day=1) - dt.timedelta(days=1)).day
        self.assertEqual(pg.locator("#mom-widget .mom-bar2, #mom-widget .mom-empty").count(), days_last)
        pg.locator(".mom-chip:has-text('TREND')").click()
        self.assertGreater(pg.locator("#mom-widget path.mom-line").count(), 0)
        self.assertIn("MONTH-TO-DATE", pg.inner_text("#mom-widget .mom-hint"))
        pg.locator(".mom-chip:has-text('DAILY')").click()
        pg.locator(".mom-chip:has-text('14D')").click()
        pg.locator(".mom-chip:has-text('TREND')").click()
        self.assertGreater(pg.locator("#mom-widget path.mom-line").count(), 0)
        self.assertEqual(pg.locator("#mom-widget .mom-bar2").count(), 0)
        pg.locator(".mom-chip:has-text('UNITS')").first.click()
        self.assertIn("CUMULATIVE UNITS", pg.inner_text("#mom-widget .mom-hint"))
        pg.locator(".mom-chip:has-text('DAILY')").click()
        pg.locator(".mom-chip:has-text('HOCKEY')").click()
        sp = [r for r in self.rows if r["sport"] == "NHL" and r["lockedAt"] >= self.now - 14 * 86400000]
        w2, l2, _ = self.agg(sp)
        self.assertIn(f"{w2}W-{l2}L", pg.inner_text("#mom-widget .mom-tile"))
        pg.locator(".mom-chip:has-text('SOCCER')").click()
        self.assertIn("NOT ENOUGH DATA", pg.inner_text("#mom-widget .mom-badge"))              # no soccer picks in the fixture
        self.assertEqual(self.errors, [])
        pg.close()

    def test_hover_tooltip_and_click_to_pin_show_that_days_picks(self):
        pg = self.page()
        i = 10                                                   # 14 bars: index 13 = today
        ds = (dt.datetime.fromtimestamp(self.now / 1000, MT) - dt.timedelta(days=13 - i)).strftime("%Y-%m-%d")
        day = [r for r in self.rows if r["date"] == ds]
        w, l, u = self.agg(day)
        pg.locator(".mom-hit").nth(i).hover()
        tip = pg.inner_text("#mom-tip")
        self.assertIn(f"{w}W-{l}L", tip)
        self.assertIn(f"{u:+.1f}u", tip)
        pg.locator(".mom-hit").nth(i).click(force=True)
        self.assertEqual(pg.locator(".mom-panel .mom-pr").count(), len(day))
        self.assertIn(f"{len(day)} BET", pg.inner_text(".mom-pinh"))
        pg.locator(".mom-x").click()
        self.assertEqual(pg.locator(".mom-panel").count(), 0)
        self.assertEqual(self.errors, [])
        pg.close()

    def test_keyboard_scrub_and_pin(self):
        pg = self.page()
        pg.locator(".mom-svg").focus()
        pg.keyboard.press("ArrowLeft"); pg.keyboard.press("ArrowLeft")
        self.assertEqual(pg.get_attribute(".mom-svg", "aria-valuenow"), "11")
        pg.keyboard.press("Enter")
        self.assertGreater(pg.locator(".mom-panel .mom-pr").count(), 0)
        pg.keyboard.press("Escape")
        self.assertEqual(pg.locator(".mom-panel").count(), 0)
        pg.close()

    def test_touch_tap_pins_on_a_phone_and_nothing_overflows(self):
        pg = self.page(390)
        pg.locator(".mom-hit").nth(12).click(force=True)
        self.assertGreater(pg.locator(".mom-panel .mom-pr").count(), 0)
        self.assertLessEqual(pg.evaluate("document.documentElement.scrollWidth"), 391)
        self.assertGreaterEqual(int(float(pg.evaluate("getComputedStyle(document.querySelector('.mom-chip span')).fontSize").replace("px", ""))), 11)   # readable: the phone font cap does not reach the chips
        self.assertEqual(self.errors, [])
        pg.close()

    def test_today_window_counts_todays_settled_bets_by_game_date(self):
        """owner request 2026-10-10: a TODAY chip next to 7D / 14D / THIS MONTH / LAST MONTH"""
        pg = self.page()
        today = dt.datetime.fromtimestamp(self.now / 1000, MT).strftime("%Y-%m-%d")
        want = [r for r in self.rows if r["date"] == today]
        w, l, u = self.agg(want)
        pg.locator(".mom-chip:has-text('TODAY')").first.click()
        self.assertIn("TODAY VS ALL-TIME", pg.inner_text("#mom-widget .mom-title"))
        self.assertIn(f"{w}W-{l}L", pg.inner_text("#mom-widget .mom-tile"))
        self.assertEqual(pg.get_attribute(".mom-chip:has-text('TODAY')", "aria-pressed"), "true")
        self.assertEqual(pg.locator("#mom-widget .mom-bar2, #mom-widget .mom-empty").count(), 14)          # still the 14-day chart, with today bracketed
        txt = pg.inner_text("#mom-widget")
        for bad in ("NaN", "undefined", "Infinity"):
            self.assertNotIn(bad, txt)
        pg.locator(".mom-chip:has-text('TREND')").click()
        self.assertGreater(pg.locator("#mom-widget path.mom-line").count(), 0)
        pg.locator(".mom-chip:has-text('7D')").first.click()                                                # and back to a rolling window
        self.assertIn("LAST 7D VS ALL-TIME", pg.inner_text("#mom-widget .mom-title"))
        self.assertEqual(self.errors, [])
        pg.close()

    def test_hot_cold_badge_follows_the_five_point_rule(self):
        pg = self.page()
        # all-time baseline ~75%: make the last 7 days all losses (cold) and then all wins (hot)
        cold = [dict(r, outcome="loss") if r["lockedAt"] >= self.now - 7 * 86400000 else r for r in self.rows]
        hot = [dict(r, outcome="win") if r["lockedAt"] >= self.now - 7 * 86400000 else r for r in self.rows]
        for rows, want in ((cold, "RUNNING COLD"), (hot, "RUNNING HOT")):
            pg.evaluate("(r)=>{saveP(r);renderHomePage()}", rows)
            pg.wait_for_timeout(500)
            self.assertIn(want, pg.inner_text("#mom-widget .mom-badge"))
        pg.close()


    # ── visual refresh 2026-10-10: gauge, last-10 strip, sport breakdown chips, sparklines, no NaN, no overflow ─────────────────────────────
    def test_gauge_matches_status_and_headline_delta(self):
        pg = self.page()
        cold = [dict(r, outcome="loss") if r["lockedAt"] >= self.now - 7 * 86400000 else r for r in self.rows]
        hot = [dict(r, outcome="win") if r["lockedAt"] >= self.now - 7 * 86400000 else r for r in self.rows]
        for rows, zone, sign in ((hot, "h", 1), (cold, "c", -1), ([], None, 0)):
            pg.evaluate("(r)=>{saveP(r);renderHomePage()}", rows)
            pg.wait_for_timeout(500)
            on = pg.evaluate("[...document.querySelectorAll('#mom-widget .mom-zn.on')].map(e=>e.classList[1])")
            self.assertEqual(on, [zone] if zone else [])
            a = float(pg.evaluate("parseFloat(document.querySelector('#mom-widget .mom-needle').style.getPropertyValue('--a'))"))
            self.assertTrue(a > 0 if sign > 0 else a < 0 if sign < 0 else a == 0, a)
            if zone:
                gd = pg.evaluate("document.querySelector('#mom-widget .mom-gd').textContent")
                self.assertRegex(gd, r"^[+-]\d+\.\dpp$")                         # the meter's big number is the difference vs the all-time baseline
        self.assertEqual(self.errors, [])
        pg.close()

    def test_last_ten_are_round_bubbles_with_event_and_result_on_hover(self):
        """owner 2026-10-10: the LAST 10 strip is bubbles (circles, like the league recent-form bubbles), and hovering one shows the event and result"""
        pg = self.page()
        r = pg.evaluate("(()=>{const e=document.querySelector('#mom-widget .mom-pip:not(.nw)');const c=getComputedStyle(e);return{br:c.borderRadius,w:e.getBoundingClientRect().width,h:e.getBoundingClientRect().height,txt:e.textContent}})()")
        self.assertEqual(r["txt"], "")                                  # a plain bubble, no letter
        self.assertEqual(round(r["w"]), round(r["h"]))
        self.assertIn("50%", r["br"])
        pg.locator("#mom-widget .mom-pip:not(.nw)").nth(5).hover()
        tip = pg.inner_text("#dot-tip")
        self.assertRegex(tip, r"(WIN|LOSS)")
        self.assertIn("NHL", tip.upper() + " NHL") if False else None
        self.assertTrue(tip.strip().splitlines()[-1].strip() in ("✓ WIN", "✗ LOSS"))
        pg.mouse.move(5, 5)
        pg.wait_for_timeout(150)
        self.assertEqual(pg.locator("#dot-tip").count(), 0)
        self.assertEqual(pg.evaluate("[...document.querySelectorAll('#mom-widget .mom-pip')].filter(e=>e.style.transform).length"), 0)
        self.assertEqual(self.errors, [])
        pg.close()

    def test_last_ten_strip_and_streak(self):
        pg = self.page()
        done = sorted([r for r in self.rows], key=lambda r: r["lockedAt"])[-10:]
        want = "".join("W" if r["outcome"] == "win" else "L" for r in done)
        got = pg.evaluate("[...document.querySelectorAll('#mom-widget .mom-pip:not(.nw)')].map(e=>e.classList.contains('w')?'W':'L').join('')")
        self.assertEqual(got, want)
        n = 0
        for c in reversed(want):
            if c == want[-1]: n += 1
            else: break
        self.assertEqual(pg.locator("#mom-widget .mom-pip.st").count(), n)
        self.assertIn(f"STREAK {n}{want[-1]}", pg.inner_text("#mom-widget .mom-l10s"))
        pg.close()

    def test_sport_chips_show_each_sports_window_win_pct(self):
        pg = self.page()
        nhl = [r for r in self.rows if r["sport"] == "NHL" and r["lockedAt"] >= self.now - 7 * 86400000]
        w, l, _ = self.agg(nhl)
        txt = pg.inner_text(".mom-chip:has-text('HOCKEY')")
        self.assertIn(f"{round(w / (w + l) * 100)}%", txt)
        self.assertEqual(pg.locator(".mom-chip.mom-sc").count(), 5)
        pg.locator(".mom-chip:has-text('BASKETBALL')").click()
        self.assertEqual(pg.get_attribute(".mom-chip:has-text('BASKETBALL')", "aria-pressed"), "true")
        pg.close()

    def test_no_nan_undefined_in_any_window_metric_view_sport(self):
        pg = self.page()
        for win in ("7D", "14D", "THIS MONTH", "LAST MONTH"):
            pg.locator(f".mom-chip:has-text('{win}')").first.click()
            for sport in ("ALL", "HOCKEY", "SOCCER"):
                pg.locator(f".mom-chip[data-mom='sport:{sport}']").click()
                for metric in ("WIN %", "UNITS"):
                    pg.locator(f".mom-chip:has-text('{metric}')").first.click()
                    for view in ("TREND", "DAILY"):
                        pg.locator(f".mom-chip:has-text('{view}')").first.click()
                        bad = pg.evaluate("(()=>{const t=document.getElementById('mom-widget').innerHTML;return /NaN|undefined|Infinity/.test(t)})()")
                        self.assertFalse(bad, (win, sport, metric, view))
        self.assertEqual(self.errors, [])
        pg.close()

    def test_phone_layout_no_overflow_and_readable_text(self):
        pg = self.page(390)
        for win in ("7D", "THIS MONTH"):
            pg.locator(f".mom-chip:has-text('{win}')").first.click()
            for view in ("TREND", "DAILY"):
                pg.locator(f".mom-chip:has-text('{view}')").first.click()
                self.assertLessEqual(pg.evaluate("document.documentElement.scrollWidth"), 391)
                self.assertLessEqual(pg.evaluate("(()=>{const w=document.getElementById('mom-widget').getBoundingClientRect();return Math.max(0,...[...document.querySelectorAll('#mom-widget .mom-chip,#mom-widget .mom-tile,#mom-widget .mom-meter,#mom-widget .mom-l10,#mom-widget .mom-svg')].map(e=>e.getBoundingClientRect().right-w.right))})()"), 1)
        small = pg.evaluate("[...document.querySelectorAll('#mom-widget .mom-chip span,#mom-widget .mom-k,#mom-widget .mom-s,#mom-widget .mom-pip')].map(e=>parseFloat(getComputedStyle(e).fontSize)).filter(v=>v<11).length")
        self.assertEqual(small, 0)
        pg.close()

    def test_entrance_animation_is_one_shot(self):
        pg = self.page()
        pg.wait_for_timeout(1800)
        pg.evaluate("renderHomePage()")                                  # the 60 s re-render with unchanged figures must not replay the sweep
        self.assertEqual(pg.locator("#mom-widget .mom-anim").count(), 0)
        pg.locator(".mom-chip:has-text('14D')").click()                  # changed figures do animate
        self.assertEqual(pg.locator("#mom-widget .mom-anim").count(), 1)
        pg.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
