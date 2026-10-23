#!/usr/bin/env python3
"""Sanity tests for scripts/backtest_calibration_rules.py (pure functions only; no ledger needed)."""
import math, sys, unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import backtest_calibration_rules as bt  # noqa: E402


class Pure(unittest.TestCase):
    def test_bands(self):
        self.assertEqual(bt.band_of(0.50), 0)
        self.assertEqual(bt.band_of(0.5499), 0)
        self.assertEqual(bt.band_of(0.55), 1)
        self.assertEqual(bt.band_of(0.99), 5)
        self.assertIsNone(bt.band_of(0.49))

    def test_platt_recovers_a_known_miscalibration(self):
        # outcomes generated at p_true = sigmoid(0.6*logit(p) + 0.3): a fit should land near (0.6, 0.3), never near the identity
        import random
        rnd = random.Random(3)
        rows = []
        for _ in range(6000):
            p = rnd.uniform(0.52, 0.92)
            pt = bt.sigmoid(0.6 * bt.logit(p) + 0.3)
            rows.append({"p": p, "y": 1 if rnd.random() < pt else 0})
        a, b = bt.fit_platt(rows, lam=1.0)
        self.assertAlmostEqual(a, 0.6, delta=0.12)
        self.assertAlmostEqual(b, 0.3, delta=0.15)

    def test_a_calibrated_stream_is_left_alone_by_a_shrunk_rule(self):
        rule = bt.make_r3(40)
        by_b = {0: (2000, 2000 * 0.525, int(2000 * 0.525))}
        p = rule(0.525, "NHL", 0, {}, by_b, {})
        self.assertAlmostEqual(p, 0.525, places=3)

    def test_todays_rule_needs_five_picks_and_a_four_point_gap(self):
        r1 = bt.RULES["R1 today's rule"]
        self.assertEqual(r1(0.62, "NHL", 2, {("NHL", 2): (4, 2.5, 4)}, {}, {}), 0.62)          # n < 5: untouched
        self.assertEqual(r1(0.62, "NHL", 2, {("NHL", 2): (10, 6.25, 6)}, {}, {}), 0.62)        # 60% actual vs 62.5% midpoint: gap < 4 pts
        up = r1(0.62, "NHL", 2, {("NHL", 2): (10, 6.25, 9)}, {}, {})                           # 90% actual: moves up, capped at +12 pts
        self.assertGreater(up, 0.62)
        self.assertLessEqual(up - 0.62, 0.12 + 1e-9)

    def test_scores_and_ci_are_sane(self):
        self.assertAlmostEqual(bt.brier(0.7, 1), 0.09)
        self.assertGreater(bt.logloss(0.7, 0), bt.logloss(0.7, 1))
        self.assertAlmostEqual(bt.ece([0.6] * 10, [1] * 6 + [0] * 4), 0.0, places=6)


if __name__ == "__main__":
    unittest.main(verbosity=2)
