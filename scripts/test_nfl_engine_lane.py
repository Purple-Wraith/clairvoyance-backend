#!/usr/bin/env python3
"""NFL alt-line lane + evidence tool (2026-10-11 NFL engine audit): what build_qualifying does with an anchored NFL card, that the moneyline selection is exactly what it was, and that the committed
evidence (data/nfl_closing_lines_2026.json -> scripts/backtest_nfl_engine.py) still supports the constants it justifies.

    python3 scripts/test_nfl_engine_lane.py
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import auto_lock_settle as A  # noqa: E402
import backtest_nfl_engine as B  # noqa: E402
import nfl_engine as NE  # noqa: E402


def leg(side, label, prob, tier=0, legacy=None, ml="-110", dec=1.909, ev=None, **kw):
    d = {"side": side, "label": label, "prob": prob, "ml": ml, "dec": dec, "tierN": tier, "evVal": prob * dec - 1 if ev is None else ev, "rawProb": None, "capCeil": None, "priceSource": None,
         "modelProb": None, "marketProb": None, "blendAlpha": None, "hkLane": False, "reasoning": "PICK", "legacyTierN": legacy}
    d.update(kw)
    return d


def qual(markets, sport="NFL"):
    res = {"gameLegs": [{"sport": sport, "hA": "HOME", "awA": "AWAY", "markets": markets, "mcSummary": None, "best": None, "socFactors": None, "startMs": None}], "propLegs": []}
    return A.build_qualifying(res, now=0)


class Lane(unittest.TestCase):
    def test_a_legacy_optimal_spread_goes_to_its_alternate_line_and_never_locks_at_the_posted_line(self):
        q = qual([leg("sprdFav", "HOME -7.5", .541, tier=0, legacy=2, marketProb=.5, modelProb=.86, blendAlpha=.9)])
        self.assertEqual(len(q), 1)
        self.assertTrue(q[0]["nflLane"])
        self.assertEqual(q[0]["altLine"]["posted"], -7.5)
        self.assertEqual(q[0]["label"], "HOME -1.5")                               # +6 cushion
        self.assertAlmostEqual(q[0]["prob"], .673, places=3)                       # the empirical cushion curve's number, not the model's
        self.assertEqual(q[0]["altLine"]["postedProb"], .541)                      # the honest anchored posted-line probability is kept in the audit trail
        self.assertIn("rated this side 54.1%", q[0]["reasoning"])
        self.assertEqual(q[0]["priceSource"], "estimated")

    def test_over_under_lane_leg_moves_toward_safety(self):
        q = qual([leg("under", "UNDER 45.5", .512, legacy=3)])
        self.assertEqual([x["label"] for x in q], ["UNDER 51.5"])
        q = qual([leg("over", "OVER 41.5", .512, legacy=2)])
        self.assertEqual([x["label"] for x in q], ["OVER 35.5"])

    def test_no_lane_without_the_legacy_selector_or_for_other_sports_or_when_switched_off(self):
        self.assertEqual(qual([leg("sprdFav", "HOME -7.5", .541, legacy=None)]), [])          # a card that did not anchor: only tier() qualifies it, as before
        self.assertEqual(qual([leg("sprdFav", "HOME -7.5", .541, legacy=1)]), [])
        self.assertEqual(qual([leg("sprdFav", "HOME -7.5", .541, legacy=3)], sport="CFB"), [])
        old = NE.NFL_ALT_LANE
        NE.NFL_ALT_LANE = False
        try:
            self.assertEqual(qual([leg("sprdFav", "HOME -7.5", .541, legacy=3)]), [])
        finally:
            NE.NFL_ALT_LANE = old

    def test_a_lane_leg_with_no_priceable_alternate_line_is_dropped_not_locked_at_the_posted_line(self):
        # an over on a 2.5 total cannot be moved down by a cushion (line would go <= 0)
        self.assertEqual(qual([leg("over", "OVER 2.5", .55, legacy=2)]), [])

    def test_a_leg_that_qualifies_through_tier_is_unchanged(self):
        q = qual([leg("sprdFav", "HOME -7.5", .62, tier=2, legacy=2)])
        self.assertEqual(len(q), 1)
        self.assertFalse(q[0]["nflLane"])
        self.assertEqual(q[0]["label"], "HOME -1.5")                                            # still shifted by the existing alt-line rule

    def test_moneyline_selection_is_exactly_what_it_was(self):
        """Before the anchor an OPTIMAL spread (ev .184 at the clamp) outranked a tier-2 moneyline (ev ~0) and a tier-3 moneyline outranked it; the lane keeps both orderings."""
        ml2 = leg("mlFav", "HOME ML", .86, tier=2, ml="-600", dec=1.167, ev=0.0)
        ml3 = leg("mlFav", "HOME ML", .73, tier=3, ml="-270", dec=1.37, ev=0.0)
        sp = leg("sprdFav", "HOME -7.5", .541, tier=0, legacy=2)
        ou = leg("under", "UNDER 45.5", .52, tier=0, legacy=2)
        # tier-2 ML + lane spread: the spread wins the slot (as the clamped OPTIMAL spread always did) and the ML is not locked
        self.assertEqual([x["label"] for x in qual([ml2, sp])], ["HOME -1.5"])
        # tier-3 ML + lane spread: the ML wins
        self.assertEqual([x["label"] for x in qual([ml3, sp])], ["HOME ML"])
        # ML + lane over/under are complementary (kept together), as ML + O/U always were
        self.assertEqual(sorted(x["label"] for x in qual([ml3, ou])), ["HOME ML", "UNDER 51.5"])
        # a moneyline alone is untouched by any of this
        q = qual([ml3])
        self.assertEqual([(x["label"], x["prob"], x["ml"]) for x in q], [("HOME ML", .73, "-270")])
        self.assertFalse(q[0]["nflLane"])

    def test_lane_flag_and_selector_ride_the_leg(self):
        q = qual([leg("under", "UNDER 45.5", .5, legacy=2)])
        self.assertTrue(q[0]["nflLane"])
        self.assertEqual(q[0]["legacyTierN"], 2)


class LaneLineupGate(unittest.TestCase):
    """The confirmed-lineup gate (scripts/lineups.py) still protects a lane leg: the QB-out hard rule does not depend on the leg's tier or probability."""

    def test_qb_out_holds_a_lane_leg_on_the_side_it_backs(self):
        import test_lineups as TL
        c = TL.ctx(TL.nfl_routes(TL.nfl_sum(**{"Caleb Williams": "Out"})), TL.nfl_model())
        q = TL.nfl_leg(label="CHI +3.5", side="sprdDog", prob=0.54, dec=1.909, tierN=2, nflLane=True, legacyTierN=2)
        d = TL.L.assess_leg(q, c, TL.NFL_NOW)
        self.assertEqual(d["action"], "hold")
        self.assertIn("QB out", d["reason"])

    def test_an_unconfirmed_qb_leaves_a_lane_leg_on_the_other_side_alone(self):
        import test_lineups as TL
        # the real payload has CHI's QB Questionable (open) and its RB1 Out: both hurt CHI, so a lane leg backing GB is untouched
        q = TL.nfl_leg(label="GB -3.5", side="sprdFav", prob=0.54, dec=1.909, tierN=2, nflLane=True, legacyTierN=2)
        d = TL.L.assess_leg(q, TL.ctx(TL.nfl_routes(TL.NFL_SUM), TL.nfl_model()), TL.NFL_NOW)
        self.assertEqual(d["action"], "none")


class Evidence(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.games = B.build_games(B.load_archive())

    def test_archive_is_there_and_complete(self):
        self.assertGreaterEqual(len(self.games), 60)
        self.assertTrue(all(g["spread"] is not None and g["ou"] is not None for g in self.games))
        self.assertTrue(all(g["st"] > 0 for g in self.games))

    def test_ratings_are_as_of_with_no_lookahead(self):
        rows = [
            {"id": "a", "week": 1, "date": "2026-09-10T00:00Z", "home": "AAA", "away": "BBB", "neutral": False, "homeScore": 30, "awayScore": 10, "spread": -3, "overUnder": 44},
            {"id": "b", "week": 2, "date": "2026-09-17T00:00Z", "home": "BBB", "away": "CCC", "neutral": False, "homeScore": 20, "awayScore": 17, "spread": -1, "overUnder": 40},
            {"id": "c", "week": 3, "date": "2026-09-24T00:00Z", "home": "AAA", "away": "BBB", "neutral": False, "homeScore": 13, "awayScore": 27, "spread": -6, "overUnder": 46},
        ]
        g = {x["id"]: x for x in B.build_games(rows)}
        self.assertIsNone(g["a"]["R"])                                      # first game: nobody has a rating
        self.assertEqual(g["b"]["home_n"], 1)                               # BBB has one earlier game (a), CCC none
        self.assertEqual(g["b"]["away_n"], 0)
        self.assertIsNone(g["b"]["R"])
        self.assertEqual(g["c"]["home_n"], 1)                               # AAA: game a only (never its own result)
        self.assertEqual(g["c"]["away_n"], 2)                               # BBB: games a and b
        self.assertAlmostEqual(g["c"]["home_dpg"], 20.0)
        self.assertAlmostEqual(g["c"]["away_dpg"], (-20 + 3) / 2)
        self.assertAlmostEqual(g["c"]["R"], 20.0 - (-8.5) + B.HFA)
        self.assertAlmostEqual(g["c"]["Tm"], (40 + (40 + 37) / 2) / 2)

    def test_the_findings_the_constants_rest_on(self):
        rated = [g for g in self.games if g["R"] is not None]
        self.assertGreaterEqual(len(rated), 45)
        # 1) the model's disagreement with the line carries no usable information: the slope's bootstrap interval reaches 0 and is nowhere near 1
        xs, ys = [g["R"] - g["M"] for g in rated], [g["y"] - g["M"] for g in rated]
        lo, hi = B.boot_ci(list(zip(xs, ys)), lambda b: B.slope([p[0] for p in b], [p[1] for p in b]))
        self.assertLess(lo, 0.05)
        self.assertLess(hi, 0.35)
        self.assertLess(B.slope(xs, ys), 0.2)
        # the anchor weight at the games played stays below the interval's upper end for the weeks the data cover
        self.assertLessEqual(NE.anchor_weight("margin", 4), hi + 0.01)
        # 2) the total's sd around the line is above the old sigma and brackets the new one
        errT = [g["tot"] - g["ou"] for g in self.games]
        import statistics as st
        tlo, thi = B.boot_ci(errT, st.pstdev)
        self.assertGreater(st.pstdev(errT), B.OLD_SIGMA_TOTAL + 2)
        self.assertLessEqual(tlo, NE.NFL_SIGMA_TOTAL)
        self.assertGreaterEqual(thi, NE.NFL_SIGMA_TOTAL)
        # 3) the anchored cover probabilities score at least as well as the old raw ones, by a margin the bootstrap interval excludes zero for
        out = []
        res = B.anchor_report(self.games, out=out.append)
        self.assertGreater(res["ll_gain_spread"][1], 0)
        self.assertGreater(res["ll_gain_total"][1], 0)

    def test_the_cushion_curve_is_close_to_the_alt_line_config(self):
        res = B.lane_report(self.games, out=lambda *_: None)
        ws, ns, wt, nt = res[6]
        self.assertGreater(ws / ns, 0.62)                                   # a 6-point spread cushion covers at least ~the 67% the lane prices it at (within the sample's noise)
        self.assertGreater(wt / nt, 0.55)
        self.assertLess(abs(wt / nt - 0.678), 0.09)

    def test_the_ledger_diagnosis_runs(self):
        out = []
        res = B.ledger_report(self.games, out=out.append)
        self.assertGreaterEqual(res["n"], 100)
        w, n = res["posted_nonml"]
        self.assertGreaterEqual(n, 40)
        self.assertLess(w / n, 0.60)                                       # the posted-line spread/O-U picks that dragged the record down
        cw, cn = res["posted_nonml_at_cushion6"]
        self.assertGreater(cw / cn, w / n + 0.05)                          # the same sides at a +6 cushion do clearly better


if __name__ == "__main__":
    unittest.main(verbosity=2)
