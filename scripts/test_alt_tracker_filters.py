#!/usr/bin/env python3
"""Every filter on the Analytics > ADJ. LINES tab: PERIOD buttons, LEAGUE buttons, the page-wide LOCK TIMING bar and UNITS/ROI (price) BASIS bar, 'show more', and that the tab
re-renders when a bar is toggled.  Expected numbers for every combination are computed here, independently of the app, from a synthetic ledger (no network)."""
import functools, http.server, itertools, re, socketserver, sys, threading, time, unittest
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import alt_line_scorecard as sc  # noqa: E402
from test_alt_tracker import pick  # noqa: E402

MT = ZoneInfo("America/Denver")
NOW = int(time.time() * 1000)
DUR = {"NFL": 195, "CFB": 210, "NHL": 150, "NBA": 150}

# id, sport, bet type, bet, posted, locked line, home score, away score, outcome, days ago locked, timing, price source
SPEC = [
    ("a", "NFL", "SPREAD", "HOM -3.5", -10.0, -3.5, 27, 20, "win", 0, "pre", "estimated"),
    ("b", "CFB", "OU", "UNDER 55.5", 49.5, 55.5, 28, 24, "win", 0, "late", "estimated"),
    ("c", "NHL", "OU", "OVER 3.5", 5.5, 3.5, 3, 2, "win", 0, "unknown", "estimated"),
    ("d", "NFL", "SPREAD", "AWY +9.5", 3.5, 9.5, 30, 20, "loss", 3, "pre", "estimated"),
    ("e", "CFB", "SPREAD", "AWY +10.0", 3.0, 10.0, 30, 20, "push", 3, "late", "market"),
    ("f", "NBA", "OU", "UNDER 235.5", 229.5, 235.5, 110, 105, "win", 3, "unknown", "estimated"),
    ("g", "NFL", "OU", "OVER 38.5", 44.5, 38.5, None, None, "win", 20, "pre", "estimated"),          # settled, no score
    ("h", "NFL", "OU", "UNDER 50.5", 44.5, 50.5, 21, 20, "loss", 20, "late", "market"),              # scores contradict the outcome
    ("i", "CFB", "OU", "OVER 40.5", 46.5, 40.5, 24, 21, "win", 20, "unknown", "market"),
    ("j", "NFL", "SPREAD", "HOM -3.5", -10.0, -3.5, None, None, "pending", 0, "pre", "estimated"),
    ("k", "NHL", "OU", "UNDER 6.5", 5.5, 6.5, 4, 1, "win", 20, "pre", "estimated"),
]


def build():
    picks = []
    for i, (k, sport, bt, bet, posted, line, h, a, out, days, timing, src) in enumerate(SPEC):
        p = pick(i, sport, bt, bet, posted, line, h, a, out, label=None)
        p["id"] = "f" + k
        locked = NOW - int(days * 86400000) - 2 * 3600000
        p["lockedAt"] = locked
        p["date"] = datetime.fromtimestamp(locked / 1000, MT).strftime("%Y-%m-%d")
        p["priceSource"] = src
        if timing == "pre":
            p["startMs"] = locked + 3600000
        elif timing == "late":
            p["startMs"] = locked - 30 * 60000
        picks.append(p)
    return picks


PICKS = build()


def in_period(p, per):
    now = datetime.fromtimestamp(NOW / 1000, MT)
    if per == "today":
        return p["date"] == now.strftime("%Y-%m-%d")
    if per == "7d":
        return p["lockedAt"] >= NOW - 7 * 86400000
    if per == "month":
        return p["lockedAt"] >= int(now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)
    return True


def timing_class(p):
    st = p.get("startMs")
    if not st:
        return "unknown"
    lead = (st - p["lockedAt"]) / 60000
    return "pre" if lead > 0 else "late"


def in_timing(p, tb):
    t = timing_class(p)
    return tb == "all" or (tb == "clean" and t in ("pre", "unknown")) or (tb == "strict" and t == "pre")


def in_price(p, pb):
    real = p["priceSource"] == "market"
    return pb == "all" or (pb == "real" and real) or (pb == "assumed" and not real)


def expect(per, tb, pb, league):
    rows = [p for p in PICKS if in_period(p, per) and in_timing(p, tb)]
    if league:
        rows = [p for p in rows if p["sport"] == league]
    e = dict(n=len(rows), settled=0, pending=0, altW=0, altL=0, altP=0, both=0, bPostW=0, bPostL=0, saved=0, cost=0, uN=0, uAlt=0.0, uPost=0.0)
    for p in rows:
        if p["outcome"] == "pending":
            e["pending"] += 1; continue
        e["settled"] += 1
        e["altW" if p["outcome"] == "win" else "altL" if p["outcome"] == "loss" else "altP"] += 1
        al = p["altLine"]
        ra = sc.grade(p, float(al["line"]))
        post = None if (ra is not None and ra != p["outcome"]) else sc.grade(p, float(al["posted"]))
        if post is None:
            continue
        e["both"] += 1
        e["bPostW"] += post == "win"; e["bPostL"] += post == "loss"
        e["saved"] += (p["outcome"] == "win" and post == "loss"); e["cost"] += (p["outcome"] == "loss" and post == "win")
        if in_price(p, pb):
            e["uN"] += 1
            e["uAlt"] += (p["decOdds"] - 1) if p["outcome"] == "win" else -1 if p["outcome"] == "loss" else 0
            e["uPost"] += (al["postedDec"] - 1) if post == "win" else -1 if post == "loss" else 0
    return e


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a, **k):
        pass


def read(pg):
    """Parse the tab's tiles from its text."""
    t = pg.evaluate("document.getElementById('adjlines-body').innerText")
    t = "\n".join(l.strip() for l in t.split("\n") if l.strip())      # the page text is indented by the template
    out = {"text": t}
    m = re.search(r"ADJUSTED\n(\d+)\n(\d+) settled · (\d+) pending", t)
    out["n"], out["settled"], out["pending"] = (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else (0, 0, 0)
    m = re.search(r"LOCKED ADJ\. LINE\n(\d+)-(\d+)\n", t); out["altW"], out["altL"] = (int(m.group(1)), int(m.group(2))) if m else (0, 0)
    m = re.search(r"SAME PICKS AT POSTED\n(\d+)-(\d+)\n", t); out["bPostW"], out["bPostL"] = (int(m.group(1)), int(m.group(2))) if m else (0, 0)
    m = re.search(r"SAVED BY SHIFT\n(\d+)\nlost at posted, won at locked · cost (\d+)", t); out["saved"], out["cost"] = (int(m.group(1)), int(m.group(2))) if m else (0, 0)
    m = re.search(r"units on those (\d+): locked adj\. ([+-][\d.]+)u \(est\. price\) vs posted ([+-][\d.]+)u", t)
    out["uN"], out["uAlt"], out["uPost"] = (int(m.group(1)), float(m.group(2)), float(m.group(3))) if m else (0, 0.0, 0.0)
    out["empty"] = "NO ADJUSTED-LINE PICKS" in t
    return out


class AltFilters(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        socketserver.TCPServer.allow_reuse_address = True
        cls.srv = socketserver.TCPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=str(ROOT / "docs")))
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.port = cls.srv.server_address[1]
        cls.pw = sync_playwright().start(); cls.browser = cls.pw.chromium.launch()
        cls.pg = cls.browser.new_page(viewport={"width": 1300, "height": 900})
        cls.pg.goto(f"http://127.0.0.1:{cls.port}/app.html?nosb=1")
        cls.pg.wait_for_function("typeof _altRows==='function'&&typeof saveP==='function'"); cls.pg.wait_for_timeout(1200)
        cls.pg.evaluate("(p)=>{saveP(p);window._altPeriod='all';window._altSport=''}", PICKS)
        cls.pg.evaluate("setSub('analytics','adjlines')"); cls.pg.wait_for_timeout(600)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close(); cls.pw.stop(); cls.srv.shutdown()

    def set(self, per="all", tb="all", pb="all", league=""):
        pg = self.pg
        pg.evaluate("([per,tb,pb,lg])=>{_altSetPeriod(per);setTimingBasis(tb);setPriceBasis(pb);_altSetSport(lg)}", [per, tb, pb, league])

    def test_every_combination(self):
        leagues = ["", "NFL", "CFB", "NHL", "NBA"]
        checked = 0
        for per, tb, pb in itertools.product(("all", "month", "7d", "today"), ("all", "clean", "strict"), ("all", "real", "assumed")):
            for lg in leagues:
                self.set(per, tb, pb, lg)
                got = read(self.pg)
                # a league with no picks under the other filters resets to ALL LEAGUES (never a stuck empty view) -> recompute the expectation the way the tab does
                exp_lg = lg if any(p["sport"] == lg and in_period(p, per) and in_timing(p, tb) for p in PICKS) else ""
                exp = expect(per, tb, pb, exp_lg)
                tag = f"period={per} timing={tb} price={pb} league={lg or 'ALL'}"
                if exp["n"] == 0:
                    self.assertTrue(got["empty"], tag); checked += 1; continue
                for k in ("n", "settled", "pending", "altW", "altL", "bPostW", "bPostL", "saved", "cost"):
                    self.assertEqual(got[k], exp[k], f"{k}: {tag}")
                if exp["both"]:
                    self.assertEqual(got["uN"], exp["uN"], f"uN: {tag}")
                    if exp["uN"]:
                        self.assertAlmostEqual(got["uAlt"], exp["uAlt"], delta=0.06, msg=f"uAlt: {tag}")
                        self.assertAlmostEqual(got["uPost"], exp["uPost"], delta=0.06, msg=f"uPost: {tag}")
                checked += 1
        self.assertEqual(checked, 4 * 3 * 3 * 5)
        self.set()

    def test_buttons_are_wired(self):
        pg = self.pg
        self.set()
        def click(sel_js):
            pg.evaluate(sel_js); pg.wait_for_timeout(250)
        # period buttons (in the tab)
        for label, per in (("THIS MONTH", "month"), ("7D", "7d"), ("TODAY", "today"), ("ALL TIME", "all")):
            click("[...document.querySelectorAll('#adjlines-body button')].find(b=>b.textContent.trim()==='%s').click()" % label)
            self.assertEqual(pg.evaluate("window._altPeriod"), per)
            self.assertTrue(pg.evaluate("[...document.querySelectorAll('#adjlines-body button.act')].some(b=>b.textContent.trim()==='%s')" % label), label)
        # league buttons
        click("[...document.querySelectorAll('#adjlines-body button')].find(b=>b.textContent.trim()==='NFL').click()")
        self.assertEqual(pg.evaluate("window._altSport"), "NFL")
        self.assertEqual(read(pg)["n"], expect("all", "all", "all", "NFL")["n"])
        click("[...document.querySelectorAll('#adjlines-body button')].find(b=>b.textContent.trim()==='ALL LEAGUES').click()")
        self.assertEqual(pg.evaluate("window._altSport"), "")
        # the page-wide bars (real buttons) re-render THIS tab
        before = read(pg)
        click("document.querySelector('#tb-bar button[data-tb=\"strict\"]').click()")
        after = read(pg)
        self.assertEqual(after["n"], expect("all", "strict", "all", "")["n"]); self.assertNotEqual(before["n"], after["n"])
        click("document.querySelector('#tb-bar button[data-tb=\"all\"]').click()")
        click("document.querySelector('#pb-bar button[data-pb=\"real\"]').click()")
        self.assertEqual(read(pg)["uN"], expect("all", "all", "real", "")["uN"])
        click("document.querySelector('#pb-bar button[data-pb=\"all\"]').click()")
        self.assertEqual(read(pg)["n"], len(PICKS))

    def test_league_filter_resets_when_empty_in_period(self):
        pg = self.pg; self.set("all", "all", "all", "NBA")
        self.assertEqual(pg.evaluate("window._altSport"), "NBA")
        pg.evaluate("_altSetPeriod('today')")                       # the NBA pick is 3 days old: no NBA picks today
        self.assertEqual(pg.evaluate("window._altSport"), "")
        self.assertFalse(read(pg)["empty"])
        self.set()

    def test_price_filter_only_touches_units(self):
        self.set("all", "all", "all"); base = read(self.pg)
        self.set("all", "all", "real"); real = read(self.pg)
        self.assertEqual({k: base[k] for k in ("n", "settled", "altW", "altL", "bPostW", "bPostL", "saved")}, {k: real[k] for k in ("n", "settled", "altW", "altL", "bPostW", "bPostL", "saved")})
        self.set("all", "all", "assumed"); self.assertLess(read(self.pg)["uN"], base["uN"])
        self.set()

    def test_show_more(self):
        pg = self.pg
        many = []
        for i in range(95):
            p = pick(100 + i, "NFL", "SPREAD", "HOM -3.5", -10.0, -3.5, 27, 20, "win", label="HOM -10.0"); p["id"] = f"m{i}"
            p["lockedAt"] = NOW - i * 60000; many.append(p)
        pg.evaluate("(p)=>{saveP(p);window._altShown=40}", many); self.set()
        pg.evaluate("renderAnalyticsAdjLines()")
        self.assertEqual(pg.evaluate("document.querySelectorAll('#adjlines-body [style*=\"border-left\"]').length"), 40)
        self.assertIn("SHOW 40 MORE (55 LEFT)", pg.evaluate("document.getElementById('adjlines-body').innerText"))
        pg.evaluate("_altMore()")
        self.assertEqual(pg.evaluate("document.querySelectorAll('#adjlines-body [style*=\"border-left\"]').length"), 80)
        pg.evaluate("_altSetPeriod('all')")                         # changing a filter resets the list to the first page
        self.assertEqual(pg.evaluate("window._altShown"), 40)


if __name__ == "__main__":
    unittest.main(verbosity=2)
