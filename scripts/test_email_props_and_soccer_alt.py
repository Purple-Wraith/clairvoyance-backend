#!/usr/bin/env python3
"""Two email rules from 2026-10-25:

  * NBA PLAYER PROPS are retired from the subscriber emails (owner: "retire player props for nba from emails but not from backend ... resume once 20 games are hit"): they are still generated, locked and tracked,
    but no locks email lists them (EMAIL_NBA_PROPS = False; flip to True to resume).  An email whose only legs are NBA props is not sent (and is never turned into a 'no picks' email).
  * Club-soccer SPREADS get adjusted lines in the emails, from the same cover table the game cards use (+1 goal of cushion -> 75.8%, quarter lines never move).

    python3 scripts/test_email_props_and_soccer_alt.py
"""
from __future__ import annotations

import os, sys, time, unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import auto_lock_settle as A  # noqa: E402


def prop(sport="NBA", player="Jayson Tatum"):
    return {"kind": "PROP", "sport": sport, "leg": {"player": player, "over": True, "line": 24.5, "stat": "PTS", "prob": .66, "grade": "PREMIUM", "hA": "BOS", "awA": "NYK", "sportTag": sport}}


def gameleg(label="OVER 3.5"):
    return {"kind": "GAME", "sport": "NBA", "hA": "BOS", "awA": "NYK", "side": "over", "label": label, "prob": .66, "tierN": 2, "evVal": .08, "dec": 1.91, "ml": "-110", "reasoning": "", "mcSummary": None, "best": None}


class Props(unittest.TestCase):
    def test_emailable_drops_only_nba_props(self):
        legs = [prop("NBA"), prop("NHL"), gameleg()]
        self.assertEqual([(q["kind"], q["sport"]) for q in A._emailable(legs)], [("PROP", "NHL"), ("GAME", "NBA")])
        with mock.patch.object(A, "EMAIL_NBA_PROPS", True):
            self.assertEqual(len(A._emailable(legs)), 3)

    def run_send(self, legs, **kw):
        sent = {}
        def fake(subject, recipients, body):
            sent.update(subject=subject, body=body, recipients=recipients); return True, "ok"
        with mock.patch.object(A, "_send_gmail", side_effect=fake), mock.patch.object(A, "OWNER_EMAIL", "owner@example.com"), mock.patch.object(A, "LOCKS_EMAIL_TO", "owner@example.com"):
            r = A.send_locks_email(legs, live=True, to=["owner@example.com"], label="NBA", **kw)
        return r, sent

    def test_a_props_only_email_is_not_sent_at_all(self):
        r, sent = self.run_send([prop(), prop(player="Jaylen Brown")], locked_count=2)
        self.assertFalse(r); self.assertEqual(sent, {})                      # not even a 'no picks cleared the bar' email

    def test_props_are_left_out_of_a_mixed_email_and_the_count_follows(self):
        r, sent = self.run_send([gameleg(), prop()], locked_count=2)
        self.assertTrue(r)
        self.assertIn("(1)", sent["subject"]); self.assertNotIn("Tatum", sent["body"]); self.assertIn("OVER 3.5", sent["body"])

    def test_the_switch_brings_them_back(self):
        with mock.patch.object(A, "EMAIL_NBA_PROPS", True):
            r, sent = self.run_send([gameleg(), prop()], locked_count=2)
        self.assertTrue(r); self.assertIn("Tatum", sent["body"]); self.assertIn("(2)", sent["subject"])

    def test_props_are_still_locked(self):
        calls = []
        with mock.patch.object(A, "lock_prop_leg", side_effect=lambda page, sport, leg: calls.append(leg["player"]) or "locked"), mock.patch.object(A, "_EMAILED_READY", False):
            res = A._lock_qualifying_legs(mock.Mock(), [prop()])
        self.assertEqual(calls, ["Jayson Tatum"]); self.assertEqual(res.new, 1)

    def test_props_are_not_a_product_pick_for_the_catch_up(self):
        import email_tracking as ET
        self.assertIsNone(ET.product_of_pick({"id": "x", "sport": "NBA", "league": "NBA", "betType": "PROP", "player": "Jayson Tatum", "hA": "BOS", "awA": "NYK", "date": "2026-12-30", "outcome": "pending"}))


class SoccerSpreadAlt(unittest.TestCase):
    def leg(self, label, side="ahFav", sport="SOC_PL", prob=.66):
        return {"sport": sport, "side": side, "label": label, "prob": prob, "dec": 1.91, "ml": "-110", "reasoning": ""}

    def test_a_half_line_moves_one_goal_and_prices_from_the_card_table(self):
        r = A._alt_shift_leg(self.leg("Arsenal -1.5"))
        self.assertEqual((r["label"], r["prob"]), ("Arsenal -0.5", .758)); self.assertEqual(r["altLine"]["postedLabel"], "Arsenal -1.5"); self.assertEqual(r["altLine"]["shift"], 1.0)
        self.assertEqual(r["priceSource"], "estimated"); self.assertIn("goal", r["reasoning"])

    def test_dog_whole_lines_and_all_four_leagues(self):
        self.assertEqual(A._alt_shift_leg(self.leg("Chelsea +0.5", "ahDog"))["label"], "Chelsea +1.5")
        self.assertEqual((A._alt_shift_leg(self.leg("Arsenal -1"))["label"], A._alt_shift_leg(self.leg("Arsenal -1"))["prob"]), ("Arsenal +0.5", .833))     # a whole line moves onto a half
        for sp in ("SOC_PL", "SOC_LIGA", "SOC_ITA", "SOC_CL"):
            self.assertEqual(A._alt_shift_leg(self.leg("Arsenal -1.5", sport=sp))["label"], "Arsenal -0.5", sp)

    def test_quarter_lines_totals_and_moneylines_never_move(self):
        self.assertIsNone(A._alt_shift_leg(self.leg("Arsenal -0.75")))
        self.assertIsNone(A._alt_shift_leg(self.leg("Arsenal +1.25", "ahDog")))
        self.assertIsNone(A._alt_shift_leg(self.leg("OVER 2.5", "over")))
        self.assertIsNone(A._alt_shift_leg(self.leg("Arsenal ML", "mlFav")))

    def test_build_qualifying_emails_the_adjusted_soccer_spread(self):
        gl = {"sport": "SOC_PL", "hA": "Arsenal", "awA": "Chelsea", "startMs": time.time() * 1000 + 3 * 3600e3, "mcSummary": None, "best": None, "socFactors": None,
              "markets": [{"side": "ahFav", "label": "Arsenal -1.5", "prob": .66, "tierN": 2, "tier": "OPTIMAL", "evVal": .08, "dec": 1.91, "ml": "-110", "priceSource": "market"}]}
        q = A.build_qualifying({"gameLegs": [gl], "propLegs": []}, only_sports=None)
        self.assertEqual([x["label"] for x in q], ["Arsenal -0.5"])
        html = A._leg_html(q[0])
        self.assertIn("ADJUSTED LINE", html); self.assertIn("Arsenal -1.5", html)


if __name__ == "__main__":
    unittest.main(verbosity=2)
