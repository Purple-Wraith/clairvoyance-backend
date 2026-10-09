#!/usr/bin/env python3
"""Analytics > VISUALS tab (owner request 2026-10-09: enhance every visual and make them interactive).

Seeds a synthetic ledger through saveP(), opens Analytics > Visuals and checks: every original card still renders, the shared sport
filter chips re-plot the data, tooltips (mouse hover, touch tap, keyboard) carry the right numbers, the tier drill-down and the
legend / sort / period controls work, the price-basis and empty-state paths do not throw, and nothing scrolls sideways at 390px.
Expected numbers are recomputed here from the fixture (not read back from the code under test).

    python3 scripts/test_analytics_visuals.py
"""
import time
import datetime, functools, http.server, random, re, threading, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CARDS = ["cal", "eq", "tier", "streak", "pl", "sc", "roi"]
HOCKEY = ("NHL", "SHL")


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a, **k):
        pass


def build_ledger():
    """~330 settled picks over 130 days across NBA / NHL / SHL / NFL / PL, deterministic. Some NBA picks are real-priced ('market')."""
    rnd = random.Random(7)
    spec = [("NBA", 60), ("NHL", 80), ("SHL", 30), ("NFL", 80), ("PL", 60)]
    players = ["Victor Wembanyama", "Jalen Brunson", "OG Anunoby"]
    picks, n = [], 0
    for lg, cnt in spec:
        for i in range(cnt):
            n += 1
            p = round(rnd.uniform(0.52, 0.84), 3)
            win = rnd.random() < p - 0.04
            day = int(i * 130 / cnt) + (n % 3)
            date = _date(day)
            bt = ["ML", "ML", "SPREAD", "OU", "ML"][i % 5]
            row = {"id": "t%d" % n, "date": date, "hA": lg[:3] + "H", "awA": lg[:3] + "A", "sport": lg, "league": lg, "betType": bt,
                   "betOn": "%sH ML" % lg[:3], "winProb": p, "decOdds": round(1.06 / p, 2), "ml": "-110", "wager": 0,
                   "outcome": "win" if win else "loss", "lockedAt": 1780000000000 + n * 1000, "settledAt": 1780000000000 + n * 1000 + 9}
            if lg == "NBA" and i < 24:        # props with a player identity (3 players x 8)
                row.update({"betType": "PROP", "player": players[i % 3], "betOn": "%s PTS OVER 20.5" % players[i % 3]})
            if lg == "NBA" and i % 4 == 0:
                row["priceSource"] = "market"
            picks.append(row)
    return picks


def _date(day):

    return (datetime.date(2026, 6, 1) + datetime.timedelta(days=day)).isoformat()


def pnl(p):
    return (p["decOdds"] - 1) if p["outcome"] == "win" else (0 if p["outcome"] == "push" else -1)


def ordered(ledger):
    return sorted(ledger, key=lambda p: (p["date"], p["lockedAt"]))


LEDGER = build_ledger()


class Visuals(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        cls.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=str(ROOT / "docs")))
        cls.srv.daemon_threads = True      # Chromium opens idle speculative sockets; a single-threaded server would stall behind them
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.pw = sync_playwright().start()
        cls.b = cls.pw.chromium.launch()

    @classmethod
    def tearDownClass(cls):
        cls.b.close(); cls.pw.stop(); cls.srv.shutdown()

    def open(self, ledger=None, width=1300, height=2600, touch=False):
        ctx = self.b.new_context(viewport={"width": width, "height": height}, has_touch=touch)
        pg = ctx.new_page()
        pg.set_default_timeout(6000)
        self.errors = []
        pg.on("pageerror", lambda e: self.errors.append(str(e)))
        pg.goto("http://127.0.0.1:%d/app.html?nosb=1" % self.srv.server_address[1])
        pg.wait_for_function("typeof saveP==='function'&&typeof navTap==='function'")
        pg.wait_for_timeout(600)
        pg.evaluate("d=>saveP(d)", LEDGER if ledger is None else ledger)
        pg.evaluate("""()=>{navTap(document.querySelector('[onclick*="analytics"]'),'analytics');setSub('analytics','visuals');renderAnalyticsVisuals();try{hideAllND()}catch(e){}}""")
        pg.wait_for_selector("#analytics-visuals-content .card")
        pg.wait_for_timeout(1300)     # let the first-render animation finish
        pg._ctx = ctx
        return pg

    def done(self, pg):
        self.assertEqual(self.errors, [], "page errors")
        pg._ctx.close()

    def op(self, pg, sel):
        """Opacity once any fade has settled (legend toggles use an opacity transition): read until two reads agree."""
        read = lambda: pg.locator(sel).first.evaluate("e=>getComputedStyle(e).opacity")
        prev = None
        for _ in range(20):
            pg.wait_for_timeout(150)
            cur = read()
            if cur == prev:
                return cur
            prev = cur
        return prev

    def expect_op(self, pg, sel, want, timeout_ms=4000):
        """Opacity settles via a CSS transition, so poll instead of reading once (a single read raced the transition on a loaded CI runner)."""
        end = time.time() + timeout_ms / 1000
        got = None
        while time.time() < end:
            got = self.op(pg, sel)
            if got == want:
                return
            pg.wait_for_timeout(100)
        self.assertEqual(got, want)

    def tip(self, pg):
        t = pg.locator("#vz-tip")
        return t.inner_text() if t.is_visible() else ""

    def hover_in(self, pg, sel, fx, fy=0.4):
        loc = pg.locator(sel).first
        loc.scroll_into_view_if_needed()
        bb = loc.bounding_box()
        pg.mouse.move(bb["x"] + bb["width"] * fx, bb["y"] + bb["height"] * fy)
        pg.wait_for_timeout(120)

    # ---------------------------------------------------------------- render
    def test_every_original_card_still_renders(self):
        pg = self.open()
        for k in CARDS:
            txt = pg.locator("#vz-c-" + k).inner_text()
            self.assertTrue(txt.strip(), k)
            self.assertNotIn("CHART UNAVAILABLE", txt, k)
            self.assertNotIn("NOT ENOUGH DATA YET", txt, k)
        body = pg.locator("#analytics-visuals-content").inner_text()
        for title in ["RELIABILITY CURVE", "EQUITY CURVE", "SPORT × CONFIDENCE TIER", "RECENT FORM", "PLAYER PROP FORM", "MODEL CONFIDENCE VS OUTCOME", "BET TYPE × SPORT"]:
            self.assertIn(title, body)
        self.assertEqual(pg.locator(".vz-filter .vz-chip").count(), 5)
        self.assertGreaterEqual(pg.locator("#vz-c-cal svg circle").count(), 5)          # calibration dots
        self.assertEqual(pg.locator("#vz-c-sc .vz-g-win circle").count() + pg.locator("#vz-c-sc .vz-g-loss circle").count(), len(LEDGER))
        self.done(pg)

    # ---------------------------------------------------------------- filter chips
    def test_filter_chips_replot_every_card(self):
        pg = self.open()
        n_all = len(LEDGER)
        self.assertEqual(self.stat(pg, "BETS"), str(n_all))
        pg.click(".vz-filter [data-v=HOCKEY]"); pg.wait_for_timeout(300)
        n_hk = len([p for p in LEDGER if p["sport"] in HOCKEY])
        self.assertEqual(self.stat(pg, "BETS"), str(n_hk))
        self.assertEqual(pg.locator("#vz-c-streak .vz-strip").count(), 1)
        self.assertIn("HOCKEY", pg.locator("#vz-c-streak .vz-strip").inner_text())
        self.assertEqual(pg.locator("#vz-c-sc .vz-g-win circle").count() + pg.locator("#vz-c-sc .vz-g-loss circle").count(), n_hk)
        self.assertEqual(pg.locator("#vz-c-roi .vz-rrow").count(), 2)                    # header row + the one hockey row
        self.assertEqual(pg.locator(".vz-filter [aria-pressed=true]").inner_text().split()[0], "HOCKEY")
        self.assertEqual(pg.evaluate("document.activeElement.dataset.v"), "HOCKEY")      # focus survives the re-render
        pg.click(".vz-filter [data-v=ALL]"); pg.wait_for_timeout(300)
        self.assertEqual(self.stat(pg, "BETS"), str(n_all))
        self.done(pg)

    def stat(self, pg, label):
        return pg.evaluate("""l=>{for(const s of document.querySelectorAll('#vz-c-eq .vz-st')){if(s.querySelector('span').textContent===l)return s.querySelector('b').textContent}return null}""", label)

    # ---------------------------------------------------------------- equity
    def test_equity_hover_shows_the_right_running_units(self):
        pg = self.open()
        self.hover_in(pg, "#vz-c-eq .vz-chart", 0.5)
        t = self.tip(pg)
        m = re.search(r"BET (\d+) OF (\d+)", t)
        self.assertTrue(m, t)
        i, n = int(m.group(1)), int(m.group(2))
        self.assertEqual(n, len(LEDGER))
        run = sum(pnl(p) for p in ordered(LEDGER)[:i])
        got = float(re.search(r"RUNNING\s*([+−-][\d.]+)u", t).group(1).replace("−", "-"))
        self.assertAlmostEqual(got, run, delta=0.011)
        pick = ordered(LEDGER)[i - 1]
        self.assertIn(pick["awA"] + " @ " + pick["hA"], t)
        self.assertTrue(pg.locator("#vz-c-eq .vz-xh").evaluate("e=>e.style.display!=='none'"))   # crosshair is up
        box = pg.locator("#vz-tip").bounding_box()
        self.assertTrue(box["x"] >= 0 and box["x"] + box["width"] <= 1300)
        pg.mouse.move(5, 5); pg.wait_for_timeout(150)
        self.assertEqual(self.tip(pg), "")
        self.done(pg)

    def test_equity_period_chips_and_final_marker(self):
        pg = self.open()
        last = ordered(LEDGER)[-1]["date"]

        cut = (datetime.date.fromisoformat(last) - datetime.timedelta(days=29)).isoformat()
        want = [p for p in LEDGER if p["date"] >= cut]
        pg.click("#vz-c-eq [data-vzact=per][data-v='30D']"); pg.wait_for_timeout(300)
        self.assertEqual(self.stat(pg, "BETS"), str(len(want)))
        self.assertEqual(self.stat(pg, "UNITS"), ("+" if sum(map(pnl, want)) >= 0 else "−") + "%.1f" % abs(sum(map(pnl, want))) + "u")
        self.assertIn("LAST 30D", pg.locator("#vz-c-eq svg").text_content())
        self.assertEqual(pg.locator("#vz-c-eq [data-vzact=per][aria-pressed=true]").inner_text(), "30D")
        pg.click("#vz-c-eq [data-vzact=per][data-v='ALL']"); pg.wait_for_timeout(300)
        self.assertEqual(self.stat(pg, "BETS"), str(len(LEDGER)))
        self.done(pg)

    def test_equity_drawdown_toggle_and_stats(self):
        pg = self.open()
        run = peak = mdd = 0.0
        for p in ordered(LEDGER):
            run += pnl(p); peak = max(peak, run); mdd = max(mdd, peak - run)
        self.assertEqual(self.stat(pg, "MAX DRAWDOWN"), "−%.1fu" % mdd)
        self.assertEqual(pg.locator("#vz-c-eq .vz-dd").count(), 1)
        pg.click("#vz-c-eq [data-vzact=leg][data-v=dd]")
        self.assertEqual(pg.locator("#vz-c-eq [data-v=dd]").get_attribute("aria-pressed"), "false")
        self.assertIn("vz-off-dd", pg.locator("#vz-c-eq").get_attribute("class"))
        self.expect_op(pg, "#vz-c-eq .vz-dd", "0")
        self.done(pg)

    def test_equity_keyboard_scrub(self):
        pg = self.open()
        host = pg.locator("#vz-c-eq .vz-chart")
        host.scroll_into_view_if_needed(); host.focus()
        pg.keyboard.press("End"); pg.keyboard.press("ArrowLeft")
        self.assertEqual(host.get_attribute("aria-valuenow"), str(len(LEDGER) - 1))
        self.assertIn("BET %d OF %d" % (len(LEDGER) - 1, len(LEDGER)), self.tip(pg))
        pg.keyboard.press("Escape")
        self.assertEqual(self.tip(pg), "")
        self.done(pg)

    # ---------------------------------------------------------------- calibration
    def test_calibration_hover_matches_the_fixture_bin(self):
        pg = self.open()
        bins = {}
        for lo in range(50, 90, 5):
            bb = [p for p in LEDGER if lo <= p["winProb"] * 100 < lo + 5]
            if bb:
                bins[lo] = (len(bb), 100.0 * sum(1 for p in bb if p["outcome"] == "win") / len(bb))
        dots = pg.locator("#vz-c-cal .vz-pt")
        dots.first.scroll_into_view_if_needed()
        seen = 0
        for k in range(dots.count()):
            bb = dots.nth(k).bounding_box()
            pg.mouse.move(bb["x"] + bb["width"] / 2, bb["y"] + bb["height"] / 2); pg.wait_for_timeout(100)
            t = self.tip(pg)
            m = re.search(r"PREDICTED (\d+)–(\d+)%", t)
            if not m:
                continue
            lo = int(m.group(1)); n, acc = bins[lo]
            self.assertIn("n = %d" % n, t)
            self.assertIn("%.1f%%" % acc, t)
            self.assertIn("95% BAND", t)
            seen += 1
        self.assertGreaterEqual(seen, 5)
        self.assertEqual(pg.locator("#vz-c-cal .vz-hl").evaluate("e=>e.style.display!=='none'"), True)
        self.done(pg)

    def test_calibration_league_tabs_and_legend(self):
        pg = self.open()
        self.assertIn("NBA", pg.locator("#vz-c-cal .vz-chips").first.inner_text())
        pg.click("#vz-c-cal [data-vzact=lg][data-v=NFL]"); pg.wait_for_timeout(200)
        self.assertIn("%d settled, probability-tagged" % len([p for p in LEDGER if p["sport"] == "NFL"]), pg.locator("#vz-c-cal").inner_text())
        pg.click("#vz-c-cal [data-vzact=leg][data-v=band]")
        self.expect_op(pg, "#vz-c-cal .vz-band", "0")
        pg.click("#vz-c-cal [data-vzact=leg][data-v=band]")
        self.expect_op(pg, "#vz-c-cal .vz-band", "1")
        self.done(pg)

    def test_calibration_keyboard_focus_shows_bin(self):
        pg = self.open()
        pt = pg.locator("#vz-c-cal .vz-pt").first
        pt.scroll_into_view_if_needed(); pt.focus()
        pg.keyboard.press("ArrowRight")
        self.assertIn("PREDICTED", self.tip(pg))
        self.assertEqual(pg.locator("#vz-c-cal .vz-pt").nth(1).evaluate("e=>document.activeElement===e"), True)
        self.done(pg)

    # ---------------------------------------------------------------- tier drill-down
    def test_tier_bar_drill_down(self):
        pg = self.open()
        bars = pg.locator("#vz-c-tier .vz-tb")
        self.assertGreaterEqual(bars.count(), 2)
        grade = bars.first.get_attribute("data-v")
        want = pg.evaluate("""g=>{const b=getP().filter(p=>p.outcome!=='pending'&&_gradeLabel(p)===g&&['NBA','NHL','SHL','NFL','PL'].includes(_normSport(p)));
          return {n:b.length,w:b.filter(p=>p.outcome==='win').length,l:b.filter(p=>p.outcome==='loss').length}}""", grade)
        self.assertGreater(want["n"], 0)
        bars.first.scroll_into_view_if_needed()
        bars.first.click(); pg.wait_for_timeout(250)
        d = pg.locator("#vz-c-tier .vz-drill")
        txt = d.inner_text()
        self.assertIn(grade, txt)
        self.assertIn("%dW–%dL" % (want["w"], want["l"]), txt)
        self.assertIn("%.1f%%" % (100.0 * want["w"] / want["n"]), txt)
        self.assertIn("UNITS", txt); self.assertIn("ROI", txt); self.assertIn("CLAIMED", txt)
        self.assertLessEqual(d.locator(".vz-rp").count(), 8)
        self.assertGreaterEqual(d.locator(".vz-rp").count(), 1)
        self.assertEqual(pg.locator("#vz-c-tier .vz-tb[aria-pressed=true]").get_attribute("data-v"), grade)
        pg.click("#vz-c-tier [data-vzact=tierx]"); pg.wait_for_timeout(200)
        self.assertIn("TAP A GRADE BAR", pg.locator("#vz-c-tier .vz-drill").inner_text())
        self.done(pg)

    def test_tier_heatmap_cell_drills_into_one_sport(self):
        pg = self.open()
        cell = pg.locator("#vz-c-tier button.vz-cell").first
        cell.scroll_into_view_if_needed()
        tipsrc = cell.get_attribute("data-vztip")
        self.assertIn("RECORD", tipsrc)
        cell.hover(); pg.wait_for_timeout(120)
        self.assertIn("VS CLAIM", self.tip(pg))
        cell.click(); pg.wait_for_timeout(200)
        self.assertRegex(pg.locator("#vz-c-tier .vz-drill").inner_text(), r"(BASKETBALL|HOCKEY|FOOTBALL|SOCCER)")
        self.done(pg)

    def test_grade_colours_are_the_uniform_ones(self):
        pg = self.open()
        cols = pg.evaluate("""()=>{const o={};document.querySelectorAll('#vz-c-tier .vz-tbl').forEach(e=>{o[e.textContent]=e.style.color});return o}""")
        for g, want in (("PREMIUM", "var(--gc)"), ("OPTIMAL", "var(--pc)"), ("LEAN", "var(--nc)")):
            if g in cols:
                self.assertEqual(cols[g], want)
        self.done(pg)

    # ---------------------------------------------------------------- streak + players
    def test_streak_dot_tooltip_and_pin(self):
        pg = self.open()
        dot = pg.locator("#vz-c-streak .vz-dot").last
        dot.scroll_into_view_if_needed(); dot.hover(); pg.wait_for_timeout(120)
        t = self.tip(pg)
        self.assertRegex(t, r"RESULT\s*(WIN|LOSS)")
        self.assertIn("MODEL PROB", t)
        dot.click(); pg.wait_for_timeout(150)
        self.assertEqual(pg.locator("#vz-c-streak .vz-detail:not(:empty)").count(), 1)     # pinned panel opens under the clicked strip only
        det = pg.locator("#vz-c-streak .vz-detail:not(:empty)").inner_text()
        self.assertRegex(det, r"(WIN|LOSS)")
        self.assertIn("GAME", det)
        self.assertIn("sel", dot.get_attribute("class"))
        pg.click("#vz-c-streak [data-vzact=leg][data-v=sw]")
        self.expect_op(pg, "#vz-c-streak .vz-dot[data-o=win]", "0.1")
        self.done(pg)

    def test_streak_roving_arrow_keys(self):
        pg = self.open()
        dot = pg.locator("#vz-c-streak .vz-dot").nth(5)
        dot.scroll_into_view_if_needed(); dot.focus()
        pg.keyboard.press("ArrowRight")
        self.assertEqual(pg.locator("#vz-c-streak .vz-dot").nth(6).evaluate("e=>document.activeElement===e"), True)
        self.done(pg)

    def test_player_rows_sort_and_pin(self):
        pg = self.open()
        rows = pg.locator("#vz-c-pl .vz-prow")
        self.assertEqual(rows.count(), 3)
        self.assertIn("8 props", rows.first.inner_text())
        pg.click("#vz-c-pl [data-vzact=plsort][data-v=hit]"); pg.wait_for_timeout(200)
        pcts = [float(x.replace("%", "")) for x in pg.locator("#vz-c-pl .vz-pct").all_inner_texts()]
        self.assertEqual(pcts, sorted(pcts, reverse=True))
        pg.locator("#vz-c-pl .vz-dot").first.click(); pg.wait_for_timeout(120)
        self.assertIn("PTS OVER 20.5", pg.locator("#vz-c-pl .vz-detail:not(:empty)").inner_text())
        self.done(pg)

    # ---------------------------------------------------------------- scatter
    def test_scatter_hover_pin_and_toggles(self):
        pg = self.open()
        dot = pg.locator("#vz-c-sc .vz-g-win circle").nth(40)
        dot.scroll_into_view_if_needed()
        bb = dot.bounding_box()
        pg.mouse.move(bb["x"] + bb["width"] / 2, bb["y"] + bb["height"] / 2); pg.wait_for_timeout(120)
        t = self.tip(pg)
        self.assertRegex(t, r"RESULT\s*WIN")
        self.assertIn("MODEL PROB", t)
        pg.mouse.click(bb["x"] + bb["width"] / 2, bb["y"] + bb["height"] / 2); pg.wait_for_timeout(150)
        self.assertIn("WIN", pg.locator("#vz-c-sc .vz-detail").inner_text())
        self.assertTrue(pg.locator("#vz-c-sc .vz-sel").evaluate("e=>e.style.display!=='none'"))
        pg.click("#vz-c-sc [data-vzact=leg][data-v=win]")
        self.expect_op(pg, "#vz-c-sc .vz-g-win", "0")
        self.done(pg)

    def test_scatter_trend_node_has_bucket_stats(self):
        pg = self.open()
        node = pg.locator("#vz-c-sc .vz-pt").first
        node.scroll_into_view_if_needed(); node.focus()
        pg.keyboard.press("Tab"); pg.keyboard.press("Shift+Tab")
        t = self.tip(pg)
        self.assertIn("CONFIDENCE", t); self.assertIn("REAL WIN RATE", t)
        self.done(pg)

    # ---------------------------------------------------------------- ROI
    def test_roi_units_toggle_sort_and_bars(self):
        pg = self.open()
        cells = pg.locator("#vz-c-roi button.vz-cell")
        before = cells.first.inner_text()
        self.assertIn("%", before.split("\n")[0])
        pg.click("#vz-c-roi [data-vzact=roimode][data-v=units]"); pg.wait_for_timeout(200)
        self.assertTrue(pg.locator("#vz-c-roi button.vz-cell").first.inner_text().split("\n")[0].endswith("u"))
        pg.click("#vz-c-roi [data-vzact=roiview][data-v=bars]"); pg.wait_for_timeout(200)
        pg.click("#vz-c-roi [data-vzact=roisort][data-v=best]"); pg.wait_for_timeout(200)
        vals = [float(re.search(r"([+−-][\d.]+)u", x).group(1).replace("−", "-")) for x in pg.locator("#vz-c-roi .vz-rbv").all_inner_texts()]
        self.assertGreaterEqual(len(vals), 5)
        self.assertEqual(vals, sorted(vals, reverse=True))
        pg.click("#vz-c-roi [data-vzact=roisort][data-v=worst]"); pg.wait_for_timeout(200)
        vals = [float(re.search(r"([+−-][\d.]+)u", x).group(1).replace("−", "-")) for x in pg.locator("#vz-c-roi .vz-rbv").all_inner_texts()]
        self.assertEqual(vals, sorted(vals))
        pg.locator("#vz-c-roi .vz-rb").first.hover(); pg.wait_for_timeout(120)
        t = self.tip(pg)
        for k in ("ROI", "UNITS", "RECORD", "WIN %", "AVG ODDS"):
            self.assertIn(k, t)
        self.done(pg)

    def test_roi_cell_numbers_match_fixture(self):
        pg = self.open()
        hk = [p for p in LEDGER if p["sport"] in HOCKEY and p["betType"] == "ML"]
        u = sum(map(pnl, hk))
        txt = pg.locator("#vz-c-roi .vz-rrow", has_text="HOCKEY").inner_text()
        self.assertIn("%s%.1f%%" % ("+" if u >= 0 else "−", abs(u / len(hk) * 100)), txt)
        self.assertIn("n%d" % len(hk), txt)
        self.done(pg)

    # ---------------------------------------------------------------- price basis, empty state, touch, layout
    def test_price_basis_real_only_uses_real_priced_picks(self):
        pg = self.open()
        pg.evaluate("setPriceBasis('real')"); pg.wait_for_timeout(500)
        real = [p for p in LEDGER if p.get("priceSource") == "market"]
        self.assertEqual(self.stat(pg, "BETS"), str(len(real)))
        pg.evaluate("setPriceBasis('all')"); pg.wait_for_timeout(500)
        self.assertEqual(self.stat(pg, "BETS"), str(len(LEDGER)))
        self.done(pg)

    def test_empty_and_tiny_ledgers_degrade_gracefully(self):
        pg = self.open(ledger=LEDGER[:4])
        self.assertIn("NOT ENOUGH DATA YET", pg.locator("#vz-c-eq").inner_text())
        self.done(pg)
        pg = self.open(ledger=[])
        self.assertIn("No settled bets yet", pg.locator("#analytics-visuals-content").inner_text())
        self.done(pg)

    def test_filter_with_no_matching_sport_shows_empty_states(self):
        led = [p for p in LEDGER if p["sport"] != "PL"]
        pg = self.open(ledger=led)
        pg.click(".vz-filter [data-v=SOCCER]"); pg.wait_for_timeout(300)
        self.assertIn("NOT ENOUGH DATA YET", pg.locator("#vz-c-streak").inner_text())
        self.assertIn("NOT ENOUGH", pg.locator("#vz-c-eq").inner_text())
        self.done(pg)

    def test_touch_tap_shows_tooltip_and_taps_elsewhere_hide_it(self):
        pg = self.open(width=390, height=2800, touch=True)
        host = pg.locator("#vz-c-eq .vz-chart")
        host.scroll_into_view_if_needed()
        bb = host.bounding_box()
        pg.touchscreen.tap(bb["x"] + bb["width"] * 0.6, bb["y"] + 60); pg.wait_for_timeout(200)
        self.assertRegex(self.tip(pg), r"BET \d+ OF %d" % len(LEDGER))
        tb = pg.locator("#vz-tip").bounding_box()
        self.assertTrue(tb["x"] >= 0 and tb["x"] + tb["width"] <= 390)
        pg.touchscreen.tap(190, 5); pg.wait_for_timeout(150)
        self.assertEqual(self.tip(pg), "")
        dot = pg.locator("#vz-c-streak .vz-dot").nth(3)
        dot.scroll_into_view_if_needed(); dot.tap(); pg.wait_for_timeout(200)
        self.assertRegex(pg.locator("#vz-c-streak .vz-detail:not(:empty)").inner_text(), r"WIN|LOSS")
        self.done(pg)

    def test_no_horizontal_scroll_at_390(self):
        pg = self.open(width=390, height=900)
        r = pg.evaluate("""()=>{const c=document.getElementById('analytics-visuals-content');
          const worst=[...c.querySelectorAll('*')].filter(e=>e.getBoundingClientRect().right>391&&getComputedStyle(e).position!=='fixed').map(e=>e.className||e.tagName).slice(0,5);
          return {doc:document.documentElement.scrollWidth,body:document.body.scrollWidth,c:c.scrollWidth,cw:c.clientWidth,worst}}""")
        self.assertLessEqual(r["doc"], 390, r); self.assertLessEqual(r["body"], 390, r); self.assertLessEqual(r["c"], r["cw"], r)
        self.assertEqual(r["worst"], [], r)
        for k in CARDS:
            self.assertTrue(pg.locator("#vz-c-" + k).inner_text().strip())
        self.done(pg)

    def test_reduced_motion_disables_animation(self):
        ctx = self.b.new_context(viewport={"width": 1300, "height": 2600}, reduced_motion="reduce")
        pg = ctx.new_page()
        pg.goto("http://127.0.0.1:%d/app.html?nosb=1" % self.srv.server_address[1])
        pg.wait_for_function("typeof saveP==='function'&&typeof navTap==='function'")
        pg.evaluate("d=>saveP(d)", LEDGER)
        pg.evaluate("""()=>{navTap(document.querySelector('[onclick*="analytics"]'),'analytics');setSub('analytics','visuals');renderAnalyticsVisuals();try{hideAllND()}catch(e){}}""")
        pg.wait_for_selector("#vz-c-eq .vz-line")
        self.assertEqual(pg.locator("#vz-c-eq .vz-line").evaluate("e=>getComputedStyle(e).animationName"), "none")
        self.assertEqual(pg.locator("#vz-c-eq .vz-pulse").evaluate("e=>getComputedStyle(e).animationName"), "none")
        ctx.close()

    def test_aria_labels_on_charts(self):
        pg = self.open()
        for sel in ("#vz-c-cal .vz-chart", "#vz-c-eq .vz-chart", "#vz-c-sc .vz-chart"):
            self.assertTrue((pg.locator(sel).get_attribute("aria-label") or "").strip(), sel)
        self.assertEqual(pg.locator("#vz-c-eq .vz-chart").get_attribute("role"), "slider")
        self.assertEqual(pg.locator("#vz-c-eq .vz-chart").get_attribute("tabindex"), "0")
        self.assertTrue(pg.locator("#vz-c-cal .vz-pt").first.get_attribute("aria-label"))
        self.done(pg)


if __name__ == "__main__":
    unittest.main(verbosity=2)
