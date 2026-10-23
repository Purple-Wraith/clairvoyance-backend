#!/usr/bin/env python3
"""Top Picks ranking with each KIND of pick's recent track record (docs/app.html _topPicksRankLegs / _tpTrackRecords), owner request 2026-10-23.

A kind = league x bet type x adjusted-line-or-not.  Its record = settled pre-start picks of the last 21 days, shrunk toward its bet type's record and the overall record.  A kind under 65% is demoted in the
Top 10 (still shown in its league section, still fills the list when nothing better is left).  Thin evidence changes nothing, and past days are never re-ranked with today's records.

    python3 scripts/test_top_picks_rank.py
"""
import functools, http.server, json, socketserver, threading, time, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a, **k):
        pass


NOW = int(time.time() * 1000)


def settled(i, sport, bt, outcome, alt=False, days_ago=3):
    d = {"id": f"s{i}", "sport": sport, "league": sport, "betType": bt, "betOn": "X", "hA": "AAA", "awA": "BBB", "outcome": outcome, "decOdds": 1.7, "winProb": .65,
         "date": "2026-10-15", "lockedAt": NOW - (days_ago + 1) * 86400000, "settledAt": NOW - days_ago * 86400000}
    if alt:
        d["altLine"] = {"posted": 3.5, "line": 9.5, "shift": 6, "postedLabel": "X +3.5"}
    return d


def leg(i, sport, side, label, prob, tier=2, from_ledger=False):
    l = {"sport": sport, "hA": f"H{i}", "awA": f"A{i}", "best": {"label": label, "side": side, "tierN": tier}, "markets": [{"side": side, "label": label, "prob": prob}]}
    if from_ledger:
        l["fromLedger"] = True
    return l


class Rank(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        socketserver.TCPServer.allow_reuse_address = True
        cls.srv = socketserver.TCPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=str(ROOT / "docs")))
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.pw = sync_playwright().start(); cls.b = cls.pw.chromium.launch(); cls.pg = cls.b.new_page()
        cls.pg.goto(f"http://127.0.0.1:{cls.srv.server_address[1]}/app.html?nosb=1")
        cls.pg.wait_for_function("typeof _topPicksRankLegs==='function'&&typeof saveP==='function'"); cls.pg.wait_for_timeout(1200)

    @classmethod
    def tearDownClass(cls):
        cls.b.close(); cls.pw.stop(); cls.srv.shutdown()

    def rank(self, ledger, legs):
        return self.pg.evaluate("""([led,legs])=>{saveP(led);_tpRecCache={at:0,v:null};const r=_topPicksRankLegs(legs);
          return {top10:r.top10.map(e=>({m:e.matchup,weak:e.weakKind,rec:e.kindRec&&{n:e.kindRec.n,w:e.kindRec.w,rate:+e.kindRec.rate.toFixed(3)}})),all:r.qualifying.map(e=>({m:e.matchup,weak:e.weakKind}))}}""", [ledger, legs])

    def ledger(self):
        led, i = [], 0
        for k in range(40):                      # strong kind: NHL moneylines win 85%
            led.append(settled(i, "NHL", "ML", "win" if k % 20 < 17 else "loss")); i += 1
        for k in range(40):                      # weak kind: NHL totals win 45%
            led.append(settled(i, "NHL", "OU", "win" if k % 20 < 9 else "loss")); i += 1
        for k in range(30):                      # filler so the overall record is established
            led.append(settled(i, "NFL", "ML", "win" if k % 10 < 7 else "loss")); i += 1
        return led

    def test_a_weak_kind_is_demoted_even_with_the_highest_stated_probability(self):
        legs = [leg(i, "NHL", "over", "OVER 5.5", 0.80) for i in range(3)] + [leg(10 + i, "NHL", "mlFav", f"H{10+i} ML", 0.66) for i in range(8)]
        r = self.rank(self.ledger(), legs)
        order = [e["m"] for e in r["top10"]]
        weak = {e["m"] for e in r["all"] if e["weak"]}
        self.assertEqual(len(weak), 3)                                                    # the three NHL totals are flagged weak
        self.assertTrue(all(m not in weak for m in order[:8]))                            # the eight moneylines fill the top first
        self.assertEqual(len(order), 10)

    def test_a_weak_kind_still_fills_the_list_when_nothing_better_is_left(self):
        legs = [leg(i, "NHL", "over", "OVER 5.5", 0.80) for i in range(3)] + [leg(10, "NHL", "mlFav", "H10 ML", 0.66)]
        r = self.rank(self.ledger(), legs)
        self.assertEqual(len(r["top10"]), 4)
        self.assertEqual(r["top10"][0]["m"], "A10 @ H10")                                 # the good kind leads, the weak ones follow

    def test_thin_evidence_changes_nothing(self):
        led = [settled(i, "NHL", "OU", "loss") for i in range(5)] + [settled(100 + i, "NFL", "ML", "win") for i in range(40)]
        legs = [leg(0, "NHL", "over", "OVER 5.5", 0.80), leg(1, "NHL", "mlFav", "H1 ML", 0.66)]
        r = self.rank(led, legs)
        self.assertFalse(any(e["weak"] for e in r["all"]))                                # 5 losses are not enough to condemn a kind (shrunk toward the pool)
        self.assertEqual(r["top10"][0]["m"], "A0 @ H0")                                   # so the higher stated probability still leads

    def test_with_no_ledger_it_ranks_exactly_as_before(self):
        legs = [leg(0, "NHL", "over", "OVER 5.5", 0.80), leg(1, "NHL", "mlFav", "H1 ML", 0.66)]
        r = self.rank([], legs)
        self.assertFalse(any(e["weak"] for e in r["all"]))
        self.assertEqual(r["top10"][0]["m"], "A0 @ H0")

    def test_past_days_are_never_re_ranked_with_todays_records(self):
        legs = [leg(i, "NHL", "over", "OVER 5.5", 0.80, from_ledger=True) for i in range(3)] + [leg(10, "NHL", "mlFav", "H10 ML", 0.66, from_ledger=True)]
        r = self.rank(self.ledger(), legs)
        self.assertFalse(any(e["weak"] for e in r["all"]))                                # a historical day keeps its original order
        self.assertEqual(r["top10"][0]["m"], "A0 @ H0")

    def test_old_results_outside_the_window_do_not_count(self):
        led = [settled(i, "NHL", "OU", "loss", days_ago=60) for i in range(60)] + [settled(100 + i, "NFL", "ML", "win") for i in range(40)]
        r = self.rank(led, [leg(0, "NHL", "over", "OVER 5.5", 0.80), leg(1, "NHL", "mlFav", "H1 ML", 0.66)])
        self.assertFalse(any(e["weak"] for e in r["all"]))

    def test_the_row_shows_the_kinds_record_and_flags_a_demotion(self):
        self.rank(self.ledger(), [leg(0, "NHL", "over", "OVER 5.5", 0.80), leg(1, "NHL", "mlFav", "H1 ML", 0.66)])
        html = self.pg.evaluate("""()=>{const r=_topPicksRankLegs([{sport:'NHL',hA:'H0',awA:'A0',best:{label:'OVER 5.5',side:'over',tierN:2},markets:[{side:'over',label:'OVER 5.5',prob:.8}]},
          {sport:'NHL',hA:'H1',awA:'A1',best:{label:'H1 ML',side:'mlFav',tierN:2},markets:[{side:'mlFav',label:'H1 ML',prob:.66}]}]);return r.top10.map(e=>_topPickRowHTML(e)).join('')}""")
        self.assertIn("LAST 40 SAME KIND", html)
        self.assertIn("DEMOTED", html)
        self.assertIn("85% LAST 40", html)                                                # the strong kind's raw record (34 of 40)
        self.assertIn("45% LAST 40", html)


if __name__ == "__main__":
    unittest.main(verbosity=2)
