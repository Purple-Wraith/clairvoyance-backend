#!/usr/bin/env python3
"""Home > "// MOMENTUM — LAST 7D VS ALL-TIME" (interactive, 2026-10-09): the headline numbers keep the original rules (window = lockedAt within N days, so 7D equals the ROLLING 7D card; baseline = all-time
win% of in-scope picks; hot/cold = +/-5 points with 5+ settled bets) and every visual is interactive: window / metric / view / sport chips, tooltips on hover + tap + arrow keys, click-a-day to pin its picks.

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
    for off in range(0, 40):
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
        self.assertEqual(txt[2], f"{(w / (w + l) - a[0] / (a[0] + a[1])) * 100:+.1f}pp")
        self.assertEqual(txt[3], f"{u:+.1f}u")
        self.assertIn("LAST 7D VS ALL-TIME", pg.inner_text("#mom-widget .mom-title"))
        self.assertEqual(self.errors, [])
        pg.close()

    def test_chips_change_window_metric_view_and_sport(self):
        pg = self.page()
        pg.locator(".mom-chip:has-text('30D')").click()
        self.assertIn("LAST 30D", pg.inner_text("#mom-widget .mom-title"))
        win = [r for r in self.rows if r["lockedAt"] >= self.now - 30 * 86400000]
        w, l, _ = self.agg(win)
        self.assertIn(f"{w}W-{l}L", pg.inner_text("#mom-widget .mom-tile"))
        self.assertEqual(pg.locator("#mom-widget .mom-bar2, #mom-widget .mom-empty").count(), 30)       # one bar per day of the 30D window
        pg.locator(".mom-chip:has-text('TREND')").click()
        self.assertGreater(pg.locator("#mom-widget path.mom-line").count(), 0)
        self.assertEqual(pg.locator("#mom-widget .mom-bar2").count(), 0)
        pg.locator(".mom-chip:has-text('UNITS')").first.click()
        self.assertIn("CUMULATIVE UNITS", pg.inner_text("#mom-widget .mom-hint"))
        pg.locator(".mom-chip:has-text('DAILY')").click()
        pg.locator(".mom-chip:has-text('HOCKEY')").click()
        sp = [r for r in self.rows if r["sport"] == "NHL" and r["lockedAt"] >= self.now - 30 * 86400000]
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
