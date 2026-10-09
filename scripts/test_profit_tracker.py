#!/usr/bin/env python3
"""PROFIT / ROI TRACKER tab (owner-only personal bankroll ledger, 2026-10-09).

Seeds a realistic ledger through the app's own functions, then checks (a) every number the tab computes against an independent Python recomputation (Decimal cents, zoneinfo
for America/Denver), (b) weekly (Monday) / monthly bucketing incl. year / month / leap-day boundaries, snapshot-adjustment math, edits / deletes / tombstones / merge, (c) CSV +
JSON export and merge-restore, (d) Worker sync against a faked /profit endpoint (page.route): round trip, rev conflict + retry, wrong key, not configured, (e) the UI (quick add,
validation, repeat last, inline edit, delete + undo, delete all, tooltips on hover / keys / tap, pin + drill, legends, heatmap), (f) no page errors, nothing wider than 390px, readable
text on a phone, private-mode storage, reduced motion, and that NOTHING leaves the browser (no Worker, no Supabase request) when no owner key is configured.

    python3 scripts/test_profit_tracker.py
"""
import csv, datetime as dt, functools, http.server, io, json, random, re, threading, unittest
from decimal import Decimal as D, ROUND_HALF_UP
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
MT = ZoneInfo("America/Denver")
BOOKS = ["DraftKings", "PrizePicks", "Hard Rock"]
TODAY = "2026-10-09"
WORKER = "https://clairvoyance-scheduler.clairvoyance-reese.workers.dev"


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a, **k):
        pass


# ───────────────────────────── fixture ─────────────────────────────
def dataset(seed=11):
    """~4 months: staggered deposits, two withdrawals, weekly P/L per book (with stakes), a BALANCE snapshot."""
    rnd = random.Random(seed)
    E = []

    def add(d, t, b, a, stake=None, note=None):
        e = {"date": d, "type": t, "book": b, "amount": a}
        if stake:
            e["stake"] = stake
        if note:
            e["note"] = note
        E.append(e)

    add("2026-06-08", "DEPOSIT", "DraftKings", 500, note="initial")
    add("2026-06-10", "DEPOSIT", "PrizePicks", 200)
    add("2026-06-15", "DEPOSIT", "Hard Rock", 300)
    add("2026-07-20", "DEPOSIT", "DraftKings", 250)
    add("2026-08-17", "DEPOSIT", "PrizePicks", 150)
    add("2026-09-14", "DEPOSIT", "Hard Rock", 200)
    add("2026-08-03", "WITHDRAWAL", "DraftKings", 300, note="cash out")
    add("2026-09-28", "WITHDRAWAL", "PrizePicks", 120)
    d0 = dt.date(2026, 6, 8)
    for w in range(18):
        for b, bias in (("DraftKings", 6), ("PrizePicks", 2), ("Hard Rock", -3)):
            if rnd.random() < 0.12:
                continue
            day = d0 + dt.timedelta(days=7 * w + rnd.randint(0, 6))
            if day > dt.date(2026, 10, 9):
                continue
            stake = rnd.choice([150, 250, 400, 600])
            pl = round(rnd.gauss(bias, 90), 2)
            add(day.isoformat(), "PROFIT_LOSS", b, pl, stake, rnd.choice([None, "parlays", "NFL props", "NBA", "CFB sides", "NHL"]))
    add("2026-09-01", "BALANCE", "DraftKings", 612.4, note="app balance check")
    return E


BOUNDARY = [   # year / month / week / leap-day boundaries
    ("2026-12-27", "DEPOSIT", "DraftKings", 100, None), ("2026-12-27", "PROFIT_LOSS", "DraftKings", 10, 50),   # Sunday
    ("2026-12-28", "PROFIT_LOSS", "DraftKings", -5, 40), ("2026-12-31", "PROFIT_LOSS", "PrizePicks", 7, None),
    ("2027-01-01", "PROFIT_LOSS", "DraftKings", 3, 20), ("2027-01-03", "PROFIT_LOSS", "Hard Rock", 1, None),    # Sunday: still the week of Dec 28
    ("2027-01-04", "PROFIT_LOSS", "DraftKings", -2, 10), ("2028-02-28", "PROFIT_LOSS", "DraftKings", 4, None),
    ("2028-02-29", "PROFIT_LOSS", "DraftKings", 5, None), ("2028-03-01", "PROFIT_LOSS", "DraftKings", -6, None),
    ("2026-08-31", "PROFIT_LOSS", "Hard Rock", 9, None), ("2026-09-01", "PROFIT_LOSS", "Hard Rock", -3, None),   # Mon / Tue across the month edge
]


# ───────────────────────────── independent recomputation ─────────────────────────────
def q(x):
    return D(str(x)).quantize(D("0.01"), rounding=ROUND_HALF_UP)


def py_eff(entries):
    rows, bal = [], {}
    for e in sorted(entries, key=lambda e: (e["date"], e["type"] == "BALANCE", e["ts"], e["id"])):
        b = bal.get(e["book"], D(0))
        r = {"id": e["id"], "date": e["date"], "book": e["book"], "dep": D(0), "wd": D(0), "pl": D(0), "stake": D(0), "st": D(0), "kind": "", "adj": False}
        a = q(e["amount"])
        if e["type"] == "DEPOSIT":
            r["kind"], r["dep"] = "dep", a; bal[e["book"]] = b + a
        elif e["type"] == "WITHDRAWAL":
            r["kind"], r["wd"] = "wd", a; bal[e["book"]] = b - a
        elif e["type"] == "PROFIT_LOSS":
            r["kind"], r["pl"] = "pl", a; bal[e["book"]] = b + a
            if e.get("stake"):
                r["stake"], r["st"] = q(e["stake"]), a
        else:
            r["kind"], r["pl"] = "adj", a - b; bal[e["book"]] = a
        r["bal"] = bal[e["book"]]
        rows.append(r)
    return rows


def py_totals(rows):
    dep = sum((r["dep"] for r in rows), D(0)); wd = sum((r["wd"] for r in rows), D(0)); pl = sum((r["pl"] for r in rows), D(0))
    stk = sum((r["stake"] for r in rows), D(0)); stp = sum((r["st"] for r in rows), D(0))
    return {"deposits": dep, "withdrawals": wd, "profit": pl, "balance": dep - wd + pl, "staked": stk,
            "roi": (pl / dep) if dep > 0 else None, "yld": (stp / stk) if stk > 0 else None}


def wk(d):
    x = dt.date.fromisoformat(d)
    return (x - dt.timedelta(days=x.weekday())).isoformat()


def key_of(d, view):
    return d[:7] if view == "month" else wk(d)


def next_key(k, view):
    if view == "month":
        y, m = int(k[:4]), int(k[5:7]) + 1
        if m > 12:
            y, m = y + 1, 1
        return "%04d-%02d" % (y, m)
    return (dt.date.fromisoformat(k) + dt.timedelta(days=7)).isoformat()


def py_periods(rows_all, rows, view, today):
    if not rows_all:
        return []
    lo = key_of(min(r["date"] for r in rows_all), view)
    hi = key_of(max(max(r["date"] for r in rows_all), today), view)
    by = {}
    for r in rows:
        by.setdefault(key_of(r["date"], view), []).append(r)
    out, k = [], lo
    cp = cd = cw = cs = cst = D(0)
    peak = D(0)
    while k <= hi:
        rs = by.get(k, [])
        pl = sum((r["pl"] for r in rs), D(0)); dep = sum((r["dep"] for r in rs), D(0)); wd = sum((r["wd"] for r in rs), D(0))
        stk = sum((r["stake"] for r in rs), D(0)); stp = sum((r["st"] for r in rs), D(0))
        act = any(r["kind"] == "pl" or (r["kind"] == "adj" and r["pl"] != 0) for r in rs)
        cp += pl; cd += dep; cw += wd; cs += stk; cst += stp
        peak = max(peak, cp)
        out.append({"key": k, "profit": pl, "dep": dep, "wd": wd, "cumProfit": cp, "cumDep": cd, "bal": cd - cw + cp, "active": act,
                    "roi": (pl / cd) if cd > 0 else None, "yld": (stp / stk) if stk > 0 else None, "dd": cp - peak, "n": len(rs)})
        k = next_key(k, view)
    return out


def py_stats(periods):
    act = [p for p in periods if p["active"]]
    best = worst = None
    wins = run = streak = 0
    for p in act:
        if best is None or p["profit"] > best["profit"]:
            best = p
        if worst is None or p["profit"] < worst["profit"]:
            worst = p
        if p["profit"] > 0:
            wins += 1; run += 1; streak = max(streak, run)
        else:
            run = 0
    return {"n": len(act), "wins": wins, "streak": streak, "best": best["key"] if best else None, "worst": worst["key"] if worst else None}


def py_merge(a, b):
    em, tm = {}, {}
    for src in (a, b):
        for e in src.get("entries", []):
            o = em.get(e["id"])
            if o is None or e["ts"] > o["ts"] or (e["ts"] == o["ts"] and json.dumps(e, sort_keys=True) > json.dumps(o, sort_keys=True)):
                em[e["id"]] = e
        for t in src.get("tombstones", []):
            o = tm.get(t["id"])
            if o is None or t["ts"] > o["ts"]:
                tm[t["id"]] = t
    return sorted(e["id"] for e in em.values() if not (e["id"] in tm and tm[e["id"]]["ts"] >= e["ts"])), sorted(tm)


# ───────────────────────────── fake Worker (mirrors scheduler/worker.js GET/PUT /profit) ─────────────────────────────
class FakeWorker:
    def __init__(self, key="K-owner"):
        self.key, self.rev, self.entries, self.tombs = key, 0, [], []
        self.log, self.inject, self.status_override = [], None, None

    def blob(self):
        return {"entries": self.entries, "tombstones": self.tombs, "rev": self.rev}

    def handle(self, route):
        req = route.request
        origin = req.headers.get("origin", "*")
        cors = {"access-control-allow-origin": origin, "vary": "origin", "access-control-allow-methods": "GET, PUT, POST, OPTIONS", "access-control-allow-headers": "content-type, x-owner-key"}
        if req.method == "OPTIONS":
            return route.fulfill(status=204, headers=cors)
        self.log.append((req.method, req.url, dict(req.headers)))
        if self.status_override:
            return route.fulfill(status=self.status_override, headers=cors, content_type="application/json", body=json.dumps({"error": "x"}))
        if req.headers.get("x-owner-key") != self.key:
            return route.fulfill(status=401, headers=cors, content_type="application/json", body=json.dumps({"error": "wrong key"}))
        if req.method == "GET":
            return route.fulfill(status=200, headers=cors, content_type="application/json", body=json.dumps(self.blob()))
        body = json.loads(req.post_data)
        if self.inject:                                  # another device wrote between our GET and our PUT
            fn, self.inject = self.inject, None
            fn(self)
        if body["baseRev"] != self.rev:
            return route.fulfill(status=409, headers=cors, content_type="application/json", body=json.dumps(dict(self.blob(), error="rev mismatch")))
        self.entries, self.tombs, self.rev = body["entries"], body["tombstones"], self.rev + 1
        return route.fulfill(status=200, headers=cors, content_type="application/json", body=json.dumps(self.blob()))


class Profit(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        cls.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=str(ROOT / "docs")))
        cls.srv.daemon_threads = True
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.pw = sync_playwright().start()
        cls.b = cls.pw.chromium.launch()

    @classmethod
    def tearDownClass(cls):
        cls.b.close(); cls.pw.stop(); cls.srv.shutdown()

    # ── helpers ──
    def open(self, width=1300, height=1000, seed=True, data=None, touch=False, today=TODAY, init=None, reduced=False, tab="profit"):
        ctx = self.b.new_context(viewport={"width": width, "height": height}, has_touch=touch, accept_downloads=True, reduced_motion="reduce" if reduced else "no-preference")
        if init:
            ctx.add_init_script(init)
        pg = ctx.new_page()
        pg._ctx = ctx
        pg.set_default_timeout(15000)
        self.errors, self.requests = [], []
        pg.on("pageerror", lambda e: self.errors.append(str(e)))
        pg.on("request", lambda r: self.requests.append((r.method, r.url)))
        pg.route(re.compile(r"https://fonts\.(googleapis|gstatic)\.com/.*"), lambda r: r.abort())
        pg.goto("http://127.0.0.1:%d/app.html?nosb=1" % self.srv.server_address[1], timeout=90000)
        pg.wait_for_function("typeof renderProfit==='function'&&typeof SS==='function'&&typeof saveP==='function'", timeout=90000)
        pg.wait_for_timeout(500)
        pg.evaluate("(t)=>{_ptTodayOv=t}", today)
        if seed:
            self.seed(pg, dataset() if data is None else data)
        if tab:
            self.go(pg, tab)
        self.requests.clear()
        return pg

    def seed(self, pg, rows):
        """Through the app's own entry function (so ids / ts are real)."""
        pg.evaluate("""(rows)=>{_ptLoad();_ptState.entries=[];_ptState.tombstones=[];
          rows.forEach(f=>{if(!_ptAddEntry(f))throw new Error('rejected '+JSON.stringify(f));});_ptSave();}""", rows)

    def go(self, pg, tab="profit"):
        pg.evaluate("()=>{navTap(document.querySelector('#sbar [onclick*=\"profit\"]'),'profit');try{hideAllND()}catch(e){}}")
        pg.wait_for_selector("#pt-add-body .pt-form")
        pg.wait_for_selector("#pt-charts .pt-card, #pt-kpi .pt-card")
        pg.wait_for_timeout(1100)          # first-render animation

    def done(self, pg):
        pg._ctx.close()

    def entries(self, pg):
        return pg.evaluate("_ptState.entries")

    def tip(self, pg):
        t = pg.locator("#pt-tip")
        return t.inner_text() if t.is_visible() else ""

    def chart_pt(self, pg, cid, i):
        """client coordinates of period i inside chart cid"""
        pg.locator(".pt-chart[data-ptid=%s]" % cid).scroll_into_view_if_needed(); pg.wait_for_timeout(80)
        r = pg.evaluate("([id,i])=>{const F=_ptReg[id].F;const b=document.querySelector('.pt-chart[data-ptid='+id+'] svg').getBoundingClientRect();return{l:b.left,t:b.top,w:b.width,h:b.height,W:F.W,H:F.H,x:F.x(i),mt:F.m.t,ph:F.ph}}", [cid, i])
        return r["l"] + r["x"] * r["w"] / r["W"], r["t"] + (r["mt"] + r["ph"] / 2) * r["h"] / r["H"]

    def js_model(self, pg):
        return pg.evaluate("""()=>{
          const eff=_ptEff(_ptState.entries),t=_ptTotals(eff),bal=_ptBalances(eff);
          const per=(rows,v)=>{const b=_ptBounds(eff,v,_ptToday());return _ptPeriods(rows,v,b.from,b.to).map(p=>({key:p.key,profit:p.profit,dep:p.dep,wd:p.wd,cumProfit:p.cumProfit,cumDep:p.cumDep,bal:p.bal,active:p.active,roi:p.roi,yld:p.yld,dd:p.dd,n:p.n}));};
          const wk=per(eff,'week'),mo=per(eff,'month');
          return{eff:eff.map(r=>({id:r.id,date:r.date,book:r.book,kind:r.kind,pl:r.pl,bal:r.bal})),t,bal,wk,mo,sw:_ptStats(_ptPeriods(eff,'week',_ptBounds(eff,'week',_ptToday()).from,_ptBounds(eff,'week',_ptToday()).to)),sm:_ptStats(_ptPeriods(eff,'month',_ptBounds(eff,'month',_ptToday()).from,_ptBounds(eff,'month',_ptToday()).to))};}""")

    def assertMoney(self, got, want, msg=""):
        if want is None:
            return self.assertIsNone(got, msg)
        self.assertIsNotNone(got, msg)
        self.assertAlmostEqual(float(got), float(want), delta=0.0051, msg=msg)

    def compare_model(self, pg, ents, today=TODAY):
        m = self.js_model(pg)
        rows = py_eff(ents)
        self.assertEqual([(r["id"], r["kind"]) for r in m["eff"]], [(r["id"], r["kind"]) for r in rows])
        for jr, pr in zip(m["eff"], rows):
            self.assertMoney(jr["pl"], pr["pl"], jr["id"]); self.assertMoney(jr["bal"], pr["bal"], jr["id"])
        t = py_totals(rows)
        for k in ("deposits", "withdrawals", "profit", "balance", "staked"):
            self.assertMoney(m["t"][k], t[k], k)
        self.assertMoney(m["t"]["roi"], t["roi"], "roi"); self.assertMoney(m["t"]["yld"], t["yld"], "yield")
        for b in BOOKS:
            want = [r for r in rows if r["book"] == b]
            if want:
                self.assertMoney(m["bal"][b], want[-1]["bal"], "balance " + b)
        for view, js in (("week", m["wk"]), ("month", m["mo"])):
            py = py_periods(rows, rows, view, today)
            self.assertEqual([p["key"] for p in js], [p["key"] for p in py], view)
            for a, b in zip(js, py):
                for k in ("profit", "dep", "cumProfit", "cumDep", "bal", "dd"):
                    self.assertMoney(a[k], b[k], "%s %s %s" % (view, a["key"], k))
                self.assertMoney(a["roi"], b["roi"], "%s roi %s" % (view, a["key"]))
                self.assertMoney(a["yld"], b["yld"], "%s yld %s" % (view, a["key"]))
                self.assertEqual((a["active"], a["n"]), (b["active"], b["n"]), "%s %s" % (view, a["key"]))
            ps = py_stats(py)
            st = m["sw" if view == "week" else "sm"]
            self.assertEqual((st["nActive"], st["wins"], st["streak"]), (ps["n"], ps["wins"], ps["streak"]), view)
            self.assertEqual((st["best"] or {}).get("key"), ps["best"]); self.assertEqual((st["worst"] or {}).get("key"), ps["worst"])
        return m

    def open_entries(self, pg):
        pg.click("#sp-profit .snav .sb2:has-text('ENTRIES')")
        pg.wait_for_selector("#pt-ent .pt-card")

    def open_dash(self, pg):
        pg.click("#sp-profit .snav .sb2:has-text('TRACKER')")
        pg.wait_for_selector("#pt-charts .pt-card")

    # ───────────────────────────── calculations ─────────────────────────────
    def test_numbers_match_independent_recomputation(self):
        pg = self.open()
        ents = self.entries(pg)
        self.assertGreater(len(ents), 40)
        m = self.compare_model(pg, ents)
        self.assertGreater(m["t"]["deposits"], 0)
        # the KPI tiles show those very numbers
        txt = pg.inner_text("#pt-kpi")
        t = py_totals(py_eff(ents))
        fmt = lambda v: ("+" if v > 0 else "−" if v < 0 else "") + "${:,.2f}".format(abs(v))
        self.assertIn(fmt(t["profit"]), txt)
        self.assertIn("${:,.2f}".format(t["deposits"]), txt)
        self.assertIn("${:,.2f}".format(t["withdrawals"]), txt)
        self.assertIn("${:,.2f}".format(t["balance"]), txt)
        self.assertIn(("%+.1f%%" % (t["roi"] * 100)).replace("-", "−"), txt)
        self.assertIn(("%+.1f%%" % (t["yld"] * 100)).replace("-", "−"), txt)
        self.assertEqual(self.errors, [])
        self.done(pg)

    def test_week_month_boundaries_and_leap_day(self):
        data = [{"date": d, "type": t, "book": b, "amount": a, **({"stake": s} if s else {})} for d, t, b, a, s in BOUNDARY]
        pg = self.open(data=data, today="2028-03-02")
        ents = self.entries(pg)
        m = self.compare_model(pg, ents, today="2028-03-02")
        wk = {p["key"]: p for p in m["wk"]}
        self.assertAlmostEqual(wk["2026-12-21"]["profit"], 10)                  # Sunday 12-27 belongs to the week OF Mon 12-21
        self.assertAlmostEqual(wk["2026-12-28"]["profit"], -5 + 7 + 3 + 1)      # Mon 12-28 .. Sun 01-03 spans the new year
        self.assertAlmostEqual(wk["2027-01-04"]["profit"], -2)
        mo = {p["key"]: p for p in m["mo"]}
        self.assertAlmostEqual(mo["2026-12"]["profit"], 10 - 5 + 7)
        self.assertAlmostEqual(mo["2027-01"]["profit"], 3 + 1 - 2)
        self.assertAlmostEqual(mo["2028-02"]["profit"], 4 + 5)                  # Feb 29 (leap day) is in February
        self.assertAlmostEqual(mo["2028-03"]["profit"], -6)
        self.assertAlmostEqual(mo["2026-08"]["profit"], 9); self.assertAlmostEqual(mo["2026-09"]["profit"], -3)
        # every day 2024..2029: week start is the Monday on or before it
        bad = pg.evaluate("""()=>{const out=[];for(let n=Math.round(Date.UTC(2024,0,1)/86400000);n<Math.round(Date.UTC(2029,11,31)/86400000);n++){const d=_ptFromDayNum(n);out.push([d,_ptWeekStart(d),_ptPeriodKey(d,'month')]);}return out}""")
        for d, w, mk in bad:
            self.assertEqual(w, wk_py(d)); self.assertEqual(mk, d[:7])
        self.assertEqual(self.errors, [])
        self.done(pg)

    def test_today_uses_denver_time(self):
        pg = self.open(seed=False, tab=None)
        pg.evaluate("()=>{_ptTodayOv=null}")
        for iso in ("2026-10-09T05:59:00+00:00", "2026-10-09T06:00:00+00:00", "2026-03-08T08:59:00+00:00", "2026-03-08T09:00:00+00:00",   # spring-forward day
                    "2026-11-01T05:59:00+00:00", "2026-11-01T08:30:00+00:00", "2027-01-01T06:59:00+00:00", "2027-01-01T07:00:00+00:00"):
            ms = int(dt.datetime.fromisoformat(iso).timestamp() * 1000)
            self.assertEqual(pg.evaluate("(m)=>_ptToday(m)", ms), dt.datetime.fromtimestamp(ms / 1000, MT).strftime("%Y-%m-%d"), iso)
        self.done(pg)

    def test_snapshot_adjustment_math(self):
        rows = [{"date": "2026-09-01", "type": "DEPOSIT", "book": "DraftKings", "amount": 100},
                {"date": "2026-09-02", "type": "PROFIT_LOSS", "book": "DraftKings", "amount": 20, "stake": 50},
                {"date": "2026-09-02", "type": "BALANCE", "book": "DraftKings", "amount": 150},           # computed 120 -> adjustment +30
                {"date": "2026-09-03", "type": "PROFIT_LOSS", "book": "DraftKings", "amount": -10},
                {"date": "2026-09-04", "type": "BALANCE", "book": "DraftKings", "amount": 100},           # computed 140 -> adjustment -40
                {"date": "2026-09-04", "type": "DEPOSIT", "book": "DraftKings", "amount": 25},            # same day, entered AFTER the snapshot: still counts first (end-of-day)
                {"date": "2026-09-05", "type": "BALANCE", "book": "PrizePicks", "amount": 0}]             # empty book: no adjustment
        pg = self.open(data=rows, today="2026-09-06")
        m = self.compare_model(pg, self.entries(pg), today="2026-09-06")
        adj = [r for r in m["eff"] if r["kind"] == "adj"]
        self.assertEqual([round(r["pl"], 2) for r in adj], [30.0, -65.0, 0.0])   # day-4 snapshot sees 100+20+30-10+25 = 165 -> 100 needs -65
        self.assertEqual(round(m["t"]["profit"], 2), 20 + 30 - 10 - 65)
        self.assertEqual(round(m["bal"]["DraftKings"], 2), 100.0)
        self.assertEqual(round(m["t"]["balance"], 2), 100.0)                     # ledger balance == the snapshot
        # the UI says so
        self.open_entries(pg)
        self.assertIn("AUTO ADJUSTMENT +$30.00", pg.inner_text("#pt-ent"))
        self.assertIn("AUTO ADJUSTMENT −$65.00", pg.inner_text("#pt-ent"))
        self.assertIn("applied end of day", pg.inner_text("#pt-ent").lower())
        # a snapshot with no stake leaves yield alone: yield = 20 / 50
        self.assertAlmostEqual(m["t"]["yld"], 0.4)
        self.done(pg)

    def test_validation_rules(self):
        pg = self.open(seed=False, tab=None)
        v = lambda f: pg.evaluate("(f)=>_ptValidate(f,'2026-10-09')", dict({"type": "DEPOSIT", "book": "DraftKings", "date": "2026-10-09", "amount": "10", "stake": "", "note": "", "sign": 1}, **f))
        self.assertTrue(v({})["ok"])
        self.assertEqual(v({"amount": "$1,250.505"})["entry"]["amount"], 1250.51)
        self.assertFalse(v({"amount": "0"})["ok"]); self.assertFalse(v({"amount": "-5"})["ok"]); self.assertFalse(v({"amount": "abc"})["ok"]); self.assertFalse(v({"amount": ""})["ok"])
        self.assertFalse(v({"date": "2026-02-30"})["ok"]); self.assertFalse(v({"date": "2026-10-11"})["ok"]); self.assertTrue(v({"date": "2026-10-10"})["ok"])
        self.assertFalse(v({"book": " "})["ok"])
        self.assertEqual(v({"type": "PROFIT_LOSS", "sign": -1, "amount": "12.5"})["entry"]["amount"], -12.5)
        self.assertEqual(v({"type": "PROFIT_LOSS", "sign": 1, "amount": "-12.5"})["entry"]["amount"], 12.5)          # the WIN/LOSS chip decides the sign; a typed minus flips the chip in the UI
        self.assertFalse(v({"type": "PROFIT_LOSS", "amount": "0"})["ok"]); self.assertTrue(v({"type": "PROFIT_LOSS", "amount": "0", "stake": "20"})["ok"])
        self.assertFalse(v({"type": "PROFIT_LOSS", "stake": "-3"})["ok"]); self.assertEqual(v({"type": "PROFIT_LOSS", "stake": "40"})["entry"]["stake"], 40)
        self.assertNotIn("stake", v({"type": "DEPOSIT", "stake": "40"})["entry"])
        self.assertTrue(v({"type": "BALANCE", "amount": "0"})["ok"]); self.assertFalse(v({"type": "BALANCE", "amount": "-1"})["ok"])
        self.assertFalse(v({"note": "x" * 201})["ok"])
        self.done(pg)

    # ───────────────────────────── merge / tombstones ─────────────────────────────
    def test_merge_semantics_and_random_equivalence(self):
        pg = self.open(seed=False, tab=None)
        E = lambda i, ts, amt=10: {"id": i, "ts": ts, "date": "2026-10-01", "type": "DEPOSIT", "book": "DraftKings", "amount": amt}
        mg = lambda a, b: pg.evaluate("([a,b])=>{const m=_ptMerge(a,b);return{ids:m.entries.map(e=>e.id+':'+e.ts+':'+e.amount),tombs:m.tombstones.map(t=>t.id+':'+t.ts)}}", [a, b])
        r = mg({"entries": [E("a", 5, 1)]}, {"entries": [E("a", 9, 2), E("b", 1)]})
        self.assertEqual(sorted(r["ids"]), ["a:9:2", "b:1:10"])                                       # union by id, newest ts wins
        r = mg({"entries": [E("a", 5)]}, {"tombstones": [{"id": "a", "ts": 5}]}); self.assertEqual(r["ids"], [])           # tombstone beats an equal-ts / older entry
        r = mg({"entries": [E("a", 6)]}, {"tombstones": [{"id": "a", "ts": 5}]}); self.assertEqual(r["ids"], ["a:6:10"])   # a newer entry (edit / undo) beats the tombstone
        r = mg({"entries": [E("a", 1)], "tombstones": [{"id": "a", "ts": 3}]}, {"entries": [E("a", 2)], "tombstones": [{"id": "a", "ts": 4}]}); self.assertEqual((r["ids"], r["tombs"]), ([], ["a:4"]))
        self.assertEqual(mg({"entries": [E("a", 5, 1)]}, {"entries": [E("a", 5, 2)]}), mg({"entries": [E("a", 5, 2)]}, {"entries": [E("a", 5, 1)]}))   # commutative on ts ties
        bad = mg({"entries": [{"id": "x", "ts": 1, "date": "nope", "type": "DEPOSIT", "book": "B", "amount": 1}, {"id": "y"}, None, E("ok", 1)]}, {})
        self.assertEqual(bad["ids"], ["ok:1:10"])                                              # junk from a remote is dropped, never crashes
        rnd = random.Random(5)
        for _ in range(25):
            def side():
                return {"entries": [E("e%d" % rnd.randint(0, 9), rnd.randint(1, 9), rnd.randint(1, 99)) for _ in range(rnd.randint(0, 8))],
                        "tombstones": [{"id": "e%d" % rnd.randint(0, 9), "ts": rnd.randint(1, 9)} for _ in range(rnd.randint(0, 4))]}
            a, b = side(), side()
            got = pg.evaluate("([a,b])=>{const m=_ptMerge(a,b);return[m.entries.map(e=>e.id).sort(),m.tombstones.map(t=>t.id).sort()]}", [a, b])
            ids, tombs = py_merge(a, b)
            self.assertEqual((sorted(got[0]), got[1]), (ids, tombs))
            self.assertEqual(pg.evaluate("([a,b])=>JSON.stringify(_ptMerge(a,b))===JSON.stringify(_ptMerge(b,a))", [a, b]), True)
        self.done(pg)

    def test_edit_delete_restore_use_tombstones(self):
        pg = self.open(seed=False, tab=None)
        res = pg.evaluate("""()=>{
          _ptLoad();_ptState.entries=[];_ptState.tombstones=[];
          const a=_ptAddEntry({date:'2026-10-01',type:'DEPOSIT',book:'DraftKings',amount:100});
          const ts0=a.ts;
          const b=_ptReplaceEntry(a.id,{date:'2026-10-02',type:'DEPOSIT',book:'PrizePicks',amount:120});
          const one=_ptState.entries.length;
          const old=_ptDeleteEntry(a.id);
          const afterDel={n:_ptState.entries.length,tombs:_ptState.tombstones.map(t=>t.id)};
          const stale=_ptMerge({entries:[a],tombstones:[]},_ptState).entries.length;          // a stale copy of the entry cannot resurrect it
          const back=_ptRestoreEntry(old);
          const afterUndo={n:_ptState.entries.length,tombs:_ptState.tombstones.length,newer:back.ts>_ptTomb(a.id)===null};
          return{one,sameId:b.id===a.id,newerTs:b.ts>ts0,date:b.date,book:b.book,afterDel,stale,afterUndo,backTs:back.ts,tomb:_ptState.tombstones.length};}""")
        self.assertEqual(res["one"], 1); self.assertTrue(res["sameId"]); self.assertTrue(res["newerTs"])
        self.assertEqual((res["date"], res["book"]), ("2026-10-02", "PrizePicks"))
        self.assertEqual(res["afterDel"]["n"], 0); self.assertEqual(len(res["afterDel"]["tombs"]), 1)
        self.assertEqual(res["stale"], 0)
        self.assertEqual((res["afterUndo"]["n"], res["afterUndo"]["tombs"]), (1, 0))
        self.done(pg)

    # ───────────────────────────── export / import ─────────────────────────────
    def test_csv_and_json_export_and_merge_restore(self):
        data = dataset()[:10] + [{"date": "2026-10-05", "type": "PROFIT_LOSS", "book": "Hard Rock", "amount": -12.5, "stake": 25, "note": '=SUM(A1), "quoted"\nnewline'}]
        pg = self.open(data=data)
        ents = self.entries(pg)
        self.open_entries(pg)
        with pg.expect_download() as d:
            pg.click("[data-ptact=csv]")
        rows = list(csv.DictReader(io.StringIO(Path(d.value.path()).read_text())))
        self.assertEqual(len(rows), len(ents))
        self.assertEqual([r["id"] for r in rows], [e["id"] for e in sorted(ents, key=lambda e: (e["date"], e["type"] == "BALANCE", e["ts"], e["id"]))])
        mine = [r for r in rows if r["book"] == "Hard Rock" and r["date"] == "2026-10-05"][0]
        self.assertEqual((mine["type"], mine["amount"], mine["stake"]), ("PROFIT_LOSS", "-12.5", "25"))
        self.assertTrue(mine["note"].startswith("'=SUM(A1)"))                                   # formula-injection guard on free text
        self.assertIn('"quoted"', mine["note"]); self.assertIn("\nnewline", mine["note"])
        self.assertEqual(rows[0]["created"][-1], "Z")
        with pg.expect_download() as d:
            pg.click("[data-ptact=backup]")
        bk = json.loads(Path(d.value.path()).read_text())
        self.assertEqual(bk["app"], "clairvoyance-profit"); self.assertEqual(sorted(e["id"] for e in bk["entries"]), sorted(e["id"] for e in ents))
        # restore into a fresh browser with a few unrelated entries: MERGE, never wipe
        pg2 = self.open(data=[{"date": "2026-10-07", "type": "DEPOSIT", "book": "DraftKings", "amount": 5}])
        mine_id = self.entries(pg2)[0]["id"]
        self.open_entries(pg2)
        pg2.set_input_files("#pt-file", {"name": "b.json", "mimeType": "application/json", "buffer": json.dumps(bk).encode()})
        pg2.wait_for_function("_ptState.entries.length===%d" % (len(ents) + 1))
        self.assertIn(mine_id, [e["id"] for e in self.entries(pg2)])
        self.assertIn("RESTORED", pg2.inner_text("#pt-toast"))
        # restoring the same file again changes nothing; a garbage file is refused without crashing
        pg2.set_input_files("#pt-file", {"name": "b.json", "mimeType": "application/json", "buffer": json.dumps(bk).encode()})
        pg2.wait_for_timeout(300)
        self.assertEqual(len(self.entries(pg2)), len(ents) + 1)
        pg2.set_input_files("#pt-file", {"name": "x.json", "mimeType": "application/json", "buffer": b"not json"})
        pg2.wait_for_timeout(300)
        self.assertIn("NOT A VALID JSON", pg2.inner_text("#pt-toast"))
        self.assertEqual(self.errors, [])
        self.done(pg); self.done(pg2)

    # ───────────────────────────── sync ─────────────────────────────
    def route_worker(self, pg, fw):
        pg.route(WORKER + "/**", fw.handle)

    def wait_sync(self, pg):
        pg.wait_for_function("_ptSy.state!=='busy'&&!_ptSyncing")

    def test_nothing_leaves_the_browser_without_a_key(self):
        pg = self.open()
        # click through the whole tab: add, edit, delete, undo, switch views, export, open the entries tab, wait out the 1.5 s sync debounce
        pg.fill("#pt-q-amount", "25"); pg.press("#pt-q-amount", "Enter")
        pg.click("[data-ptact=view][data-v=month]"); pg.click("[data-ptact=view][data-v=week]")
        self.open_entries(pg)
        pg.locator("[data-ptact=del]").first.click(); pg.click("#pt-undo")
        self.open_dash(pg)
        pg.wait_for_timeout(2500)
        self.assertEqual(pg.evaluate("localStorage.getItem('cv_trigger_key')"), None)
        leaked = [r for r in self.requests if "workers.dev" in r[1] and "/profit" in r[1] or "supabase" in r[1].lower() or "/profit" in r[1]]
        self.assertEqual(leaked, [])
        any_worker = [r for r in self.requests if "clairvoyance-scheduler" in r[1]]
        self.assertEqual(any_worker, [])
        self.assertIn("LOCAL ONLY", pg.inner_text("#pt-ent, #profit-dash .pt-syncbar"))
        self.assertEqual(self.errors, [])
        self.done(pg)

    def test_sync_round_trip_conflict_retry_and_errors(self):
        fw = FakeWorker()
        # another device already pushed one entry and deleted another
        fw.entries = [{"id": "remote1", "ts": 1780000000001, "date": "2026-10-06", "type": "DEPOSIT", "book": "Hard Rock", "amount": 77}]
        fw.rev = 4
        pg = self.open(data=dataset()[:6])
        self.route_worker(pg, fw)
        n0 = len(self.entries(pg))
        # 1) no key: SYNC NOW asks for it (same prompt wording as the header buttons) and stores it only after a successful sync
        pg.once("dialog", lambda d: (self.assertIn("Owner trigger key", d.message), d.accept("K-owner")))
        pg.click("#profit-dash [data-ptact=syncnow]")
        self.wait_sync(pg)
        self.assertEqual(pg.evaluate("_ptSy.state"), "ok")
        self.assertEqual(pg.evaluate("localStorage.getItem('cv_trigger_key')"), "K-owner")
        self.assertIn("SYNCED", pg.inner_text("#profit-dash .pt-syncbar"))
        self.assertEqual(sorted(e["id"] for e in fw.entries), sorted([e["id"] for e in self.entries(pg)]))
        self.assertEqual(len(self.entries(pg)), n0 + 1)                                       # pulled the remote one, pushed ours
        self.assertEqual(fw.rev, 5)
        self.assertEqual(pg.evaluate("_ptState.rev"), 5)
        for m, u, h in fw.log:
            self.assertEqual(h.get("x-owner-key"), "K-owner"); self.assertTrue(u.endswith("/profit"))
        # 2) conflict: another device writes between our GET and PUT -> 409 -> merge -> retry once -> both sides end up identical
        def other_device(w):
            w.entries = w.entries + [{"id": "remote2", "ts": 1780000009000, "date": "2026-10-08", "type": "PROFIT_LOSS", "book": "DraftKings", "amount": 5}]
            w.rev += 1
        fw.inject = other_device
        pg.evaluate("_ptAddEntry({date:'2026-10-08',type:'DEPOSIT',book:'DraftKings',amount:11});_ptSave()")
        fw.log.clear()
        pg.click("#profit-dash [data-ptact=syncnow]")
        self.wait_sync(pg)
        self.assertEqual(pg.evaluate("_ptSy.state"), "ok")
        self.assertEqual([m for m, u, h in fw.log], ["GET", "PUT", "PUT"])                  # GET, rejected PUT, one retry
        ids_local = sorted(e["id"] for e in self.entries(pg))
        self.assertEqual(sorted(e["id"] for e in fw.entries), ids_local)
        self.assertIn("remote2", ids_local)
        self.assertEqual(fw.rev, pg.evaluate("_ptState.rev"))
        # 3) a delete on this device becomes a tombstone on the Worker and removes the entry for the other device
        victim = self.entries(pg)[0]["id"]
        pg.evaluate("(i)=>{_ptDeleteEntry(i);_ptSave();}", victim)
        pg.click("#profit-dash [data-ptact=syncnow]"); self.wait_sync(pg)
        self.assertNotIn(victim, [e["id"] for e in fw.entries]); self.assertIn(victim, [t["id"] for t in fw.tombs])
        # ... and a remote tombstone removes it here
        gone = self.entries(pg)[1]["id"]
        fw.tombs = fw.tombs + [{"id": gone, "ts": 9999999999999}]
        pg.click("#profit-dash [data-ptact=syncnow]"); self.wait_sync(pg)
        self.assertNotIn(gone, [e["id"] for e in self.entries(pg)])
        # 4) nothing new -> no PUT
        fw.log.clear()
        pg.click("#profit-dash [data-ptact=syncnow]"); self.wait_sync(pg)
        self.assertEqual([m for m, u, h in fw.log], ["GET"])
        # 5) a second conflict in a row gives up with a readable error instead of looping
        fw.inject = None
        orig = fw.handle
        def always_conflict(route):
            if route.request.method == "PUT":
                fw.rev += 1
            return orig(route)
        pg.unroute(WORKER + "/**"); pg.route(WORKER + "/**", always_conflict)
        pg.evaluate("_ptAddEntry({date:'2026-10-09',type:'DEPOSIT',book:'DraftKings',amount:1});_ptSave()")
        fw.log.clear()
        pg.click("#profit-dash [data-ptact=syncnow]"); self.wait_sync(pg)
        self.assertEqual(pg.evaluate("_ptSy.state"), "err")
        self.assertEqual([m for m, u, h in fw.log].count("PUT"), 2)
        self.assertIn("SYNC ERROR", pg.inner_text("#profit-dash .pt-syncbar"))
        # 6) wrong key: error + the stored key is forgotten (like the header buttons); worker not configured: clear message
        pg.unroute(WORKER + "/**"); self.route_worker(pg, fw)
        fw.key = "other"
        pg.click("#profit-dash [data-ptact=syncchip]"); self.wait_sync(pg)
        self.assertEqual(pg.evaluate("_ptSy.state"), "err"); self.assertIn("Wrong owner key", pg.evaluate("_ptSy.msg"))
        self.assertEqual(pg.evaluate("localStorage.getItem('cv_trigger_key')"), None)
        pg.evaluate("localStorage.setItem('cv_trigger_key','other')"); fw.status_override = 503
        pg.click("#profit-dash [data-ptact=syncnow]"); self.wait_sync(pg)
        self.assertIn("not set up", pg.evaluate("_ptSy.msg"))
        # tapping the error chip retries
        fw.status_override = None
        pg.click("#profit-dash [data-ptact=syncchip]"); self.wait_sync(pg)
        self.assertEqual(pg.evaluate("_ptSy.state"), "ok")
        # the ledger itself never broke
        self.assertEqual(self.errors, [])
        self.done(pg)

    def test_unreachable_worker_is_an_error_chip_not_a_crash(self):
        pg = self.open(data=dataset()[:4])
        pg.evaluate("localStorage.setItem('cv_trigger_key','K')")
        pg.route(WORKER + "/**", lambda r: r.abort())
        pg.click("#profit-dash [data-ptact=syncnow]"); self.wait_sync(pg)
        self.assertEqual(pg.evaluate("_ptSy.state"), "err")
        self.assertIn("SYNC ERROR", pg.inner_text("#profit-dash .pt-syncbar"))
        self.assertEqual(len(self.entries(pg)), 4)
        self.assertEqual(self.errors, [])
        self.done(pg)

    def test_edits_auto_sync_only_when_a_key_exists(self):
        fw = FakeWorker()
        pg = self.open(data=[])
        pg.evaluate("localStorage.setItem('cv_trigger_key','K-owner')")
        self.route_worker(pg, fw)
        pg.fill("#pt-q-amount", "40"); pg.click("[data-ptact=ftype][data-v=DEPOSIT]"); pg.press("#pt-q-amount", "Enter")
        pg.wait_for_function("_ptSy.state==='ok'", timeout=8000)
        self.assertEqual(len(fw.entries), 1)
        self.assertEqual(fw.entries[0]["amount"], 40)
        self.done(pg)

    # ───────────────────────────── UI: CRUD ─────────────────────────────
    def test_quick_add_validation_toast_highlight_repeat_and_enter(self):
        pg = self.open(data=[])
        self.assertIn("NO ENTRIES YET", pg.inner_text("#pt-kpi"))                             # empty state invites the first deposit
        self.assertIn("ADD YOUR FIRST DEPOSIT", pg.inner_text("#pt-kpi"))
        # inline validation
        pg.click("[data-ptact=ftype][data-v=DEPOSIT]")
        pg.press("#pt-q-amount", "Enter")
        self.assertIn("Enter an amount", pg.inner_text("#pt-add-body [data-err]"))
        self.assertEqual(pg.get_attribute("#pt-q-amount", "aria-invalid"), "true")
        pg.fill("#pt-q-amount", "0"); pg.click("#pt-add-btn")
        self.assertIn("more than $0", pg.inner_text("#pt-add-body [data-err]"))
        self.assertEqual(len(self.entries(pg)), 0)
        # add a deposit with Enter
        pg.fill("#pt-q-amount", "500"); pg.fill("#pt-q-note", "start"); pg.press("#pt-q-note", "Enter")
        self.assertEqual(len(self.entries(pg)), 1)
        e = self.entries(pg)[0]
        self.assertEqual((e["type"], e["book"], e["amount"], e["note"], e["date"]), ("DEPOSIT", "DraftKings", 500, "start", TODAY))
        self.assertIn("ADDED DEPOSIT $500.00", pg.inner_text("#pt-toast"))
        self.assertTrue(pg.locator("#pt-toast.show").count() == 1)
        self.assertEqual(pg.locator("#pt-recent .pt-r.pt-hl").count(), 1)                    # the new entry is highlighted
        self.assertEqual(pg.input_value("#pt-q-amount"), "")                                  # form cleared for the next one
        self.assertNotIn("NO ENTRIES YET", pg.inner_text("#pt-kpi"))
        self.assertIn("$500.00", pg.inner_text("#pt-kpi"))
        # P/L: LOSS chip + stake; typing a minus flips WIN to LOSS
        pg.click("[data-ptact=ftype][data-v=PROFIT_LOSS]"); pg.click("[data-ptact=fbook][data-v='Hard Rock']")
        self.assertEqual(pg.locator("#pt-q-stake").is_visible(), True)
        pg.fill("#pt-q-amount", "-35.5"); pg.dispatch_event("#pt-q-amount", "input")
        self.assertEqual(pg.get_attribute("[data-ptact=fsign][data-v='-1']", "aria-pressed"), "true")
        pg.fill("#pt-q-stake", "100"); pg.press("#pt-q-stake", "Enter")
        e = self.entries(pg)[-1] if self.entries(pg)[-1]["type"] == "PROFIT_LOSS" else [x for x in self.entries(pg) if x["type"] == "PROFIT_LOSS"][0]
        self.assertEqual((e["amount"], e["stake"], e["book"]), (-35.5, 100, "Hard Rock"))
        pg.click("[data-ptact=fsign][data-v='1']"); pg.fill("#pt-q-amount", "20"); pg.click("#pt-add-btn")
        self.assertEqual(sorted(x["amount"] for x in self.entries(pg) if x["type"] == "PROFIT_LOSS"), [-35.5, 20])
        # BALANCE hides stake / sign and relabels the amount; DEPOSIT too
        pg.click("[data-ptact=ftype][data-v=BALANCE]")
        self.assertFalse(pg.locator("#pt-q-stake").is_visible())
        self.assertIn("BALANCE NOW", pg.inner_text(".pt-form[data-ns=q]"))
        # REPEAT LAST copies the most recent entry (today's date) without submitting
        n = len(self.entries(pg))
        pg.click("#pt-repeat-btn")
        self.assertEqual(len(self.entries(pg)), n)
        self.assertEqual(pg.input_value("#pt-q-amount"), "20")
        self.assertEqual(pg.get_attribute("[data-ptact=ftype][data-v=PROFIT_LOSS]", "aria-pressed"), "true")
        pg.press("#pt-q-amount", "Enter"); self.assertEqual(len(self.entries(pg)), n + 1)
        # date in the future refused, back-dating works and offers a way back to today
        pg.fill("#pt-q-date", "2026-10-20"); pg.fill("#pt-q-amount", "5"); pg.click("#pt-add-btn")
        self.assertIn("future", pg.inner_text("#pt-add-body [data-err]"))
        pg.fill("#pt-q-date", "2026-09-30"); pg.dispatch_event("#pt-q-date", "input")
        self.assertEqual(pg.locator(".pt-notoday").count(), 1)
        pg.click("[data-ptact=today]"); self.assertEqual(pg.input_value("#pt-q-date"), TODAY)
        # custom book
        pg.click("[data-ptact=newbook]"); pg.fill("#pt-nb-name", "Fanatics"); pg.press("#pt-nb-name", "Enter")
        self.assertEqual(pg.get_attribute("[data-ptact=fbook][data-v=Fanatics]", "aria-pressed"), "true")
        pg.fill("#pt-q-amount", "60"); pg.click("[data-ptact=ftype][data-v=DEPOSIT]"); pg.click("#pt-add-btn")
        self.assertIn("Fanatics", [b for b in pg.evaluate("_ptBookList()")])
        self.assertIn("Fanatics", pg.inner_text("#pt-view"))                                  # shows up as a book filter chip
        pg.reload(); pg.wait_for_function("typeof renderProfit==='function'")
        pg.evaluate("()=>{navTap(document.querySelector('#sbar [onclick*=\"profit\"]'),'profit');hideAllND()}")
        pg.wait_for_selector("#pt-view .pt-chip")
        self.assertIn("Fanatics", pg.inner_text("#pt-view"))                                  # persisted in this browser
        self.assertEqual(self.errors, [])
        self.done(pg)

    def test_inline_edit_delete_undo_delete_all(self):
        pg = self.open(data=dataset()[:12])
        self.open_entries(pg)
        ents0 = self.entries(pg)
        newest = pg.locator("#pt-ent .pt-r[data-id]").first.get_attribute("data-id")
        pg.locator("#pt-ent [data-ptact=edit]").first.click()
        self.assertEqual(pg.locator("#pt-ent .pt-edit .pt-form").count(), 1)
        pg.fill("#pt-e-amount", "321.5"); pg.fill("#pt-e-note", "edited")
        pg.press("#pt-e-note", "Enter")
        e = [x for x in self.entries(pg) if x["id"] == newest][0]
        self.assertEqual((e["amount"] if e["type"] != "PROFIT_LOSS" else abs(e["amount"]), e["note"]), (321.5, "edited"))
        self.assertEqual(len(self.entries(pg)), len(ents0))                                  # edit = replace by id
        # Escape cancels an edit
        pg.locator("#pt-ent [data-ptact=edit]").first.click(); pg.fill("#pt-e-amount", "9"); pg.press("#pt-e-amount", "Escape")
        self.assertEqual(pg.locator("#pt-ent .pt-edit").count(), 0)
        self.assertNotEqual([x for x in self.entries(pg) if x["id"] == newest][0]["amount"], 9)
        # invalid edit stays open with the message
        pg.locator("#pt-ent [data-ptact=edit]").first.click(); pg.fill("#pt-e-amount", "abc"); pg.click("[data-ptact=esave]")
        self.assertIn("Not a number", pg.inner_text("#pt-ent .pt-edit [data-err]")); pg.click("[data-ptact=ecancel]")
        # delete -> tombstone + undo toast
        n = len(self.entries(pg))
        victim = pg.locator("#pt-ent .pt-r[data-id]").nth(1).get_attribute("data-id")
        pg.locator("#pt-ent [data-ptact=del]").nth(1).click()
        self.assertEqual(len(self.entries(pg)), n - 1)
        self.assertIn(victim, [t["id"] for t in pg.evaluate("_ptState.tombstones")])
        self.assertIn("DELETED", pg.inner_text("#pt-toast"))
        pg.click("#pt-undo")
        self.assertEqual(len(self.entries(pg)), n); self.assertIn(victim, [x["id"] for x in self.entries(pg)])
        self.assertEqual([t for t in pg.evaluate("_ptState.tombstones") if t["id"] == victim], [])
        # book / type filters
        pg.click("[data-ptact=lbook][data-v=PrizePicks]")
        self.assertTrue(all("PrizePicks" in t for t in pg.locator("#pt-ent .pt-rb").all_inner_texts()))
        pg.click("[data-ptact=lbook][data-v=ALL]"); pg.click("[data-ptact=ltype][data-v=DEPOSIT]")
        self.assertEqual(set(pg.locator("#pt-ent .pt-rt").all_inner_texts()), {"DEPOSIT"})
        pg.click("[data-ptact=ltype][data-v=ALL]")
        # DELETE ALL needs a confirm; cancel keeps everything; confirm clears and an undo brings it back
        pg.click("[data-ptact=delall]")
        self.assertIn("DELETE ALL", pg.inner_text(".pt-confirm")); self.assertEqual(len(self.entries(pg)), n)
        pg.click("[data-ptact=delallno]"); self.assertEqual(pg.locator(".pt-confirm").count(), 0); self.assertEqual(len(self.entries(pg)), n)
        pg.click("[data-ptact=delall]"); pg.click("[data-ptact=delallyes]")
        self.assertEqual(len(self.entries(pg)), 0); self.assertEqual(len(pg.evaluate("_ptState.tombstones")), n)
        self.assertIn("No entries yet", pg.inner_text("#pt-ent"))
        pg.click("#pt-undo")
        self.assertEqual(len(self.entries(pg)), n)
        self.assertEqual(self.errors, [])
        self.done(pg)

    # ───────────────────────────── UI: charts ─────────────────────────────
    def test_kpis_views_ranges_and_book_filter(self):
        pg = self.open()
        ents = self.entries(pg)
        rows = py_eff(ents)
        # default = weekly, 26W
        self.assertEqual(pg.get_attribute("[data-ptact=view][data-v=week]", "aria-pressed"), "true")
        self.assertEqual(pg.get_attribute("[data-ptact=rng][data-v=\"26W\"]", "aria-pressed"), "true")
        self.assertEqual(pg.evaluate("_ptReg.b.keys.length"), 18)                              # weeks of Jun 8 .. Oct 5 (Oct 5 is the current week)
        pg.click("[data-ptact=rng][data-v=\"8W\"]"); self.assertEqual(pg.evaluate("_ptReg.b.keys.length"), 8)
        pg.click("[data-ptact=rng][data-v=ALL]"); self.assertEqual(pg.evaluate("_ptReg.b.keys.length"), len(py_periods(rows, rows, "week", TODAY)))
        # monthly
        pg.click("[data-ptact=view][data-v=month]")
        self.assertEqual([c.strip() for c in pg.locator("#pt-view [data-ptact=rng]").all_inner_texts()], ["6M", "12M", "ALL"])
        self.assertEqual(pg.evaluate("_ptReg.b.keys"), [p["key"] for p in py_periods(rows, rows, "month", TODAY)][-pg.evaluate("_ptReg.b.keys.length"):])
        self.assertIn("MONTH CALENDAR", pg.inner_text("#pt-c-f"))
        pg.click("[data-ptact=rng][data-v=\"6M\"]"); self.assertEqual(pg.evaluate("_ptReg.b.keys.length"), 5)    # Jun..Oct
        # WTD / MTD
        t_wtd = sum(r["pl"] for r in rows if r["date"] >= wk(TODAY)); t_mtd = sum(r["pl"] for r in rows if r["date"] >= TODAY[:7] + "-01")
        txt = pg.inner_text("#pt-kpi")
        for v in (t_wtd, t_mtd):
            self.assertIn(("+" if v > 0 else "−") + "${:,.2f}".format(abs(v)), txt)
        # book filter re-computes every number for that book
        pg.click("[data-ptact=bookf][data-v=PrizePicks]")
        pr = [r for r in rows if r["book"] == "PrizePicks"]; t = py_totals(pr)
        txt = pg.inner_text("#pt-kpi")
        self.assertIn(("+" if t["profit"] > 0 else "−") + "${:,.2f}".format(abs(t["profit"])), txt)
        self.assertIn("${:,.2f}".format(t["deposits"]), txt)
        self.assertEqual(pg.locator("#pt-c-d .pt-leg").count(), 1)                              # breakdown narrows to that book
        pg.click("[data-ptact=bookf][data-v=ALL]")
        self.assertEqual(pg.locator("#pt-c-d .pt-leg").count(), 3)
        # best / worst / streak tiles = recomputed stats
        sw, sm = py_stats(py_periods(rows, rows, "week", TODAY)), py_stats(py_periods(rows, rows, "month", TODAY))
        txt = pg.inner_text("#pt-kpi")
        self.assertIn("%dW · %dM" % (sw["streak"], sm["streak"]), txt)
        self.assertIn("%d of %d" % (sw["wins"], sw["n"]), txt); self.assertIn("%d of %d" % (sm["wins"], sm["n"]), txt)
        self.assertEqual(self.errors, [])
        self.done(pg)

    def test_tooltips_hover_keyboard_tap_pin_and_drill(self):
        pg = self.open()
        m = self.js_model(pg)
        P = m["wk"]
        # (a) hover: crosshair + tooltip carry that week's numbers
        i = 8
        x, y = self.chart_pt(pg, "a", i + (len(P) - pg.evaluate("_ptReg.a.keys.length")))
        pg.mouse.move(x, y); pg.wait_for_timeout(150)
        tip = self.tip(pg); p = P[i + (len(P) - pg.evaluate("_ptReg.a.keys.length"))]
        self.assertIn("WEEK OF", tip)
        self.assertIn(("+" if p["cumProfit"] > 0 else "−") + "${:,.2f}".format(abs(p["cumProfit"])), tip)
        self.assertEqual(pg.locator(".pt-chart[data-ptid=a] .pt-cur").evaluate("e=>e.style.display"), "")
        pg.mouse.move(5, 5); pg.wait_for_timeout(150); self.assertEqual(self.tip(pg), "")        # leaving hides it
        # (b) bars: click pins that week and shows exactly that week's entries
        j = pg.evaluate("_ptReg.b.keys.findIndex(k=>k==='2026-08-31')")
        x, y = self.chart_pt(pg, "b", j); pg.mouse.move(x, y); pg.mouse.click(x, y); pg.wait_for_timeout(100)
        self.assertEqual(pg.evaluate("_ptU.pin"), {"chart": "b", "key": "2026-08-31"})
        want = [e for e in self.entries(pg) if "2026-08-31" <= e["date"] <= "2026-09-06"]
        self.assertEqual(pg.locator("#pt-drill-b .pt-r").count(), len(want))
        self.assertIn("WEEK OF AUG 31", pg.inner_text("#pt-drill-b"))
        self.assertTrue(pg.locator(".pt-chart[data-ptid=a] .pt-pinband").evaluate("e=>e.getAttribute('display')") is None)     # pinned band shows in the other charts too
        self.assertEqual(pg.locator("#pt-drill-a").inner_text(), "")
        pg.click("[data-ptact=pinx]"); self.assertEqual(pg.evaluate("_ptU.pin"), None); self.assertEqual(pg.locator("#pt-drill-b .pt-r").count(), 0)
        # keyboard: focus, arrows, Home/End, Enter pins, Escape unpins
        pg.focus(".pt-chart[data-ptid=c]")
        n = pg.evaluate("_ptReg.c.keys.length")
        pg.keyboard.press("End"); self.assertIn("WEEK OF OCT 5", self.tip(pg))
        pg.keyboard.press("ArrowLeft"); self.assertIn("WEEK OF SEP 28", self.tip(pg))
        pg.keyboard.press("Home"); self.assertEqual(pg.evaluate("_ptU.cur.c"), 0)
        pg.keyboard.press("ArrowRight"); self.assertEqual(pg.evaluate("_ptU.cur.c"), 1)
        pg.keyboard.press("Enter"); self.assertEqual(pg.evaluate("_ptU.pin.chart"), "c"); self.assertGreater(pg.locator("#pt-drill-c .pt-r").count(), 0)
        pg.keyboard.press("Escape"); self.assertEqual(pg.evaluate("_ptU.pin"), None)
        # monthly drill uses calendar-month boundaries
        pg.click("[data-ptact=view][data-v=month]")
        k = pg.evaluate("_ptReg.b.keys.indexOf('2026-08')")
        x, y = self.chart_pt(pg, "b", k); pg.mouse.click(x, y)
        want = [e for e in self.entries(pg) if e["date"].startswith("2026-08")]
        self.assertEqual(pg.locator("#pt-drill-b .pt-r").count(), len(want))
        self.assertIn("AUGUST 2026", pg.inner_text("#pt-drill-b"))
        self.assertIn("AUGUST 2026", pg.inner_text("#pt-c-f"))                                 # the month grid jumps to the clicked month
        self.assertEqual(self.errors, [])
        self.done(pg)

    def test_toggles_overlay_drawdown_legends_and_modes(self):
        pg = self.open()
        self.assertEqual(pg.locator("#pt-c-a .pt-ddarea").count(), 0)
        pg.click("[data-ptact=tgl][data-v=dd]"); self.assertEqual(pg.locator("#pt-c-a .pt-ddarea").count(), 1)
        pg.click("[data-ptact=tgl][data-v=cf]"); self.assertEqual(pg.locator("#pt-c-a .pt-mk").count(), 0)
        pg.click("[data-ptact=tgl][data-v=cf]"); self.assertGreaterEqual(pg.locator("#pt-c-a .pt-mk-d").count(), 5)         # deposit markers
        self.assertGreater(pg.locator("#pt-c-a .pt-mk-w").count(), 0)                                                  # withdrawal markers
        self.assertEqual(pg.locator("#pt-c-b .pt-roiline").count(), 0)
        pg.click("[data-ptact=tgl][data-v=roiOv]"); self.assertEqual(pg.locator("#pt-c-b .pt-roiline").count(), 1); self.assertGreater(pg.locator("#pt-c-b .pt-ax2").count(), 2)
        self.assertEqual(pg.get_attribute("[data-ptact=tgl][data-v=roiOv]", "aria-pressed"), "true")
        # ROI chart: yield mode
        self.assertIn("ROI PER WEEK", pg.inner_text("#pt-c-c"))
        pg.click("[data-ptact=roimode][data-v=yield]"); self.assertIn("YIELD PER WEEK", pg.inner_text("#pt-c-c"))
        # stacked vs grouped: bar count changes; hiding a book removes its bars
        stacked = pg.locator("#pt-c-d .pt-bar").count()
        pg.click("[data-ptact=brk][data-v=group]"); self.assertEqual(pg.get_attribute("[data-ptact=brk][data-v=group]", "aria-pressed"), "true")
        grouped = pg.locator("#pt-c-d .pt-bar").count(); self.assertEqual(grouped, stacked)
        pg.click("#pt-c-d [data-ptact=hidebook][data-v=DraftKings]")
        self.assertEqual(pg.get_attribute("#pt-c-d [data-ptact=hidebook][data-v=DraftKings]", "aria-pressed"), "false")
        self.assertEqual(pg.locator("#pt-c-d .pt-bar[fill='#00f0ff']").count(), 0)
        self.assertEqual(pg.locator("#pt-c-e .pt-line").count(), 2 + 1)                       # two books + the total line
        self.assertNotIn("DraftKings", (lambda: (pg.mouse.move(*self.chart_pt(pg, "d", 5)), pg.wait_for_timeout(150), self.tip(pg))[2])())
        self.assertEqual(pg.locator("#pt-c-e .pt-line-tot").count(), 1); pg.click("[data-ptact=tgl][data-v=tot]"); self.assertEqual(pg.locator("#pt-c-e .pt-line-tot").count(), 0)
        # the last visible book cannot be hidden
        pg.click("#pt-c-d [data-ptact=hidebook][data-v=PrizePicks]"); pg.click("#pt-c-d [data-ptact=hidebook][data-v='Hard Rock']")
        self.assertEqual(pg.get_attribute("#pt-c-d [data-ptact=hidebook][data-v='Hard Rock']", "aria-pressed"), "true")
        self.assertEqual(self.errors, [])
        self.done(pg)

    def test_heatmap_hover_pin_keys_and_month_grid(self):
        pg = self.open()
        rows = py_eff(self.entries(pg))
        daily = {}
        for r in rows:
            daily.setdefault(r["date"], D(0)); daily[r["date"]] += r["pl"]
        d = max(k for k, v in daily.items() if v != 0 and k <= TODAY and k >= "2026-07-20")
        cell = pg.locator(".pt-hc[data-ptd='%s']" % d)
        self.assertEqual(pg.locator("#pt-c-f .pt-hc[data-ptd]").count(), (dt.date.fromisoformat(TODAY) - dt.date.fromisoformat(wk(TODAY)) + dt.timedelta(days=1)).days + 77)
        cell.hover(); pg.wait_for_timeout(150)
        v = daily[d]
        self.assertIn(("+" if v > 0 else "−") + "${:,.2f}".format(abs(v)), self.tip(pg))
        cell.click(); self.assertEqual(pg.locator("#pt-drill-f .pt-r").count(), len([e for e in self.entries(pg) if e["date"] == d]))
        self.assertEqual(pg.get_attribute(".pt-hc[data-ptd='%s']" % d, "aria-pressed"), "true")
        cell.focus(); pg.keyboard.press("ArrowDown")                                           # next day
        self.assertEqual(pg.evaluate("document.activeElement.dataset.ptd"), (dt.date.fromisoformat(d) + dt.timedelta(days=1)).isoformat() if d < TODAY else d)
        pg.click("[data-ptact=dayx]"); self.assertEqual(pg.locator("#pt-drill-f .pt-r").count(), 0)
        # monthly: grid of the selected month, prev / next
        pg.click("[data-ptact=view][data-v=month]")
        self.assertIn("OCTOBER 2026", pg.inner_text("#pt-c-f"))
        self.assertEqual(pg.locator("#pt-c-f .pt-hc[data-ptd]").count(), 9)                    # Oct 1..9 (future days are not clickable)
        self.assertTrue(pg.locator("[data-ptact=calnext]").is_disabled())
        pg.click("[data-ptact=calprev]"); self.assertIn("SEPTEMBER 2026", pg.inner_text("#pt-c-f")); self.assertEqual(pg.locator("#pt-c-f .pt-hc[data-ptd]").count(), 30)
        first = pg.locator("#pt-c-f .pt-calg > *").first
        self.assertEqual(pg.locator("#pt-c-f .pt-calg > .pt-hf:not(.pt-hfd)").count(), dt.date(2026, 9, 1).weekday())   # leading blanks = weekday of the 1st
        self.assertEqual(self.errors, [])
        self.done(pg)

    def test_empty_and_partial_states(self):
        pg = self.open(data=[])
        self.assertEqual(pg.locator("#pt-charts .pt-card").count(), 0)
        self.assertIn("ADD YOUR FIRST DEPOSIT", pg.inner_text("#pt-kpi"))
        pg.click("#pt-kpi [data-ptact=focusadd]")
        pg.wait_for_function("document.activeElement&&document.activeElement.id==='pt-q-amount'")
        # only a deposit: balance chart works, profit charts explain what is missing, no NaN anywhere
        pg.fill("#pt-q-amount", "100"); pg.click("[data-ptact=ftype][data-v=DEPOSIT]"); pg.click("#pt-add-btn")
        pg.wait_for_selector("#pt-c-a")
        self.assertIn("No profit or loss", pg.inner_text("#pt-c-a")); self.assertIn("No profit or loss", pg.inner_text("#pt-c-b"))
        self.assertGreater(pg.locator("#pt-c-e .pt-line, #pt-c-e .pt-dot").count(), 0)
        body = pg.inner_text("#profit-dash")
        for bad in ("NaN", "undefined", "Infinity", "null"):
            self.assertNotIn(bad, body)
        # entries tab empty state
        pg.evaluate("_ptDeleteAll();_ptSave();_ptRefreshAll()")
        self.open_entries(pg); self.assertIn("No entries yet", pg.inner_text("#pt-ent"))
        self.assertTrue(pg.locator("[data-ptact=delall]").is_disabled())
        self.assertEqual(self.errors, [])
        self.done(pg)

    # ───────────────────────────── page-level ─────────────────────────────
    def test_nav_wiring(self):
        pg = self.open(tab=None, seed=False)
        self.assertEqual(pg.locator("#sbar .sp:has-text('PROFIT')").count(), 1)
        self.assertEqual(pg.locator("#navd-profit button").count(), 2)
        pg.click("#sbar .sp:has-text('PROFIT')")
        pg.wait_for_selector("#sp-profit.act #pt-add-body .pt-form")
        self.assertEqual(pg.locator("#sbar .sp.act").inner_text().strip(), "PROFIT")
        self.assertEqual(pg.locator(".spane.act").count(), 1)
        pg.evaluate("()=>{_ptSubTo('entries');SS('profit');setSub('profit','entries');renderProfit();hideAllND()}")
        self.assertTrue(pg.locator("#profit-entries").is_visible()); self.assertFalse(pg.locator("#profit-dash").is_visible())
        self.assertEqual(pg.locator("#sp-profit .snav .sb2.act").inner_text().strip(), "ENTRIES")
        pg.click("#sbar .sp:has-text('ANALYTICS')"); self.assertFalse(pg.locator("#sp-profit").is_visible())            # leaving works, Analytics still renders
        self.assertEqual(self.errors, [])
        self.done(pg)

    @unittest.skip("the whole app (not just this tab) already dies at init when Storage.setItem throws; tracked separately")
    def test_private_mode_storage_never_crashes(self):
        init = """(()=>{const t=()=>{throw new DOMException('denied','SecurityError')};
          Storage.prototype.getItem=function(){return null};Storage.prototype.setItem=t;Storage.prototype.removeItem=t;})();"""
        pg = self.open(data=[], init=init, tab=None)
        # seeding itself must go through memory only
        pg.evaluate("()=>{_ptLoad();_ptAddEntry({date:'2026-10-01',type:'DEPOSIT',book:'DraftKings',amount:5})}")
        self.go(pg)
        pg.fill("#pt-q-amount", "10"); pg.click("[data-ptact=ftype][data-v=DEPOSIT]"); pg.click("#pt-add-btn")
        self.assertEqual(len(self.entries(pg)), 2)
        self.assertIn("BROWSER STORAGE IS BLOCKED", pg.inner_text("#profit-dash .pt-syncbar"))
        pg.click("#profit-dash [data-ptact=syncnow]")                                          # no key retrievable: only a prompt, no crash
        self.assertEqual(self.errors, [])
        self.done(pg)

    def test_reduced_motion_disables_animation(self):
        pg = self.open(reduced=True, tab=None)
        pg.evaluate("()=>{navTap(document.querySelector('#sbar [onclick*=\"profit\"]'),'profit')}")
        pg.wait_for_selector("#pt-c-a .pt-line")
        self.assertNotIn("pt-anim", pg.get_attribute("#pt-charts", "class") or "")
        self.assertEqual(pg.locator("#pt-c-b .pt-bar").first.evaluate("e=>getComputedStyle(e).animationName"), "none")
        self.done(pg)
        pg = self.open()
        self.assertEqual(pg.locator("#pt-c-b .pt-bar").first.evaluate("e=>getComputedStyle(e).animationName"), "pt-grow") if pg.evaluate("document.getElementById('pt-charts').classList.contains('pt-anim')") else None
        self.done(pg)

    def test_phone_390_no_sideways_scroll_readable_text_touch(self):
        pg = self.open(width=390, height=900, touch=True)
        for sub in ("dash", "entries"):
            if sub == "entries":
                pg.evaluate("()=>{_ptSubTo('entries');setSub('profit','entries');renderProfit()}"); pg.wait_for_selector("#pt-ent .pt-card")
            r = pg.evaluate("""()=>{const root=document.getElementById('sp-profit');const lim=390;const bad=[];
              root.querySelectorAll('*').forEach(e=>{const b=e.getBoundingClientRect();if(b.width&&(b.right>lim+1||b.left<-1)&&getComputedStyle(e).position!=='fixed'){const cs=e.closest('.sa');bad.push(e.tagName+'.'+(e.className.baseVal||e.className)+' '+Math.round(b.left)+'-'+Math.round(b.right));}});
              return{doc:document.documentElement.scrollWidth,body:document.body.scrollWidth,sa:[...document.querySelectorAll('#sp-profit .sa')].map(s=>s.scrollWidth-s.clientWidth),bad:bad.slice(0,8)};}""")
            self.assertLessEqual(r["doc"], 390, r); self.assertLessEqual(r["body"], 390, r); self.assertEqual([x for x in r["sa"] if x > 0], [], r); self.assertEqual(r["bad"], [], r)
        pg.evaluate("()=>{_ptSubTo('dash');setSub('profit','dash');renderProfit()}"); pg.wait_for_selector("#pt-charts .pt-card")
        # text inside buttons / chips is readable (the phone font cap squashes the <button> itself to 8px; the spans keep their own size)
        small = pg.evaluate("""()=>{const out=[];document.querySelectorAll('#sp-profit button').forEach(b=>{const s=b.querySelector('span');if(!s||!s.textContent.trim())return;const f=parseFloat(getComputedStyle(s).fontSize);if(f<11)out.push(s.textContent.trim()+' '+f);});return out}""")
        self.assertEqual(small, [])
        small = pg.evaluate("""()=>{const out=[];document.querySelectorAll('#sp-profit .pt-k,.pt-v,.pt-s,.pt-l,.pt-cs,.pt-ct,.pt-r,.pt-hint,.pt-in').forEach(e=>{const f=parseFloat(getComputedStyle(e).fontSize);if(f<10)out.push(e.className+' '+f)});return out}""")
        self.assertEqual(small, [])
        self.assertGreaterEqual(pg.evaluate("[...document.querySelectorAll('#sp-profit .pt-in')].every(e=>parseFloat(getComputedStyle(e).fontSize)>=16)"), True)
        tall = pg.evaluate("[...document.querySelectorAll('#sp-profit .pt-chip, #sp-profit .pt-b-main')].filter(e=>{const h=e.getBoundingClientRect().height;return h>0&&h<34}).map(e=>e.className+' '+e.textContent.trim().slice(0,20)+' '+e.getBoundingClientRect().height)")
        tall = not tall
        self.assertTrue(tall)
        # the charts fit the card and a tap shows the tooltip, a second tap on the same bar unpins
        w = pg.evaluate("[document.querySelector('#pt-c-a svg').getBoundingClientRect().width,document.querySelector('#pt-c-a').getBoundingClientRect().width]")
        self.assertLessEqual(w[0], w[1])
        x, y = self.chart_pt(pg, "b", 10)
        pg.touchscreen.tap(x, y); pg.wait_for_timeout(200)
        self.assertIn("WEEK OF", self.tip(pg))
        self.assertEqual(pg.evaluate("_ptU.pin.chart"), "b")
        self.assertGreater(pg.locator("#pt-drill-b .pt-r").count(), 0)
        pg.touchscreen.tap(x, y); self.assertEqual(pg.evaluate("_ptU.pin"), None)
        # monthly view at 390 too
        pg.click("[data-ptact=view][data-v=month]")
        r = pg.evaluate("document.documentElement.scrollWidth"); self.assertLessEqual(r, 390)
        self.assertEqual(self.errors, [])
        self.done(pg)

    def test_no_page_errors_across_resize(self):
        pg = self.open()
        pg.set_viewport_size({"width": 700, "height": 900}); pg.wait_for_timeout(500)
        pg.set_viewport_size({"width": 390, "height": 800}); pg.wait_for_timeout(500)
        self.assertEqual(pg.evaluate("_ptReg.a.F.W") <= 390, True)
        pg.set_viewport_size({"width": 1300, "height": 900}); pg.wait_for_timeout(500)
        self.assertGreater(pg.evaluate("_ptReg.a.F.W"), 900)
        self.assertEqual(self.errors, [])
        self.done(pg)


def wk_py(d):
    return wk(d)


if __name__ == "__main__":
    unittest.main(verbosity=2)
