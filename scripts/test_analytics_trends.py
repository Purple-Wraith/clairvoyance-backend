#!/usr/bin/env python3
"""Analytics > TRENDS tab (owner request 2026-10-09: enhance the sub-tab's visuals and make them interactive).

Seeds a synthetic ledger through saveP(), opens Analytics > Trends and checks: every original view still renders, the shared sport
and period chips re-plot the data, the numbers match values recomputed here from the fixture (not read back from the code under
test), tooltips work for mouse hover / touch tap / keyboard, drill-downs and pins list the underlying picks, legend toggles, sort
chips and expandable rows work, the price-basis and empty-state paths do not throw, and nothing scrolls sideways at 390px.

    python3 scripts/test_analytics_trends.py
"""
import datetime, functools, http.server, math, random, re, threading, time, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SLOTS = ["sum", "hot", "rec", "cmp", "nm", "cal", "sa", "bt", "win", "loss", "jr", "pr"]
GROUP = {"BASKETBALL": ("NBA",), "HOCKEY": ("NHL", "SHL"), "FOOTBALL": ("NFL",), "SOCCER": ("PL",)}
NOW = datetime.datetime.now(datetime.timezone.utc)


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a, **k):
        pass


def build_ledger():
    """360 settled picks over 120 days across NBA / NHL / SHL / NFL / PL (deterministic). Age is whole days + 1..20 hours, so nothing sits
    within an hour of a 7D / 30D / 90D cut. Includes 3 pushes, ML/SPREAD/OU/PROP bets with final scores (some razor-margin losses),
    props with player names, journal reasons and some real-priced ('market') picks."""
    rnd = random.Random(11)
    spec = [("NBA", 70), ("NHL", 90), ("SHL", 40), ("NFL", 90), ("PL", 70)]
    players = ["Victor Wembanyama", "Jalen Brunson", "OG Anunoby"]
    reasons = ["Sharp money", "Model edge", "Gut feel"]
    picks, n = [], 0
    for lg, cnt in spec:
        for i in range(cnt):
            n += 1
            p = round(rnd.uniform(0.50, 0.84), 3)
            r = rnd.random()
            outcome = "win" if r < p - 0.03 else "loss"
            if n in (13, 101, 202):
                outcome = "push"
            age_d = int((cnt - 1 - i) * 119 / max(1, cnt - 1)) + 1          # 1..120 days, oldest first within a league
            ts = NOW - datetime.timedelta(days=age_d, hours=1 + (n * 7) % 20)
            bt = ["ML", "ML", "SPREAD", "OU", "ML"][i % 5]
            h, a = rnd.randint(0, 6), rnd.randint(0, 6)
            row = {"id": "t%d" % n, "date": ts.date().isoformat(), "hA": lg[:2] + "H", "awA": lg[:2] + "A", "sport": lg, "league": lg,
                   "betType": bt, "betOn": "%sH ML" % lg[:2], "winProb": p, "decOdds": round(1.06 / p, 2),
                   "ml": rnd.choice(["-110", "+125", "-182", "-244"]), "wager": 0, "outcome": outcome,
                   "lockedAt": int(ts.timestamp() * 1000), "settledAt": int(ts.timestamp() * 1000) + 9, "hScore": h, "aScore": a}
            if bt == "SPREAD":
                row["betOn"] = "%sH -1.5" % lg[:2]
            if bt == "OU":
                row["betOn"] = "OVER 5.5"
            if lg == "NBA" and i < 24:
                row.update({"betType": "PROP", "betOn": "%s PTS OVER 20.5" % players[i % 3], "playerResult": rnd.choice([19, 20, 21, 25, 30])})
                row.pop("hScore"); row.pop("aScore")
            if n % 4 == 0 and lg == "NBA":
                row["priceSource"] = "market"
            if n % 5 == 0:
                row["reason"] = reasons[n % 3]
            picks.append(row)
    return picks


def _fix_hscore(picks):
    """Make ML losses razor-thin in about a third of the cases so the near-miss list is non-trivial."""
    rnd = random.Random(5)
    for p in picks:
        if p["outcome"] == "loss" and p["betType"] == "ML" and rnd.random() < 0.5:
            p["hScore"], p["aScore"] = 2, 3
        if p["outcome"] == "loss" and p["betType"] == "OU" and rnd.random() < 0.6:
            p["hScore"], p["aScore"] = 3, 3          # total 6 vs OVER 5.5 -> that is a win; flip to an UNDER-style miss below
            p["betOn"] = "UNDER 5.5"
    return picks


LEDGER = _fix_hscore(build_ledger())


def pnl(p):
    return (p["decOdds"] - 1) if p["outcome"] == "win" else (0 if p["outcome"] == "push" else -1)


def ordered(ledger):
    return sorted(ledger, key=lambda p: (p["date"], p["lockedAt"]))


def agg(bets):
    w = sum(1 for p in bets if p["outcome"] == "win")
    l = sum(1 for p in bets if p["outcome"] == "loss")
    return {"n": len(bets), "w": w, "l": l, "ps": len(bets) - w - l, "u": sum(map(pnl, bets)), "acc": (w / len(bets)) if bets else None}


def rec(a):
    return "%dW–%dL" % (a["w"], a["l"]) + ("–%dP" % a["ps"] if a["ps"] else "")


def sgn(v, d=1):
    return ("+" if v >= 0 else "−") + ("%." + str(d) + "f") % abs(v)


def rnd0(x):
    return int(math.floor(x + 0.5))


def age_h(p):
    return (NOW.timestamp() * 1000 - p["lockedAt"]) / 3600000.0


def in_days(bets, d):
    return [p for p in bets if age_h(p) < d * 24]


def near_misses(bets):
    """Same razor-margin rules the page documents (ML <=2, spread <=1.5 short of the cover, totals within 1.5, props within 2)."""
    out = []
    for p in bets:
        if p["outcome"] != "loss":
            continue
        k = None
        if p["betType"] == "ML" and "hScore" in p:
            if abs(p["hScore"] - p["aScore"]) <= 2:
                k = "ML"
        elif p["betType"] == "SPREAD" and "hScore" in p:
            m = re.match(r"^(.+?)\s([+-][\d.]+)$", p["betOn"])
            if m and m.group(1) == p["hA"]:
                margin = (p["hScore"] - p["aScore"]) + float(m.group(2))
                if margin < 0 and abs(margin) <= 1.5:
                    k = "SPREAD"
        elif p["betType"] == "OU" and "hScore" in p:
            line = float(re.sub(r"OVER|UNDER", "", p["betOn"]).strip())
            if abs(p["hScore"] + p["aScore"] - line) <= 1.5:
                k = "OU"
        elif p["betType"] == "PROP" and "playerResult" in p:
            line = float(re.search(r"(?:OVER|UNDER)\s+([\d.]+)", p["betOn"]).group(1))
            if abs(p["playerResult"] - line) <= 2:
                k = "PROP"
        if k:
            out.append((k, p))
    return out


class Trends(unittest.TestCase):
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

    @staticmethod
    def block_fonts(pg):
        """Google Fonts can be slow or unreachable from a test box; the layout falls back to local fonts, so abort those requests."""
        pg.route(re.compile(r"https?://(fonts\.googleapis\.com|fonts\.gstatic\.com)/.*"), lambda r: r.abort())

    def open(self, ledger=None, width=1300, height=7000, touch=False):
        ctx = self.b.new_context(viewport={"width": width, "height": height}, has_touch=touch)
        pg = ctx.new_page()
        self.block_fonts(pg)
        pg.set_default_timeout(8000)
        self.errors = []
        pg.on("pageerror", lambda e: self.errors.append(str(e)))
        pg.goto("http://127.0.0.1:%d/app.html?nosb=1" % self.srv.server_address[1], wait_until="domcontentloaded", timeout=60000)
        pg.wait_for_function("typeof saveP==='function'&&typeof navTap==='function'")
        pg.wait_for_timeout(600)
        pg.evaluate("d=>saveP(d)", LEDGER if ledger is None else ledger)
        pg.evaluate("""()=>{navTap(document.querySelector('[onclick*="analytics"]'),'analytics');setSub('analytics','trends');renderAnalyticsTrends();try{hideAllND()}catch(e){}}""")
        pg.wait_for_selector("#analytics-trends-content .card, #analytics-trends-content .empty")
        pg.wait_for_timeout(1300)     # let the first-render animation finish
        pg._ctx = ctx
        return pg

    def done(self, pg):
        self.assertEqual(self.errors, [], "page errors")
        pg._ctx.close()

    def tip(self, pg):
        t = pg.locator("#tv-tip")
        return t.inner_text() if t.is_visible() else ""

    def op(self, pg, sel):
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
        """Opacity settles via a CSS transition, so poll instead of reading once."""
        end = time.time() + timeout_ms / 1000
        got = None
        while time.time() < end:
            got = self.op(pg, sel)
            if got == want:
                return
            pg.wait_for_timeout(100)
        self.assertEqual(got, want)

    def hover_in(self, pg, sel, fx, fy=0.4):
        loc = pg.locator(sel).first
        loc.scroll_into_view_if_needed()
        bb = loc.bounding_box()
        pg.mouse.move(bb["x"] + bb["width"] * fx, bb["y"] + bb["height"] * fy)
        pg.wait_for_timeout(150)

    def stat(self, pg, label, slot="sum"):
        return pg.evaluate("""a=>{for(const s of document.querySelectorAll('#tv-c-'+a[1]+' .vz-st')){if(s.querySelector('span').textContent.indexOf(a[0])===0)return s.querySelector('b').textContent}return null}""", [label, slot])

    # ---------------------------------------------------------------- render
    def test_every_original_view_still_renders(self):
        pg = self.open()
        for k in SLOTS:
            txt = pg.locator("#tv-c-" + k).inner_text()
            self.assertTrue(txt.strip(), k)
            self.assertNotIn("SECTION UNAVAILABLE", txt, k)
            self.assertNotIn("NOT ENOUGH DATA YET", txt, k)
        body = pg.locator("#analytics-trends-content").inner_text().upper()   # headings are upper-cased by CSS
        for title in ["ENGINE TREND ANALYSIS", "HOT / COLD SPOTS", "HOT SPOTS", "COLD SPOTS", "RECENCY", "RECENT (30D) VS HISTORICAL TRENDS",
                      "NEAR MISSES", "CONFIDENCE CALIBRATION", "SELF-ASSESSMENT", "STRONGEST", "WEAKEST", "BEST TYPE", "BY BET TYPE",
                      "WIN ANALYSIS", "LOSS ANALYSIS", "BET JOURNAL", "PLAYER PROPS PERFORMANCE", "WHY IT WON", "POSSIBLE FACTORS"]:
            self.assertIn(title, body)
        self.assertIn("%d SETTLED BETS" % len(LEDGER), pg.locator(".tv-hdr").inner_text())
        self.assertEqual(pg.locator(".vz-filter [data-tva=flt]").count(), 5)
        self.assertEqual(pg.locator(".vz-filter [data-tva=per]").count(), 4)
        self.done(pg)

    def test_headline_stats_match_fixture(self):
        pg = self.open()
        a = agg(ordered(LEDGER))
        self.assertEqual(self.stat(pg, "BETS"), str(len(LEDGER)))
        self.assertEqual(self.stat(pg, "RECORD"), rec(a))
        self.assertEqual(self.stat(pg, "WIN %"), "%.1f%%" % (100.0 * a["acc"]))
        self.assertEqual(self.stat(pg, "UNITS"), sgn(a["u"]) + "u")
        self.done(pg)

    def test_chips_are_real_buttons_with_aria_pressed(self):
        pg = self.open()
        n = pg.evaluate("""()=>[...document.querySelectorAll('#analytics-trends-content .vz-chip:not([data-tva=more]):not([data-tva=nmmore])')].filter(b=>b.tagName!=='BUTTON'||!['true','false'].includes(b.getAttribute('aria-pressed'))).length""")
        self.assertEqual(n, 0)
        self.assertGreater(pg.locator("#analytics-trends-content .vz-chip").count(), 20)
        self.assertEqual(pg.locator(".vz-filter [data-tva=flt][aria-pressed=true]").get_attribute("data-v"), "ALL")
        self.done(pg)

    # ---------------------------------------------------------------- filters
    def test_sport_chip_replots_every_section(self):
        pg = self.open()
        hk = [p for p in ordered(LEDGER) if p["sport"] in GROUP["HOCKEY"]]
        pg.click(".vz-filter [data-v=HOCKEY]"); pg.wait_for_timeout(300)
        a = agg(hk)
        self.assertEqual(self.stat(pg, "BETS"), str(len(hk)))
        self.assertEqual(self.stat(pg, "RECORD"), rec(a))
        self.assertEqual(self.stat(pg, "UNITS"), sgn(a["u"]) + "u")
        self.assertIn("%d OF %d" % (len(hk), len(LEDGER)), pg.locator("#tv-count").inner_text())
        sa = pg.locator("#tv-c-sa .tv-row").all_inner_texts()
        self.assertEqual(len(sa), 2)
        self.assertTrue(all(("NHL" in t or "SHL" in t) for t in sa), sa)
        self.assertEqual(pg.locator("#tv-c-rec .tv-rc").count(), 2)
        self.assertEqual(sorted(x.get_attribute("data-k") for x in pg.locator("#tv-c-hot .tv-row").all()).count("NFL_ML"), 0)
        self.assertEqual(pg.evaluate("document.activeElement.dataset.v"), "HOCKEY")          # focus survives the re-render
        self.assertEqual(pg.locator(".vz-filter [data-tva=flt][aria-pressed=true]").get_attribute("data-v"), "HOCKEY")
        pg.click(".vz-filter [data-v=ALL]"); pg.wait_for_timeout(300)
        self.assertEqual(self.stat(pg, "BETS"), str(len(LEDGER)))
        self.done(pg)

    def test_period_chips_recompute_numbers(self):
        pg = self.open()
        for per, d in (("30D", 30), ("7D", 7), ("90D", 90)):
            want = in_days(ordered(LEDGER), d)
            pg.click(".vz-filter [data-tva=per][data-v='%s']" % per); pg.wait_for_timeout(300)
            a = agg(want)
            self.assertEqual(self.stat(pg, "BETS"), str(len(want)), per)
            self.assertEqual(self.stat(pg, "RECORD"), rec(a), per)
            self.assertEqual(self.stat(pg, "UNITS"), sgn(a["u"]) + "u", per)
            self.assertEqual(pg.locator(".vz-filter [data-tva=per][data-v='%s'] small" % per).inner_text(), str(len(want)))
        self.assertGreater(len(LEDGER), len(in_days(LEDGER, 90)))
        pg.click(".vz-filter [data-tva=per][data-v=ALL]"); pg.wait_for_timeout(300)
        self.assertEqual(self.stat(pg, "BETS"), str(len(LEDGER)))
        self.done(pg)

    def test_period_and_sport_combine(self):
        pg = self.open()
        want = [p for p in in_days(ordered(LEDGER), 30) if p["sport"] in GROUP["FOOTBALL"]]
        pg.click(".vz-filter [data-v=FOOTBALL]"); pg.click(".vz-filter [data-tva=per][data-v='30D']"); pg.wait_for_timeout(300)
        self.assertEqual(self.stat(pg, "BETS"), str(len(want)))
        self.assertIn("%d OF %d" % (len(want), len(LEDGER)), pg.locator("#tv-count").inner_text())
        # a fixed-window section keeps its sport slice but ignores the period chip
        nfl_all = [p for p in ordered(LEDGER) if p["sport"] == "NFL"]
        self.assertEqual(pg.locator("#tv-c-rec .vz-dots .vz-dot").count(), min(10, len(nfl_all)))
        self.done(pg)

    # ---------------------------------------------------------------- rolling chart
    def test_rolling_hover_tooltip_has_the_right_window(self):
        pg = self.open()
        self.hover_in(pg, "#tv-c-sum .tv-chart", 0.6)
        t = self.tip(pg)
        m = re.search(r"BET (\d+) OF (\d+)", t)
        self.assertTrue(m, t)
        i, n = int(m.group(1)), int(m.group(2))
        self.assertEqual(n, len(LEDGER))
        led = ordered(LEDGER)
        win = led[i - 25:i]
        a = agg(win)
        got = float(re.search(r"ROLLING 25\s*([\d.]+)%", t).group(1))
        self.assertAlmostEqual(got, 100.0 * a["w"] / 25, delta=0.06)
        self.assertIn("%dW–%dL" % (a["w"], a["l"]), t)
        self.assertIn(led[i - 1]["awA"] + " @ " + led[i - 1]["hA"], t)
        avg = 100.0 * agg(led)["w"] / n
        self.assertAlmostEqual(float(re.search(r"SLICE AVERAGE\s*([\d.]+)%", t).group(1)), avg, delta=0.06)
        self.assertTrue(pg.locator("#tv-c-sum .tv-xh").evaluate("e=>e.style.display!=='none'"))
        box = pg.locator("#tv-tip").bounding_box()
        self.assertTrue(box["x"] >= 0 and box["x"] + box["width"] <= 1300)
        pg.mouse.move(5, 5); pg.wait_for_timeout(200)
        self.assertEqual(self.tip(pg), "")
        self.done(pg)

    def test_rolling_window_chips_and_pin(self):
        pg = self.open()
        pg.click("#tv-c-sum [data-tva=roll][data-v='10']"); pg.wait_for_timeout(300)
        self.assertEqual(pg.locator("#tv-c-sum [data-tva=roll][aria-pressed=true]").inner_text(), "10 BETS")
        self.assertIn("Rolling 10-bet", pg.locator("#tv-c-sum .tv-chart").get_attribute("aria-label"))
        self.hover_in(pg, "#tv-c-sum .tv-chart", 0.3)
        self.assertIn("ROLLING 10", self.tip(pg))
        host = pg.locator("#tv-c-sum .tv-chart")
        bb = host.bounding_box()
        pg.mouse.click(bb["x"] + bb["width"] * 0.5, bb["y"] + 80); pg.wait_for_timeout(250)
        det = pg.locator("#tv-c-sum .vz-detail").inner_text()
        m = re.search(r"BETS (\d+)–(\d+)", det)
        self.assertTrue(m, det)
        lo, hi = int(m.group(1)), int(m.group(2))
        self.assertEqual(hi - lo + 1, 10)
        a = agg(ordered(LEDGER)[lo - 1:hi])
        self.assertIn(rec(a), det)
        self.assertEqual(pg.locator("#tv-c-sum .vz-detail .vz-rp").count(), 10)
        self.assertTrue(pg.locator("#tv-c-sum .tv-pinm").evaluate("e=>e.style.display!=='none'"))
        pg.click("#tv-c-sum [data-tva=unpin]"); pg.wait_for_timeout(150)
        self.assertIn("TAP OR CLICK THE LINE", pg.locator("#tv-c-sum .vz-detail").inner_text())
        self.done(pg)

    def test_rolling_keyboard_scrub_and_pin(self):
        pg = self.open()
        host = pg.locator("#tv-c-sum .tv-chart")
        host.scroll_into_view_if_needed(); host.focus()
        pg.keyboard.press("End"); pg.keyboard.press("ArrowLeft")
        self.assertEqual(host.get_attribute("aria-valuenow"), str(len(LEDGER) - 1))
        self.assertIn("BET %d OF %d" % (len(LEDGER) - 1, len(LEDGER)), self.tip(pg))
        pg.keyboard.press("Enter"); pg.wait_for_timeout(150)
        self.assertIn("PINNED", pg.locator("#tv-c-sum .vz-detail").inner_text())
        pg.keyboard.press("Escape")
        self.assertEqual(self.tip(pg), "")
        self.assertEqual(host.get_attribute("role"), "slider")
        self.done(pg)

    def test_rolling_legend_toggles(self):
        pg = self.open()
        pg.click("#tv-c-sum [data-tva=leg][data-v=band]")
        self.assertEqual(pg.locator("#tv-c-sum [data-v=band]").get_attribute("aria-pressed"), "false")
        self.expect_op(pg, "#tv-c-sum .tv-band", "0")
        pg.click("#tv-c-sum [data-tva=leg][data-v=band]")
        self.expect_op(pg, "#tv-c-sum .tv-band", "1")
        pg.click("#tv-c-sum [data-tva=leg][data-v=avg]")
        self.expect_op(pg, "#tv-c-sum .tv-avg", "0")
        self.done(pg)

    # ---------------------------------------------------------------- hot / cold
    def combos(self, bets, minn=3):
        c = {}
        for p in ordered(bets):
            k = p["sport"] + "_" + p["betType"]
            c.setdefault(k, []).append(p)
        return [(k, v) for k, v in c.items() if len(v) >= minn]

    def test_hot_cold_ranking_matches_fixture(self):
        pg = self.open()
        lst = sorted(self.combos(LEDGER), key=lambda kv: -(agg(kv[1])["w"] / len(kv[1])))
        keys = lambda col: [x.get_attribute("data-k") for x in pg.locator("#tv-c-hot .tv-col").nth(col).locator(".tv-row").all()]
        self.assertEqual(keys(0), [k for k, _ in lst[:5]])
        self.assertEqual(keys(1), [k for k, _ in lst[-5:][::-1]])
        row = pg.locator("#tv-c-hot .tv-row").first
        a = agg(lst[0][1])
        self.assertIn(rec(a), row.inner_text())
        row.hover(); pg.wait_for_timeout(150)
        t = self.tip(pg)
        self.assertIn("WIN RATE", t); self.assertIn("ROI", t); self.assertIn("RECORD", t)
        pg.click("#tv-c-hot [data-tva=hsort][data-v=u]"); pg.wait_for_timeout(200)
        lst = sorted(self.combos(LEDGER), key=lambda kv: -agg(kv[1])["u"])
        self.assertEqual(keys(0), [k for k, _ in lst[:5]])
        pg.click("#tv-c-hot [data-tva=hmin][data-v='10']"); pg.wait_for_timeout(200)
        lst = sorted(self.combos(LEDGER, 10), key=lambda kv: -agg(kv[1])["u"])
        nh = min(5, (len(lst) + 1) // 2)
        self.assertEqual(keys(0), [k for k, _ in lst[:nh]])
        self.done(pg)

    def test_hot_row_drill_lists_the_picks(self):
        pg = self.open()
        row = pg.locator("#tv-c-hot .tv-row").first
        key = row.get_attribute("data-k")
        want = [p for p in ordered(LEDGER) if p["sport"] + "_" + p["betType"] == key]
        row.click(); pg.wait_for_timeout(250)
        d = pg.locator("#tv-c-hot .tv-drill")
        txt = d.inner_text()
        a = agg(want)
        self.assertIn(rec(a), txt)
        self.assertIn("%.1f%%" % (100.0 * a["acc"]), txt)
        self.assertEqual(d.locator(".vz-rp").count(), min(10, len(want)))
        first = d.locator(".vz-rp").first.inner_text()
        self.assertIn(want[-1]["betOn"], first)                                  # newest first
        self.assertEqual(pg.locator("#tv-c-hot .tv-row[aria-pressed=true]").get_attribute("data-k"), key)
        if len(want) > 10:
            pg.click("#tv-c-hot [data-tva=more]"); pg.wait_for_timeout(150)
            self.assertEqual(d.locator(".vz-rp").count(), min(20, len(want)))
        pg.click("#tv-c-hot [data-tva=drillx]"); pg.wait_for_timeout(200)
        self.assertIn("TAP A ROW", pg.locator("#tv-c-hot").inner_text())
        self.done(pg)

    # ---------------------------------------------------------------- recency
    def test_recency_dots_tooltip_pin_and_legend(self):
        pg = self.open()
        nba = [p for p in ordered(LEDGER) if p["sport"] == "NBA"][-10:]
        card = pg.locator("#tv-c-rec .tv-rc").first
        self.assertEqual(card.locator(".vz-dot").count(), 10)
        w = sum(1 for p in nba if p["outcome"] == "win")
        self.assertIn("%d%%" % rnd0(100.0 * w / 10), card.inner_text())
        dot = card.locator(".vz-dot").last
        dot.scroll_into_view_if_needed(); dot.hover(); pg.wait_for_timeout(150)
        t = self.tip(pg)
        self.assertIn(nba[-1]["betOn"], t)
        self.assertRegex(t, r"RESULT\s*(WIN|LOSS|PUSH)")
        dot.click(); pg.wait_for_timeout(150)
        self.assertEqual(pg.locator("#tv-c-rec .vz-detail:not(:empty)").count(), 1)
        self.assertIn("GAME", pg.locator("#tv-c-rec .vz-detail:not(:empty)").inner_text())
        pg.click("#tv-c-rec [data-tva=leg][data-v=sw]")
        self.expect_op(pg, "#tv-c-rec .vz-dot[data-o=win]", "0.1")
        pg.click("#tv-c-rec [data-tva=rn][data-v='20']"); pg.wait_for_timeout(250)
        self.assertEqual(pg.locator("#tv-c-rec .tv-rc").first.locator(".vz-dot").count(), 20)
        self.done(pg)

    def test_recency_roving_arrow_keys(self):
        pg = self.open()
        dot = pg.locator("#tv-c-rec .vz-dot").nth(3)
        dot.scroll_into_view_if_needed(); dot.focus()
        pg.keyboard.press("ArrowRight")
        self.assertEqual(pg.locator("#tv-c-rec .vz-dot").nth(4).evaluate("e=>document.activeElement===e"), True)
        self.done(pg)

    def test_recency_sort_by_win_rate(self):
        pg = self.open()
        pg.click("#tv-c-rec [data-tva=rsort][data-v=acc]"); pg.wait_for_timeout(250)
        pcts = [int(x) for x in re.findall(r"(\d+)%", " ".join(pg.locator("#tv-c-rec .tv-rt .tv-pv").all_inner_texts()))]
        self.assertEqual(pcts, sorted(pcts, reverse=True))
        self.done(pg)

    # ---------------------------------------------------------------- recent vs historical
    def cmp_numbers(self, sport, days):
        bets = [p for p in ordered(LEDGER) if p["sport"] == sport]
        rb = [p for p in bets if age_h(p) < days * 24]
        hb = [p for p in bets if age_h(p) >= days * 24]
        return agg(rb), agg(hb)

    def test_recent_vs_historical_numbers_and_window_chip(self):
        pg = self.open()
        for days in (30, 7):
            if days != 30:
                pg.click("#tv-c-cmp [data-tva=cmpw][data-v='%d']" % days); pg.wait_for_timeout(250)
            self.assertIn("RECENT (%dD) VS HISTORICAL TRENDS" % days, pg.locator("#tv-c-cmp").inner_text().upper())
            for sport in ("NBA", "NFL", "PL"):
                r, h = self.cmp_numbers(sport, days)
                row = pg.locator("#tv-c-cmp .tv-tr[data-k=%s]" % sport).inner_text().replace("\n", " ")
                if r["n"]:
                    self.assertIn("%d%% (%d)" % (rnd0(100.0 * r["acc"]), r["n"]), row)
                else:
                    self.assertIn("— (0)", row)
                if h["n"]:
                    self.assertIn("%d%% (%d)" % (rnd0(100.0 * h["acc"]), h["n"]), row)
                if r["n"] >= 2 and h["n"] >= 2:
                    d = r["acc"] - h["acc"]
                    self.assertIn(("+" if d > 0 else "") + str(rnd0(100.0 * d)) + "%", row)
        self.done(pg)

    def test_recent_vs_historical_sort_and_drill(self):
        pg = self.open()
        pg.click("#tv-c-cmp [data-tva=cmps][data-v=rp]"); pg.wait_for_timeout(200)
        r = [float(x) for x in re.findall(r"(\d+)% \(", " ".join(t.replace("\n", " ").split("%")[0] + "% (" for t in [pg.locator("#tv-c-cmp .tv-tr[data-k]").nth(i).locator(".tv-cc").nth(0).inner_text() for i in range(pg.locator("#tv-c-cmp .tv-tr[data-k]").count())]))]
        self.assertEqual(r, sorted(r, reverse=True))
        self.assertEqual(pg.locator("#tv-c-cmp [data-tva=cmps][data-v=rp]").get_attribute("aria-sort"), "descending")
        pg.click("#tv-c-cmp [data-tva=cmps][data-v=rp]"); pg.wait_for_timeout(200)
        self.assertEqual(pg.locator("#tv-c-cmp [data-tva=cmps][data-v=rp]").get_attribute("aria-sort"), "ascending")
        pg.click("#tv-c-cmp [data-tva=cmps][data-v=sp]"); pg.wait_for_timeout(200)
        names = pg.locator("#tv-c-cmp .tv-tr[data-k] .tv-lg").all_inner_texts()
        self.assertEqual(names, sorted(names))
        pg.click("#tv-c-cmp .tv-tr[data-k=NHL]"); pg.wait_for_timeout(250)
        rr, hh = self.cmp_numbers("NHL", 30)
        d = pg.locator("#tv-c-cmp .tv-drill")
        self.assertIn(rec(rr), d.inner_text())
        pg.click("#tv-c-cmp [data-tva=cmpset][data-v=hist]"); pg.wait_for_timeout(250)
        self.assertIn(rec(hh), pg.locator("#tv-c-cmp .tv-drill").inner_text())
        self.done(pg)

    # ---------------------------------------------------------------- near misses
    def test_near_miss_counts_filter_sort_and_expand(self):
        pg = self.open()
        nm = near_misses(ordered(LEDGER))
        self.assertGreater(len(nm), 12)
        self.assertEqual(int(pg.locator("#tv-c-nm [data-tva=nmt][data-v=ALL] small").inner_text()), len(nm))
        for k in ("ML", "SPREAD", "OU", "PROP"):
            self.assertEqual(int(pg.locator("#tv-c-nm [data-tva=nmt][data-v=%s] small" % k).inner_text()), len([1 for kk, _ in nm if kk == k]), k)
        self.assertEqual(pg.locator("#tv-c-nm .tv-xr").count(), 12)
        first = pg.locator("#tv-c-nm .tv-xb").first.inner_text()
        self.assertIn(nm[-1][1]["date"], first)                                  # newest first
        pg.click("#tv-c-nm [data-tva=nmt][data-v=ML]"); pg.wait_for_timeout(200)
        mls = [p for k, p in nm if k == "ML"]
        self.assertEqual(pg.locator("#tv-c-nm .tv-xr").count(), min(12, len(mls)))
        self.assertIn("SHOWING %d OF %d" % (min(12, len(mls)), len(mls)), pg.locator("#tv-c-nm").inner_text())
        btn = pg.locator("#tv-c-nm .tv-xb").first
        self.assertEqual(btn.get_attribute("aria-expanded"), "false")
        btn.click(); pg.wait_for_timeout(150)
        self.assertEqual(btn.get_attribute("aria-expanded"), "true")
        self.assertTrue(pg.locator("#tv-c-nm .tv-xd").first.is_visible())
        self.assertIn("FINAL", pg.locator("#tv-c-nm .tv-xd").first.inner_text())
        btn.click(); pg.wait_for_timeout(150)
        self.assertFalse(pg.locator("#tv-c-nm .tv-xd").first.is_visible())
        pg.click("#tv-c-nm [data-tva=nmt][data-v=ALL]"); pg.click("#tv-c-nm [data-tva=nmmore]"); pg.wait_for_timeout(200)
        self.assertEqual(pg.locator("#tv-c-nm .tv-xr").count(), min(24, len(nm)))
        pg.click("#tv-c-nm [data-tva=nms][data-v=closest]"); pg.wait_for_timeout(200)
        self.assertEqual(pg.locator("#tv-c-nm [data-tva=nms][aria-pressed=true]").get_attribute("data-v"), "closest")
        self.done(pg)

    # ---------------------------------------------------------------- calibration
    def test_calibration_bands_match_fixture_and_drill(self):
        pg = self.open()
        bands = [("PREMIUM 67%+", .67, 1), ("OPTIMAL 62-67%", .62, .67), ("LEAN 55-62%", .55, .62), ("SKIP <55%", 0, .55)]
        rows = pg.locator("#tv-c-cal .tv-cb")
        self.assertEqual(rows.count(), 4)
        for i, (lbl, lo, hi) in enumerate(bands):
            bb = [p for p in LEDGER if lo <= p["winProb"] < hi]
            a = agg(bb)
            txt = rows.nth(i).inner_text().replace("\n", " ")
            self.assertIn(lbl, txt)
            self.assertIn("%d bets" % len(bb), txt)
            if bb:
                self.assertIn("%d%%" % rnd0(100.0 * a["acc"]), txt)
        r0 = rows.first
        r0.hover(); pg.wait_for_timeout(150)
        t = self.tip(pg)
        for k in ("ACTUAL WIN RATE", "TARGET", "DRIFT", "95% BAND", "RECORD"):
            self.assertIn(k, t)
        bb = [p for p in LEDGER if .67 <= p["winProb"] < 1]
        self.assertIn(rec(agg(bb)), t)
        r0.click(); pg.wait_for_timeout(250)
        self.assertIn(rec(agg(bb)), pg.locator("#tv-c-cal .tv-drill").inner_text())
        self.assertEqual(pg.locator("#tv-c-cal .tv-cb[aria-pressed=true]").count(), 1)
        pg.click("#tv-c-cal [data-tva=leg][data-v=ci]")
        self.expect_op(pg, "#tv-c-cal .tv-ci", "0")
        pg.click("#tv-c-cal [data-tva=leg][data-v=tgt]")
        self.expect_op(pg, "#tv-c-cal .tv-tg", "0")
        self.done(pg)

    # ---------------------------------------------------------------- self-assessment / bet type
    def test_self_assessment_values_sort_and_callouts(self):
        pg = self.open()
        stats = {}
        for lg in ("NBA", "NHL", "SHL", "NFL", "PL"):
            stats[lg] = agg([p for p in LEDGER if p["sport"] == lg])
        rows = pg.locator("#tv-c-sa .tv-row")
        self.assertEqual(rows.count(), 5)
        order = [x.get_attribute("data-k")[3:] for x in rows.all()]
        self.assertEqual(order, sorted(stats, key=lambda k: -stats[k]["acc"]))
        top = order[0]
        self.assertIn("%.1f%%" % (100.0 * stats[top]["acc"]), rows.first.inner_text())
        self.assertIn(rec(stats[top]), rows.first.inner_text())
        pg.click("#tv-c-sa [data-tva=sasort][data-v=u]"); pg.wait_for_timeout(200)
        order = [x.get_attribute("data-k")[3:] for x in pg.locator("#tv-c-sa .tv-row").all()]
        self.assertEqual(order, sorted(stats, key=lambda k: -stats[k]["u"]))
        pg.click("#tv-c-sa [data-tva=sasort][data-v=n]"); pg.wait_for_timeout(200)
        order = [x.get_attribute("data-k")[3:] for x in pg.locator("#tv-c-sa .tv-row").all()]
        self.assertEqual([stats[k]["n"] for k in order], sorted([stats[k]["n"] for k in order], reverse=True))
        best = max(stats, key=lambda k: stats[k]["acc"])
        self.assertIn("STRONGEST: %s — %d%%" % (best, rnd0(100.0 * stats[best]["acc"])), pg.locator("#tv-c-sa").inner_text())
        pg.locator("#tv-c-sa .tv-call").first.click(); pg.wait_for_timeout(250)
        self.assertIn(best, pg.locator("#tv-c-sa .tv-drill").inner_text())
        self.assertIn(rec(stats[best]), pg.locator("#tv-c-sa .tv-drill").inner_text())
        self.done(pg)

    def test_bet_type_tiles_fold_spread_and_drill(self):
        pg = self.open()
        want = {}
        for t in ("ML", "SPREAD", "OU", "PROP"):
            want[t] = agg([p for p in LEDGER if p["betType"] == t])
        tiles = pg.locator("#tv-c-bt .tv-tile")
        self.assertEqual(tiles.count(), 4)
        sp = pg.locator("#tv-c-bt .tv-tile[data-k=Spread]")
        self.assertIn(rec(want["SPREAD"]), sp.inner_text())
        self.assertIn(sgn(want["SPREAD"]["u"]) + "u", sp.inner_text())
        sp.hover(); pg.wait_for_timeout(150)
        self.assertIn("WIN RATE", self.tip(pg))
        sp.click(); pg.wait_for_timeout(250)
        self.assertEqual(pg.locator("#tv-c-bt .tv-tile[aria-pressed=true]").get_attribute("data-k"), "Spread")
        self.assertIn(rec(want["SPREAD"]), pg.locator("#tv-c-bt .tv-drill").inner_text())
        self.done(pg)

    # ---------------------------------------------------------------- win / loss analysis
    def test_win_loss_analysis_counts_and_factor_filter(self):
        pg = self.open()
        led = ordered(LEDGER)
        wins = [p for p in led if p["outcome"] == "win"][-8:]
        self.assertEqual(pg.locator("#tv-c-win .tv-xr").count(), 8)
        self.assertIn(wins[-1]["date"], pg.locator("#tv-c-win .tv-xb").first.inner_text())
        pg.click("#tv-c-win [data-tva=winn][data-v='16']"); pg.wait_for_timeout(200)
        self.assertEqual(pg.locator("#tv-c-win .tv-xr").count(), 16)
        chip = pg.locator("#tv-c-win [data-tva=winf]").first
        n = int(chip.locator("small").inner_text())
        chip.click(); pg.wait_for_timeout(200)
        self.assertEqual(pg.locator("#tv-c-win .tv-xr").count(), n)
        self.assertIn("SHOWING %d OF 16" % n, pg.locator("#tv-c-win").inner_text())
        pg.click("#tv-c-win [data-tva=winf][aria-pressed=true]"); pg.wait_for_timeout(200)
        self.assertEqual(pg.locator("#tv-c-win .tv-xr").count(), 16)
        self.assertEqual(pg.locator("#tv-c-loss .tv-xr").count(), 8)
        pg.click("#tv-c-loss [data-tva=lossn][data-v='30']"); pg.wait_for_timeout(200)
        self.assertEqual(pg.locator("#tv-c-loss .tv-xr").count(), 30)
        self.done(pg)

    # ---------------------------------------------------------------- journal + props
    def test_journal_matches_analyze_journal_reasons(self):
        pg = self.open()
        want = pg.evaluate("analyzeJournalReasons().map(([r,d])=>[r,d.n,d.w])")
        rows = pg.locator("#tv-c-jr .tv-row")
        self.assertEqual(rows.count(), len(want))
        self.assertEqual([x.get_attribute("data-k") for x in rows.all()], [w[0] for w in want])
        for r, n, w in want:
            py = [p for p in LEDGER if p.get("reason") == r]
            self.assertEqual(len(py), n)
            self.assertEqual(sum(1 for p in py if p["outcome"] == "win"), w)
        pg.click("#tv-c-jr [data-tva=jrs][data-v=n]"); pg.wait_for_timeout(200)
        ns = [int(re.search(r"(\d+) bets", x).group(1)) for x in pg.locator("#tv-c-jr .tv-row").all_inner_texts()]
        self.assertEqual(ns, sorted(ns, reverse=True))
        pg.locator("#tv-c-jr .tv-row").first.click(); pg.wait_for_timeout(250)
        self.assertGreaterEqual(pg.locator("#tv-c-jr .tv-drill .vz-rp").count(), 1)
        # slice: the journal follows the sport chip
        pg.click(".vz-filter [data-v=HOCKEY]"); pg.wait_for_timeout(300)
        hk = [p for p in LEDGER if p["sport"] in GROUP["HOCKEY"] and p.get("reason")]
        tot = sum(int(re.search(r"(\d+) bets", x).group(1)) for x in pg.locator("#tv-c-jr .tv-row").all_inner_texts())
        by = {}
        for p in hk:
            by.setdefault(p["reason"], []).append(p)
        self.assertEqual(tot, sum(len(v) for v in by.values() if len(v) >= 2))
        self.done(pg)

    def test_player_props_rows_sort_and_drill(self):
        pg = self.open()
        props = [p for p in LEDGER if p["betType"] == "PROP"]
        a = agg(props)
        self.assertIn("%dW/%dL" % (a["w"], a["l"]), pg.locator("#tv-c-pr .vz-note").inner_text())
        self.assertIn("%d props" % len(props), pg.locator("#tv-c-pr .vz-note").inner_text())
        by = {}
        for p in ordered(props):
            by.setdefault(re.sub(r"(OVER|UNDER).*", "", p["betOn"]).strip(), []).append(p)
        names = [x.get_attribute("data-k") for x in pg.locator("#tv-c-pr .tv-row").all()]
        self.assertEqual(sorted(names), sorted(k for k, v in by.items() if len(v) >= 2))
        pg.click("#tv-c-pr [data-tva=prs][data-v=wp]"); pg.wait_for_timeout(200)
        pcts = [int(re.search(r"(\d+)%", x).group(1)) for x in pg.locator("#tv-c-pr .tv-row").all_inner_texts()]
        self.assertEqual(pcts, sorted(pcts, reverse=True))
        row = pg.locator("#tv-c-pr .tv-row").first
        k = row.get_attribute("data-k")
        row.click(); pg.wait_for_timeout(250)
        self.assertEqual(pg.locator("#tv-c-pr .tv-drill .vz-rp").count(), min(10, len(by[k])))
        self.done(pg)

    # ---------------------------------------------------------------- price basis, empty state, touch, layout
    def test_price_basis_real_only_changes_units_not_record(self):
        pg = self.open()
        before = self.stat(pg, "RECORD")
        pg.evaluate("setPriceBasis('real')"); pg.wait_for_timeout(500)
        real = [p for p in ordered(LEDGER) if p.get("priceSource") == "market"]
        self.assertEqual(self.stat(pg, "RECORD"), before)
        self.assertEqual(self.stat(pg, "UNITS"), sgn(sum(map(pnl, real))) + "u")
        self.assertEqual(self.stat(pg, "BETS"), str(len(LEDGER)))
        pg.evaluate("setPriceBasis('all')"); pg.wait_for_timeout(500)
        self.assertEqual(self.stat(pg, "UNITS"), sgn(agg(LEDGER)["u"]) + "u")
        self.done(pg)

    def test_tiny_and_empty_ledgers_degrade_gracefully(self):
        pg = self.open(ledger=LEDGER[:4])
        self.assertIn("NOT ENOUGH DATA", pg.locator("#analytics-trends-content").inner_text())
        self.done(pg)
        pg = self.open(ledger=[])
        self.assertIn("NOT ENOUGH DATA", pg.locator("#analytics-trends-content").inner_text())
        self.done(pg)

    def test_slice_with_no_matching_bets_shows_empty_states(self):
        led = [p for p in LEDGER if p["sport"] != "PL"]
        pg = self.open(ledger=led)
        pg.click(".vz-filter [data-v=SOCCER]"); pg.wait_for_timeout(300)
        self.assertIn("NOT ENOUGH DATA YET", pg.locator("#tv-c-sum").inner_text())
        self.assertIn("NOT ENOUGH DATA YET", pg.locator("#tv-c-sa").inner_text())
        pg.click(".vz-filter [data-v=ALL]"); pg.click(".vz-filter [data-tva=per][data-v='7D']"); pg.wait_for_timeout(300)
        self.assertEqual(pg.locator("#tv-c-sum .tv-chart").count(), 0)           # too few bets for a 25-bet rolling line
        self.assertIn("NEED MORE THAN", pg.locator("#tv-c-sum").inner_text())
        self.done(pg)

    def test_touch_tap_shows_tooltips_and_taps_elsewhere_hide_them(self):
        pg = self.open(width=390, height=14000, touch=True)
        host = pg.locator("#tv-c-sum .tv-chart")
        bb = host.bounding_box()
        pg.touchscreen.tap(bb["x"] + bb["width"] * 0.6, bb["y"] + 60); pg.wait_for_timeout(250)
        self.assertRegex(self.tip(pg), r"BET \d+ OF %d" % len(LEDGER))
        tb = pg.locator("#tv-tip").bounding_box()
        self.assertTrue(tb["x"] >= 0 and tb["x"] + tb["width"] <= 390)
        pg.touchscreen.tap(190, 3); pg.wait_for_timeout(200)
        self.assertEqual(self.tip(pg), "")
        row = pg.locator("#tv-c-sa .tv-row").first
        row.tap(); pg.wait_for_timeout(250)
        self.assertIn("RECORD", self.tip(pg) or pg.locator("#tv-c-sa .tv-drill").inner_text())
        self.assertGreaterEqual(pg.locator("#tv-c-sa .tv-drill .vz-rp").count(), 1)       # the same tap drilled in
        dot = pg.locator("#tv-c-rec .vz-dot").nth(3)
        dot.tap(); pg.wait_for_timeout(250)
        self.assertRegex(pg.locator("#tv-c-rec .vz-detail:not(:empty)").inner_text(), r"WIN|LOSS|PUSH")
        self.done(pg)

    def test_no_horizontal_scroll_at_390_and_readable_text(self):
        pg = self.open(width=390, height=900)
        r = pg.evaluate("""()=>{const c=document.getElementById('analytics-trends-content');
          const worst=[...c.querySelectorAll('*')].filter(e=>e.getBoundingClientRect().right>391&&getComputedStyle(e).position!=='fixed').map(e=>e.className||e.tagName).slice(0,5);
          const small=[...c.querySelectorAll('.tv-row *,.tv-tr *,.tv-xb *,.tv-call *,.tv-cb *,.tv-tile *,.vz-chip *,.tv-rc *,.vz-note,.vz-sub,.tv-hdr')].filter(e=>!e.children.length&&e.textContent.trim()&&parseFloat(getComputedStyle(e).fontSize)<9.5).map(e=>e.className+':'+getComputedStyle(e).fontSize).slice(0,8);
          return {doc:document.documentElement.scrollWidth,body:document.body.scrollWidth,c:c.scrollWidth,cw:c.clientWidth,worst,small}}""")
        self.assertLessEqual(r["doc"], 390, r); self.assertLessEqual(r["body"], 390, r); self.assertLessEqual(r["c"], r["cw"], r)
        self.assertEqual(r["worst"], [], r)
        self.assertEqual(r["small"], [], r)
        for k in SLOTS:
            self.assertTrue(pg.locator("#tv-c-" + k).inner_text().strip(), k)
        self.done(pg)

    def test_big_touch_targets_at_390(self):
        pg = self.open(width=390, height=900)
        small = pg.evaluate("""()=>[...document.querySelectorAll('#analytics-trends-content button')].filter(b=>b.getBoundingClientRect().height<34&&!b.classList.contains('vz-dot')).map(b=>b.className+':'+Math.round(b.getBoundingClientRect().height)).slice(0,5)""")
        self.assertEqual(small, [])
        self.done(pg)

    def test_keyboard_focus_shows_row_tooltip(self):
        pg = self.open()
        row = pg.locator("#tv-c-sa .tv-row").first
        row.scroll_into_view_if_needed()
        pg.locator("#tv-c-sa [data-tva=sasort][data-v=n]").focus()
        pg.keyboard.press("Tab")            # keyboard focus (focus-visible) lands on the first row
        self.assertIn("WIN RATE", self.tip(pg))
        pg.keyboard.press("Enter"); pg.wait_for_timeout(250)
        self.assertGreaterEqual(pg.locator("#tv-c-sa .tv-drill .vz-rp").count(), 1)
        pg.keyboard.press("Escape")
        self.done(pg)

    def test_reduced_motion_disables_animation(self):
        ctx = self.b.new_context(viewport={"width": 1300, "height": 3000}, reduced_motion="reduce")
        pg = ctx.new_page()
        self.block_fonts(pg)
        pg.goto("http://127.0.0.1:%d/app.html?nosb=1" % self.srv.server_address[1], wait_until="domcontentloaded", timeout=60000)
        pg.wait_for_function("typeof saveP==='function'&&typeof navTap==='function'")
        pg.evaluate("d=>saveP(d)", LEDGER)
        pg.evaluate("""()=>{navTap(document.querySelector('[onclick*="analytics"]'),'analytics');setSub('analytics','trends');renderAnalyticsTrends();try{hideAllND()}catch(e){}}""")
        pg.wait_for_selector("#tv-c-sum .tv-line")
        self.assertEqual(pg.locator("#tv-c-sum .tv-line").evaluate("e=>getComputedStyle(e).animationName"), "none")
        self.assertEqual(pg.locator("#tv-c-hot .tv-bar").first.evaluate("e=>getComputedStyle(e).animationName"), "none")
        ctx.close()

    def test_first_render_animates_and_settles(self):
        pg = self.open()
        self.assertEqual(pg.locator("#tv-c-sum .tv-line").evaluate("e=>getComputedStyle(e).animationName"), "vz-draw")
        self.assertEqual(pg.locator("#tv-c-sum .tv-line").evaluate("e=>getComputedStyle(e).strokeDashoffset"), "0px")   # animation finished
        self.done(pg)

    def test_aria_labels_on_charts_and_rows(self):
        pg = self.open()
        self.assertTrue((pg.locator("#tv-c-sum .tv-chart").get_attribute("aria-label") or "").strip())
        self.assertEqual(pg.locator("#tv-c-sum .tv-chart").get_attribute("tabindex"), "0")
        for sel in ("#tv-c-hot .tv-row", "#tv-c-cal .tv-cb", "#tv-c-bt .tv-tile", "#tv-c-cmp .tv-tr[data-k]", "#tv-c-rec .vz-dot"):
            self.assertTrue((pg.locator(sel).first.get_attribute("aria-label") or "").strip(), sel)
        self.done(pg)

    def test_visuals_tab_is_untouched_by_trends(self):
        pg = self.open()
        pg.evaluate("()=>{setSub('analytics','visuals');renderAnalyticsVisuals()}"); pg.wait_for_timeout(800)
        self.assertGreater(pg.locator("#analytics-visuals-content .vz-filter").count(), 0)
        pg.evaluate("()=>{setSub('analytics','trends');renderAnalyticsTrends()}"); pg.wait_for_timeout(500)
        self.assertEqual(self.stat(pg, "BETS"), str(len(LEDGER)))
        self.done(pg)


if __name__ == "__main__":
    unittest.main(verbosity=2)
