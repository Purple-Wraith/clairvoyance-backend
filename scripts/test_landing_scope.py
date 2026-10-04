#!/usr/bin/env python3
"""Landing-page figures (generate_social_cards.write_landing_json): the headline tile (engine_performance_subscriber.json) must not count retired
MLS / Bundesliga picks (owner decision 2026-10-03) and must AGREE with the by-league table (sport_performance.json), which never counted them.
Built from the committed ledger backup in a headless app page, exactly as the generator's Supabase-outage fallback does."""
import functools, http.server, json, socketserver, sys, tempfile, threading, unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import generate_social_cards as g  # noqa: E402


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a, **k):
        pass


class LandingScope(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        socketserver.TCPServer.allow_reuse_address = True
        cls.srv = socketserver.TCPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=str(ROOT / "docs")))
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch()
        page = cls.browser.new_page()
        page.goto(f"http://127.0.0.1:{cls.srv.server_address[1]}/app.html?nosb=1")
        page.wait_for_function("typeof saveP==='function'&&typeof getP==='function'")
        cls.backup = json.loads((ROOT / "docs" / "picks_backup.json").read_text())
        page.evaluate("(p)=>{saveP(p)}", cls.backup)
        cls.out = Path(tempfile.mkdtemp())
        g.write_landing_json(page, datetime.now(ZoneInfo("America/Denver")), None, cls.out)
        cls.sub = {p["key"]: p for p in json.loads((cls.out / "engine_performance_subscriber.json").read_text())["periods"]}
        cls.full = {p["key"]: p for p in json.loads((cls.out / "engine_performance.json").read_text())["periods"]}
        cls.sport = json.loads((cls.out / "sport_performance.json").read_text())
        cls.subfile = json.loads((cls.out / "engine_performance_subscriber.json").read_text())
        page.close()

    @classmethod
    def tearDownClass(cls):
        cls.browser.close(); cls.pw.stop(); cls.srv.shutdown()

    def test_headline_matches_league_table(self):
        for key, per in (("ALL_TIME", "allTime"), ("THIS_MONTH", "thisMonth"), ("LAST_MONTH", "lastMonth")):
            lg = self.sport[per]["leagues"]
            self.assertEqual((self.sub[key]["w"], self.sub[key]["l"]), (sum(x["w"] for x in lg), sum(x["l"] for x in lg)), per)

    def test_retired_leagues_not_in_basis(self):
        unk = self.subfile["basis_detail"]["unknown_timing_by_league"]
        self.assertFalse({"MLS", "BUND", "BL"} & set(unk), unk)

    def test_unscoped_file_still_counts_them(self):
        # engine_performance.json (personal/all-picks view) is unchanged: it must be >= the subscriber headline
        self.assertGreater(self.full["ALL_TIME"]["n"], self.sub["ALL_TIME"]["n"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
