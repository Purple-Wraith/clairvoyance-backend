#!/usr/bin/env python3
"""Ledger archive (docs/ledger_archive.json): the app keeps settled retired-league picks out of its working ledger.
Checks: the archive + _ARCH_SUM constant are current; nothing is lost (archive + working == backup); automation browsers (CI) are NOT slimmed;
the slimmed app drops exactly the archived picks, never re-adds them on a Supabase merge, and never hides a pick that is pending / recent / of an unknown tag."""
import functools, http.server, json, socketserver, subprocess, sys, threading, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a, **k):
        pass


class ArchiveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        socketserver.TCPServer.allow_reuse_address = True
        cls.srv = socketserver.TCPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=str(DOCS)))
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch()
        cls.backup = (DOCS / "picks_backup.json").read_text()
        cls.app_src = (DOCS / "app.html").read_text(encoding="utf-8")
        cls.archive = json.loads((DOCS / "ledger_archive.json").read_text())

    @classmethod
    def tearDownClass(cls):
        cls.browser.close(); cls.pw.stop(); cls.srv.shutdown()

    def page(self, qs):
        pg = self.browser.new_page()
        pg.add_init_script("localStorage.setItem('preds', %s)" % json.dumps(self.backup))
        pg.goto(f"http://127.0.0.1:{self.port}/app.html?nosb=1{qs}")
        pg.wait_for_function("typeof getP==='function'&&typeof _slimOn==='function'&&typeof _ARCH_SUM!=='undefined'")
        pg.wait_for_timeout(1500)
        return pg

    def test_archive_is_current(self):
        r = subprocess.run([sys.executable, str(ROOT / "scripts" / "build_ledger_archive.py"), "--check"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_summary_matches_file(self):
        s = self.archive["summary"]; picks = self.archive["picks"]
        self.assertEqual(s["n"], len(picks))
        self.assertEqual(s["w"], sum(p["outcome"] == "win" for p in picks))
        self.assertEqual(s["l"], sum(p["outcome"] == "loss" for p in picks))

    def test_nothing_lost(self):
        ids_arch = {p["id"] for p in self.archive["picks"]}
        pg = self.page("&slim=1")
        working = {p["id"] for p in pg.evaluate("getP()")}
        backup = {p["id"] for p in json.loads(self.backup)}
        self.assertFalse(working & ids_arch, "archived picks leaked into the working ledger")
        self.assertFalse(backup - (working | ids_arch), "a pick in the backup is in neither the working ledger nor the archive")
        extras = (working | ids_arch) - backup            # only picks the app seeds itself (hard-coded in app.html) may exist beyond the backup
        self.assertTrue(all(("id:'%s'" % i) in self.app_src for i in extras), sorted(extras)[:3])
        pg.close()

    def test_automation_is_not_slimmed(self):
        pg = self.page("")  # Playwright => navigator.webdriver===true
        self.assertFalse(pg.evaluate("_slimOn()"))
        ids = {p["id"] for p in pg.evaluate("getP()")}; bk = {p["id"] for p in json.loads(self.backup)}
        self.assertFalse(bk - ids, "automation must see every backup pick")                       # nothing hidden from CI
        self.assertTrue(all(("id:'%s'" % i) in self.app_src for i in ids - bk))                  # extras = the app's own seeded picks
        pg.close()

    def test_merge_does_not_readd_archived(self):
        pg = self.page("&slim=1")
        n = pg.evaluate("""(ps)=>{const before=getP().length;
            const rows=ps.map(p=>({id:p.id,raw:p,outcome:p.outcome,settled_at:null,locked_at:p.lockedAt}));
            _mergeRemoteBetRows(rows);return [before,getP().length]}""", self.archive["picks"])
        self.assertEqual(n[0], n[1])
        pg.close()

    def test_stale_pending_retired_row_not_pulled_in(self):
        # an old retired pick that is still 'pending' on Supabase (a stale row, as j13_slip2_wnba once was) must not come back as a phantom pending pick
        pg = self.page("&slim=1")
        r = pg.evaluate("""(ps)=>{const p=ps.find(x=>x.sport==='MLB');const raw=Object.assign({},p,{outcome:'pending',settledAt:null});
            const before=getP().length;_mergeRemoteBetRows([{id:p.id,raw:raw,outcome:'pending',settled_at:null,locked_at:p.lockedAt}]);
            return [before,getP().length,getP().some(x=>x.id===p.id)]}""", self.archive["picks"])
        self.assertEqual(r, [r[0], r[0], False])
        pg.close()

    def test_predicate_edges(self):
        pg = self.page("&slim=1")
        r = pg.evaluate("""()=>{const old='2026-01-05',recent=_mtDateOf(Date.now()-2*86400000);
            const mk=(o)=>Object.assign({id:'x',sport:'MLB',outcome:'win',date:old,hA:'A',awA:'B',betOn:'A',betType:'ML'},o);
            return [_archivable(mk({})),_archivable(mk({outcome:'pending'})),_archivable(mk({date:recent})),_archivable(mk({sport:'NHL'})),_archivable(mk({sport:'ZZZ'})),_archivable(mk({outcome:'_removed'})),\
                    _archivable(mk({sport:'MLS',date:recent})),_archivable(mk({sport:'BUND',date:recent})),_archivable(mk({sport:'MLS',date:recent,outcome:'pending'}))]}""")
        # MLB: old+settled only. MLS/BUND (retired, no live picks): settled at ANY age, still never while pending
        self.assertEqual(r, [True, False, False, False, False, False, True, True, False])
        pg.close()

    def test_header_accuracy_unchanged(self):
        full = self.page("&slim=0"); slim = self.page("&slim=1")
        calc = "(()=>{const p=getP().filter(x=>x.outcome!=='pending');const a=(_slimOn()?_ARCH_SUM:{n:0,w:0});return (p.filter(x=>x.outcome==='win').length+a.w)/(p.length+a.n)})()"
        self.assertAlmostEqual(full.evaluate(calc), slim.evaluate(calc), places=12)
        full.close(); slim.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
