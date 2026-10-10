#!/usr/bin/env python3
"""All Bets (2026-10-10 owner requests): (1) marking a bet WIN/LOSS or removing it must not make the screen jump -- the scroll position is held while the lists re-render; (2) inside every date, completed
(settled) events come first and pending ones after.

    python3 scripts/test_allbets_hold_order.py
"""
import datetime as dt, functools, http.server, re, socketserver, threading, unittest
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
MT = ZoneInfo("America/Denver")


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a, **k):
        pass


def old_ledger(n=60, date="2026-09-20"):
    """settled bets on an old date: they live in YEAR > MONTH > DAY folders that start collapsed"""
    rows = []
    for i in range(n):
        rows.append({"id": f"old{i}", "sport": "NHL", "betType": "ML", "betOn": f"NHL O{i} ML", "hA": f"OH{i}", "awA": f"OA{i}", "date": date, "lockedAt": 1790000000000 + i * 60000,
                     "outcome": "win" if i % 2 else "loss", "decOdds": 1.9, "winProb": 0.6})
    return rows


def ledger(now_ms, n_pending=40, n_settled=12):
    rows, k = [], 0
    today = dt.datetime.fromtimestamp(now_ms / 1000, MT).strftime("%Y-%m-%d")
    for i in range(n_pending + n_settled):
        k += 1
        pend = i < n_pending
        rows.append({"id": f"ab{k}", "sport": "NHL", "betType": "ML", "betOn": f"NHL T{k} ML", "hA": f"H{k}", "awA": f"A{k}", "date": today, "lockedAt": now_ms - k * 60000,
                     "outcome": "pending" if pend else ("win" if k % 2 else "loss"), "decOdds": 1.9, "winProb": 0.6})
    return rows


class AllBets(unittest.TestCase):
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

    def page(self):
        pg = self.b.new_page(viewport={"width": 1100, "height": 800})
        self.errors = []
        pg.on("pageerror", lambda e: self.errors.append(str(e)))
        pg.route(re.compile(r"https://fonts\.(googleapis|gstatic)\.com/.*"), lambda r: r.abort())
        pg.goto(f"http://127.0.0.1:{self.srv.server_address[1]}/app.html?nosb=1", timeout=90000)
        pg.wait_for_function("typeof saveP==='function'&&typeof renderOverallHistory==='function'&&typeof recR==='function'", timeout=90000)
        pg.wait_for_timeout(500)
        now = pg.evaluate("Date.now()")
        pg.evaluate("(r)=>{saveP(r);SS('ovr');T('ovr','history');renderOverallHistory()}", ledger(now))
        pg.wait_for_selector("#ovr-pending-section")
        pg.wait_for_timeout(500)
        return pg

    def scroller_top(self, pg):
        return pg.evaluate("document.getElementById('ovr-sa').scrollTop")

    def test_marking_a_bet_does_not_jump(self):
        pg = self.page()
        pg.evaluate("document.getElementById('ovr-sa').scrollTop=900")
        pg.wait_for_timeout(200)
        before = self.scroller_top(pg)
        self.assertGreater(before, 500)
        btn = pg.locator("#ovr-pending-section .btn-win").nth(12)
        btn.scroll_into_view_if_needed(); pg.wait_for_timeout(100)
        before = self.scroller_top(pg)
        btn.click()
        for ms in (50, 200, 500, 1200):
            pg.wait_for_timeout(ms)
            self.assertLess(abs(self.scroller_top(pg) - before), 3, f"jumped at +{ms}ms")
        self.assertEqual(pg.evaluate("getP().filter(p=>p.outcome==='pending').length"), 39)
        self.assertEqual(self.errors, [])
        pg.close()

    def test_removing_a_bet_does_not_jump(self):
        pg = self.page()
        pg.on("dialog", lambda d: d.accept())
        pg.evaluate("document.getElementById('ovr-sa').scrollTop=900"); pg.wait_for_timeout(200)
        btn = pg.locator("#ovr-pending-section button:has-text('REMOVE')").nth(15)
        btn.scroll_into_view_if_needed(); pg.wait_for_timeout(100)
        before = self.scroller_top(pg)
        btn.click()
        for ms in (50, 200, 500, 1200):
            pg.wait_for_timeout(ms)
            self.assertLess(abs(self.scroller_top(pg) - before), 3, f"jumped at +{ms}ms")
        self.assertEqual(pg.evaluate("getP().length"), 51)
        pg.close()

    def test_removing_inside_an_opened_old_date_keeps_the_folder_open_and_the_position(self):
        pg = self.page()
        pg.on("dialog", lambda d: d.accept())
        now = pg.evaluate("Date.now()")
        pg.evaluate("(r)=>{saveP(r);renderOverallHistory()}", old_ledger() + ledger(now, n_pending=2, n_settled=2))
        pg.wait_for_selector("#ovr-pending-section")
        pg.locator("#ovr-history-list div[onclick*='yfold_2026']").first.click()
        pg.locator("#ovr-history-list div[onclick*='mfold_202609']").first.click()
        pg.locator("#ovr-history-list div[onclick*='dfold_2026-09']").first.click()
        pg.wait_for_selector("#ovr-history-list .prow2 button:has-text('REMOVE') >> visible=true")
        pg.evaluate("document.getElementById('ovr-sa').scrollTop=document.getElementById('ovr-sa').scrollHeight-document.getElementById('ovr-sa').clientHeight-400"); pg.wait_for_timeout(250)
        before = self.scroller_top(pg)
        btn = pg.locator("#ovr-history-list .prow2:visible button:has-text('REMOVE')").nth(6)
        btn.scroll_into_view_if_needed(); pg.wait_for_timeout(100)
        before = self.scroller_top(pg)
        btn.click()
        for ms in (50, 200, 500, 1200):
            pg.wait_for_timeout(ms)
            self.assertLess(abs(self.scroller_top(pg) - before), 80, f"jumped at +{ms}ms: {before} -> {self.scroller_top(pg)}")      # one row (~100px) may leave, the view must not travel
        self.assertGreater(pg.locator("#ovr-history-list .prow2:visible").count(), 20)                               # the day folder stayed open
        self.assertEqual(pg.evaluate("getP().filter(p=>p.id.startsWith('old')).length"), 59)
        pg.close()

    def test_user_scroll_is_not_fought(self):
        pg = self.page()
        pg.evaluate("document.getElementById('ovr-sa').scrollTop=900"); pg.wait_for_timeout(200)
        pg.locator("#ovr-pending-section .btn-win").nth(10).click()
        pg.mouse.move(600, 400); pg.mouse.wheel(0, 700); pg.wait_for_timeout(1300)
        self.assertGreater(self.scroller_top(pg), 1100)                      # the wheel moved it and the hold did not drag it back
        pg.close()

    def test_completed_events_come_first_within_a_date(self):
        pg = self.page()
        order = pg.evaluate("""()=>{
          const wrap=document.getElementById('ovr-history-list');
          wrap.querySelectorAll('[onclick*="ovrToggle"],[onclick*="Folder"],.ovr-folder-h').forEach(()=>{});
          // open every folder by calling the lazy builders the list registers
          Object.keys(window._ovrLazy||{}).forEach(k=>{const el=document.getElementById(k);if(el&&!el.innerHTML.trim()){try{el.innerHTML=window._ovrLazy[k]()}catch(e){}}});
          const days=[...wrap.querySelectorAll('div[id]')].filter(d=>d.querySelectorAll('.card').length>1);
          return days.map(d=>[...d.querySelectorAll('.card')].map(c=>c.querySelector('.btn-win')?'P':'S').join(''));
        }""")
        self.assertTrue(order, "no day folder rendered")
        for seq in order:
            self.assertNotRegex(seq, r"PS", seq)          # never a pending row above a completed one
        pg.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
