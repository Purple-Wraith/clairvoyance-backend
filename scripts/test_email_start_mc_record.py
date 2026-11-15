#!/usr/bin/env python3
"""2026-11-15 email additions: every game card in the picks emails shows the game's start time (Mountain) and a labelled MONTE CARLO SIM line; every settlement email opens
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
        self.assertIn("MONTE CARLO SIM", html)
        self.assertNotIn("MC SIM", html)
        self.assertNotIn("MC PROJ", html)
        self.assertIn("ANA 3.1 – CGY 2.2 (Total 5.3)", html)

    def test_catchup_leg_rebuilds_the_sim_line_from_the_ledger_reasoning(self):
        pick = {"id": "x", "sport": "NHL", "hA": "CGY", "awA": "ANA", "betType": "ML", "betOn": "ANA ML", "date": "2026-11-15", "startMs": START,
                "reasoning": "PICK: ANA ML — OPTIMAL (64.9% win prob, EV +6.5%)\nMODEL: Projected 3.1–2.2 (total 5.3) (OT/SO in 10% of outcomes), from an exact Poisson distribution (not a simulation)."}
        leg = et.pick_to_leg(pick)
        self.assertEqual(leg["mcSummary"], "MC PROJ: ANA 3.1 – CGY 2.2 (Total 5.3)")
        self.assertIn("MONTE CARLO SIM", a.build_locks_email_html([leg], live=True, locked_count=1))

    def test_no_projection_in_the_reasoning_means_no_sim_line(self):
        self.assertIsNone(et._mc_from_reasoning({"hA": "H", "awA": "A", "reasoning": "PICK: A ML — OPTIMAL (64%)"}))
        self.assertIsNone(et._mc_from_reasoning({"hA": "H", "awA": "A"}))


class McText(unittest.TestCase):
    def test_prefix_is_dropped_under_the_monte_carlo_label(self):
        self.assertEqual(a._mc_text("MC PROJ: ANA 3.1 \u2013 CGY 2.2 (Total 5.3)"), "ANA 3.1 \u2013 CGY 2.2 (Total 5.3)")
        self.assertEqual(a._mc_text("MC PROJ TOTAL: 49.8 \u00b7 SEA projected -3.5"), "Projected total: 49.8 \u00b7 SEA projected -3.5")
        self.assertEqual(a._mc_text("xG PROJ: A 1.1 \u2013 B 0.9"), "xG PROJ: A 1.1 \u2013 B 0.9")


    def test_internal_wording_is_cleaned(self):
        self.assertEqual(a._mc_text("EXACT-POISSON PROJ: Lulea 2.6 \u2013 Brynas 3.2 (Total 5.9)"), "Lulea 2.6 \u2013 Brynas 3.2 (Total 5.9)")
        self.assertEqual(a._mc_text("MC PROJ: IND 109.6 \u2013 PHI 120.3 (Total 229.9, exact)"), "IND 109.6 \u2013 PHI 120.3 (Total 229.9)")


class FootballLine(unittest.TestCase):
    """Found 2026-11-15: the CFB/NFL email line printed the POSTED spread as the model's 'projected' margin. It must come from the engine's own margin."""
    def test_football_summary_uses_the_model_margin_not_the_posted_spread(self):
        src = (Path(__file__).resolve().parent.parent / "docs" / "app.html").read_text(encoding="utf-8")
        lines = [l for l in src.splitlines() if "_autoLockCapture('CFB'" in l or "_autoLockCapture('NFL'" in l]
        self.assertEqual(len(lines), 2)
        for l in lines:
            self.assertNotIn("baseSpread", l)
            self.assertIn("mc.avgMargin", l)


class Cushion(unittest.TestCase):
    ALT = {"posted": 49.5, "line": 56.5, "shift": 7.0, "postedLabel": "UNDER 49.5", "postedProb": 0.444}

    def _card(self, label, line, summary, sport="CFB", **kw):
        alt = dict(self.ALT, line=line)
        return _game(sport=sport, kind="GAME", label=label, altLine=alt, mcSummary=summary, side="under", **kw)

    def test_under_with_room_shows_positive_cushion(self):
        html = a.build_locks_email_html([self._card("UNDER 56.5", 56.5, "MC PROJ TOTAL: 49.8 \u00b7 SEA projected -3.5")], live=True, locked_count=1)
        self.assertIn("Projection 49.8, adjusted line UNDER 56.5: 6.7 points of cushion.", html)

    def test_over_cushion_and_goals_unit_for_hockey(self):
        html = a.build_locks_email_html([self._card("OVER 4.5", 4.5, "MC PROJ: ANA 3.1 \u2013 CGY 2.2 (Total 5.3)", sport="NHL")], live=True, locked_count=1)
        self.assertIn("Projection 5.3, adjusted line OVER 4.5: 0.8 goals of cushion.", html)

    def test_projection_on_the_other_side_is_said_plainly(self):
        html = a.build_locks_email_html([self._card("OVER 214.5", 214.5, "MC PROJ: IND 105.0 \u2013 PHI 108.0 (Total 213.0)", sport="NBA")], live=True, locked_count=1)
        self.assertIn("the projection is 1.5 points on the other side of this line.", html)

    def test_no_note_without_a_total_or_for_a_non_adjusted_pick(self):
        self.assertEqual(a._cushion_notes([self._card("UNDER 56.5", 56.5, None)], None), [])
        self.assertEqual(a._cushion_notes([_game()], "MC PROJ: ANA 3.1 \u2013 CGY 2.2 (Total 5.3)"), [])
        self.assertEqual(a._cushion_notes([self._card("MISS +8.5", 8.5, "MC PROJ TOTAL: 49.8")], "MC PROJ TOTAL: 49.8"), [])


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
