#!/usr/bin/env python3
"""The 95 NFL player-prop picks purged 2026-10-09 stay gone on every device: the app carries their ids as a built-in wipe list, so a device that still holds them locally drops them on load and
never re-pushes them.  The backup file under data/purged/ is the source of truth for the ids.

    python3 scripts/test_purged_ids.py
"""
import functools, http.server, json, socketserver, threading, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BACKUP = json.loads((ROOT / "data" / "purged" / "nfl_props_20261009.json").read_text())
IDS = sorted(r["id"] for r in BACKUP["rows"])


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a, **k):
        pass


class Purged(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        socketserver.TCPServer.allow_reuse_address = True
        cls.srv = socketserver.TCPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=str(ROOT / "docs")))
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.pw = sync_playwright().start()
        cls.b = cls.pw.chromium.launch()

    @classmethod
    def tearDownClass(cls):
        cls.b.close(); cls.pw.stop(); cls.srv.shutdown()

    def page(self):
        pg = self.b.new_page()
        pg.goto(f"http://127.0.0.1:{self.srv.server_address[1]}/app.html?nosb=1")
        pg.wait_for_function("typeof getP==='function'&&typeof _CV_PURGED_IDS!=='undefined'")
        pg.wait_for_timeout(800)
        return pg

    def test_backup_is_the_95_football_props(self):
        self.assertEqual(len(IDS), 95)
        self.assertTrue(all(r["raw"].get("betType") == "PROP" and r["raw"].get("sport") == "FOOTBALL" for r in BACKUP["rows"]))

    def test_app_carries_exactly_those_ids(self):
        pg = self.page()
        self.assertEqual(sorted(pg.evaluate("[..._CV_PURGED_IDS]")), IDS)
        self.assertTrue(pg.evaluate("i=>_getWipedIds().has(i)", IDS[0]))
        pg.close()

    def test_a_device_holding_them_drops_them_and_keeps_everything_else(self):
        pg = self.page()
        keep = {"id": "keepme_1", "sport": "NFL", "betType": "SPREAD", "betOn": "DAL -9.5", "hA": "DAL", "awA": "TB", "outcome": "win", "date": "2026-10-05", "lockedAt": 1790000000000, "winProb": 0.6, "decOdds": 1.9}
        gone = dict(next(r["raw"] for r in BACKUP["rows"]))
        n = pg.evaluate("([k,g])=>{localStorage.setItem('preds',JSON.stringify([k,g]));return getP().map(p=>p.id)}", [keep, gone])
        self.assertEqual(n, ["keepme_1"])
        stored = pg.evaluate("JSON.parse(localStorage.getItem('preds')).map(p=>p.id)")
        self.assertEqual(stored, ["keepme_1"])                      # the local copy is healed too, not just hidden
        pg.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
