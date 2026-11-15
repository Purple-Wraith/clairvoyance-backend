#!/usr/bin/env python3
"""2026-11-15 email additions: every game card in the picks emails shows the game's start time (Mountain) and a labelled MC SIM line; every settlement email opens
with the day's record and win %. Pure HTML builders -- no browser."""
import sys, unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
import auto_lock_settle as a  # noqa: E402
import email_tracking as et  # noqa: E402

MT = ZoneInfo("America/Denver")
START = datetime(2026, 11, 15, 13, 0, tzinfo=MT).timestamp() * 1000          # Sunday Nov 15, 1:00 PM MT


def _game(**kw):
    q = {"kind": "GAME", "sport": "NHL", "hA": "CGY", "awA": "ANA", "side": "mlHome", "label": "ANA ML", "prob": 0.65, "ml": "-156", "dec": 1.64,
         "tierN": 2, "evVal": 0.06, "lane": False, "priceSource": "market", "startMs": START,
         "mcSummary": "MC PROJ: ANA 3.1 – CGY 2.2 (Total 5.3)"}
    q.update(kw)
    return q


def _bet(outcome, **kw):
    b = {"sport": "NHL", "outcome": outcome, "betOn": "ANA ML", "hA": "CGY", "awA": "ANA", "hScore": 2, "aScore": 3, "date": "2026-11-14"}
    b.update(kw)
    return b


class StartTime(unittest.TestCase):
    def test_card_shows_the_start_time_in_mountain(self):
        html = a.build_locks_email_html([_game()], live=True, locked_count=1)
        self.assertIn("Starts Sun Nov 15 · 1:00 PM MT", html)

    def test_no_start_no_label(self):
        html = a.build_locks_email_html([_game(startMs=None)], live=True, locked_count=1)
        self.assertNotIn("Starts ", html)

    def test_each_game_gets_its_own_time(self):
        later = START + 3 * 3600 * 1000
        html = a.build_locks_email_html([_game(), _game(hA="EDM", awA="VAN", label="VAN ML", startMs=later)], live=True, locked_count=2)
        self.assertIn("1:00 PM MT", html)
        self.assertIn("4:00 PM MT", html)

    def test_label_helper(self):
        self.assertEqual(a._start_label(START), "Sun Nov 15 · 1:00 PM MT")
        self.assertEqual(a._start_label(None), "")


class McSim(unittest.TestCase):
    def test_sim_line_is_labelled_on_the_card(self):
        html = a.build_locks_email_html([_game()], live=True, locked_count=1)
        self.assertIn("MC SIM", html)
        self.assertIn("ANA 3.1 – CGY 2.2 (Total 5.3)", html)

    def test_catchup_leg_rebuilds_the_sim_line_from_the_ledger_reasoning(self):
        pick = {"id": "x", "sport": "NHL", "hA": "CGY", "awA": "ANA", "betType": "ML", "betOn": "ANA ML", "date": "2026-11-15", "startMs": START,
                "reasoning": "PICK: ANA ML — OPTIMAL (64.9% win prob, EV +6.5%)\nMODEL: Projected 3.1–2.2 (total 5.3) (OT/SO in 10% of outcomes), from an exact Poisson distribution (not a simulation)."}
        leg = et.pick_to_leg(pick)
        self.assertEqual(leg["mcSummary"], "MC PROJ: ANA 3.1 – CGY 2.2 (Total 5.3)")
        self.assertIn("MC SIM", a.build_locks_email_html([leg], live=True, locked_count=1))

    def test_no_projection_in_the_reasoning_means_no_sim_line(self):
        self.assertIsNone(et._mc_from_reasoning({"hA": "H", "awA": "A", "reasoning": "PICK: A ML — OPTIMAL (64%)"}))
        self.assertIsNone(et._mc_from_reasoning({"hA": "H", "awA": "A"}))


class SettlementTop(unittest.TestCase):
    def test_record_and_win_pct_lead_the_email(self):
        bets = [_bet("win"), _bet("win"), _bet("win"), _bet("loss"), _bet("loss")]
        for customer in (False, True):
            html = a.build_settlement_email_html(bets, customer=customer)
            self.assertIn("3W-2L", html)
            self.assertIn("60.0% win", html)
            self.assertLess(html.index("60.0% win"), html.index("ANA ML"))          # before the first result row

    def test_pushes_do_not_count_in_the_win_pct(self):
        html = a.build_settlement_email_html([_bet("win"), _bet("loss"), _bet("push")])
        self.assertIn("1W-1L-1P", html)
        self.assertIn("50.0% win", html)

    def test_nothing_decided_shows_a_dash_not_a_divide_error(self):
        html = a.build_settlement_email_html([_bet("push")])
        self.assertIn("— win", html)

    def test_empty_run_still_builds(self):
        self.assertIn("No bets settled this run", a.build_settlement_email_html([]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
