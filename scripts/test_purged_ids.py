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


class Base(unittest.TestCase):
    __test__ = False

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

class Purged(Base):
    __test__ = True

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


RETAG = json.loads((ROOT / "data" / "purged" / "wnba_props_retag_20261009.json").read_text())


class Retagged(Base):
    __test__ = True
    """15 WNBA props that had been locked as sport 'NBA' (2026-10-09): the app heals a device's old copies on load so they never show under NBA > Locked."""
    def test_a_device_holding_old_nba_tagged_wnba_props_is_healed(self):
        pg = self.page()
        r = RETAG["rows"][0]["raw"]
        old = dict(r, sport="NBA", league="NBA")
        keep = {"id": "nbaprop_keep", "sport": "NBA", "betType": "PROP", "betOn": "Jalen Brunson PTS OVER 27.5", "hA": "SA", "awA": "NYK", "outcome": "win", "date": "2026-06-05", "lockedAt": 1790000000000}
        out = pg.evaluate("([a,b])=>{localStorage.setItem('preds',JSON.stringify([a,b]));getP();return JSON.parse(localStorage.getItem('preds')).map(p=>[p.id,p.sport,p.league])}", [old, keep])
        self.assertEqual(out[0], [old["id"], "WNBA", "WNBA"])
        self.assertEqual(out[1][0:2], ["nbaprop_keep", "NBA"])                       # a genuine NBA prop is untouched
        nba_locked = pg.evaluate("getP().filter(p=>p.sport==='NBA').map(p=>p.id)")
        self.assertNotIn(old["id"], nba_locked)
        pg.close()

    def test_backup_lists_15_wnba_props(self):
        self.assertEqual(len(RETAG["rows"]), 15)
        self.assertTrue(all(x["raw"]["betType"] == "PROP" for x in RETAG["rows"]))
        self.assertEqual(sorted(x["id"] for x in RETAG["rows"]), pg_ids(self))


def pg_ids(case):
    pg = case.page()
    ids = sorted(pg.evaluate("[..._CV_RETAG_WNBA]"))
    pg.close()
    return ids


if __name__ == "__main__":
    unittest.main(verbosity=2)
