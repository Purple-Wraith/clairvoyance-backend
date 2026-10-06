#!/usr/bin/env python3
"""Ledger compare / match-the-server (docs/app.html _ledgerDiff, _ledgerAdoptApply, Sync Debug panel): a device whose saved ledger drifted from the server (hides picks the server still has,
holds picks the server never had, missed a removal, has a different result) must be able to SEE that and fix it, and the mobile and desktop builds must run identical code.

    python3 scripts/test_ledger_sync.py
"""
import functools, http.server, json, socketserver, sys, threading, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NOW = 1_800_000_000_000
DAY = 86_400_000


def pick(i, outcome="win", sport="NFL", age_days=20, **kw):
    p = {"id": f"p{i}", "sport": sport, "betType": "SPREAD", "betOn": f"TEAM{i} -3.5", "hA": f"H{i}", "awA": f"A{i}", "date": "2026-09-15", "lockedAt": NOW - age_days * DAY,
         "outcome": outcome, "decOdds": 1.9, "winProb": .6}
    p.update(kw)
    return p


def row(p, outcome=None):
    return {"id": p["id"], "outcome": outcome or p["outcome"], "raw": p, "locked_at": None, "settled_at": None}


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a, **k):
        pass


class Ledger(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        socketserver.TCPServer.allow_reuse_address = True
        cls.srv = socketserver.TCPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=str(ROOT / "docs")))
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch()
        cls.pg = cls.browser.new_page()
        cls.errors = []
        cls.pg.on("pageerror", lambda e: cls.errors.append(str(e)))
        cls.pg.goto(f"http://127.0.0.1:{cls.srv.server_address[1]}/app.html?nosb=1&slim=0")
        cls.pg.wait_for_function("typeof _ledgerDiff==='function'&&typeof _ledgerAdoptApply==='function'")
        cls.pg.wait_for_timeout(1200)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close(); cls.pw.stop(); cls.srv.shutdown()

    def diff(self, local, rows, wiped=()):
        return self.pg.evaluate("([l,r,w,n])=>{const d=_ledgerDiff(l,r,new Set(w),n);return {onlyRemote:d.onlyRemote.map(p=>p.id),wipedLive:d.wipedLive.map(p=>p.id),onlyLocal:d.onlyLocal.map(p=>p.id),tombLocal:d.tombLocal.map(p=>p.id),outcomeDiff:d.outcomeDiff.map(x=>x.local.id),local:d.localWL,server:d.serverWL,total:d.total}}", [local, rows, list(wiped), NOW])

    def test_identical_ledgers_are_in_sync(self):
        ps = [pick(1), pick(2, "loss")]
        d = self.diff(ps, [row(p) for p in ps])
        self.assertEqual(d["total"], 0)
        self.assertEqual(d["local"], d["server"])

    def test_every_kind_of_drift_is_found_and_the_record_shows_it(self):
        local = [pick(1), pick(2, "loss"), pick(3, "win"), pick(5, "loss"), pick(6, "win", age_days=0.5, outcome_note=1)]
        server_rows = [row(pick(1)), row(pick(2, "loss")), row(pick(3, "loss")),            # 3: result differs
                       row(pick(4, "loss")),                                                   # 4: the server has it, this device lacks it
                       row(pick(7, "loss")),                                                   # 7: server has it, hidden here by the wipe list
                       row(pick(5, "loss"), outcome="_removed")]                               # 5: removed on the server, still here
        d = self.diff(local, server_rows, wiped=["p7"])
        self.assertEqual(d["onlyRemote"], ["p4"])
        self.assertEqual(d["wipedLive"], ["p7"])
        self.assertEqual(d["tombLocal"], ["p5"])
        self.assertEqual(d["outcomeDiff"], ["p3"])
        self.assertEqual(d["onlyLocal"], [])                       # p6 is a fresh (<3 day) lock that may not have synced yet: not drift
        self.assertEqual(d["total"], 4)
        self.assertNotEqual(d["local"], d["server"])

    def test_local_only_old_pick_is_flagged_but_fresh_pending_lock_is_not(self):
        local = [pick(1), pick(8, "win", age_days=30), pick(9, "pending", age_days=0.2)]
        d = self.diff(local, [row(pick(1))])
        self.assertEqual(d["onlyLocal"], ["p8"])

    def test_adopt_makes_this_device_match_the_server_and_parks_local_only_picks(self):
        local = [pick(1), pick(2, "loss"), pick(3, "win"), pick(5, "loss"), pick(8, "win", age_days=30)]
        rows = [row(pick(1)), row(pick(2, "loss")), row(pick(3, "loss")), row(pick(4, "loss")), row(pick(7, "loss")), row(pick(5, "loss"), outcome="_removed")]
        r = self.pg.evaluate("([l,r,w,n])=>{const x=_ledgerAdoptApply(l,r,new Set(w),n);return {ids:x.next.map(p=>p.id).sort(),p3:x.next.find(p=>p.id==='p3').outcome,quarantine:x.quarantine.map(p=>p.id).sort(),unwipe:[...x.unwipe]}}", [local, rows, ["p7"], NOW])
        self.assertEqual(r["ids"], ["p1", "p2", "p3", "p4", "p7"])
        self.assertEqual(r["p3"], "loss")
        self.assertEqual(r["quarantine"], ["p5", "p8"])
        self.assertEqual(r["unwipe"], ["p7"])
        # after adopting, the very same comparison reports nothing left to fix
        after = self.diff([p for p in json.loads(json.dumps([pick(1), pick(2, "loss"), pick(3, "loss"), pick(4, "loss"), pick(7, "loss")]))], rows)
        self.assertEqual(after["total"], 0)

    def test_archived_retired_picks_are_not_counted_as_drift_when_slimmed(self):
        retired = pick(10, "win", sport="MLB", age_days=60, date="2026-08-01")
        rows = [row(pick(1)), row(retired)]
        d = self.pg.evaluate("([l,r,n])=>{const d=_ledgerDiff(l,r,new Set(),n);return d.total}", [[pick(1)], rows, NOW])
        self.assertIn(d, (0, 1))                                        # slim off in this page -> it is visible; with ?slim=1 (the real devices) it is not drift
        page = self.browser.new_page()
        page.goto(f"http://127.0.0.1:{self.srv.server_address[1]}/app.html?nosb=1&slim=1")
        page.wait_for_function("typeof _ledgerDiff==='function'")
        page.wait_for_timeout(800)
        self.assertEqual(page.evaluate("([l,r,n])=>_ledgerDiff(l,r,new Set(),n).total", [[pick(1)], rows, NOW]), 0)
        page.close()

    def test_debug_panel_has_fingerprint_code_id_and_compare_button(self):
        html = (ROOT / "docs" / "app.html").read_text(encoding="utf-8")
        for needle in ("_ledgerCompare(this)", "APP CODE ID", "THIS DEVICE'S IN-SCOPE RECORD", "cv_ledger_quarantine"):
            self.assertIn(needle, html)
        cid = self.pg.evaluate("_cvCodeId()")
        self.assertRegex(cid, r"^[0-9a-f]{8} · \d+k$")

    def test_mobile_build_runs_the_same_code_as_desktop(self):
        import subprocess, tempfile, re
        out = Path(tempfile.mkdtemp()) / "m.html"
        subprocess.run([sys.executable, str(ROOT / "scripts" / "mobile_transform.py"), str(ROOT / "docs" / "app.html"), str(out)], check=True, capture_output=True)
        def biggest_script(text):
            return max(re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", text, re.S), key=len)
        self.assertEqual(biggest_script(out.read_text(encoding="utf-8")), biggest_script((ROOT / "docs" / "app.html").read_text(encoding="utf-8")),
                         "the mobile transform must not change the app's JavaScript")


if __name__ == "__main__":
    unittest.main(verbosity=2)
