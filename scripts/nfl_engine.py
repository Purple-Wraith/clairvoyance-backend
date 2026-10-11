"""NFL engine constants and pure functions -- the Python mirror of the NFL market anchor in docs/app.html (nflMC / _nflGameCard2) plus the NFL alt-line lane the lock pipeline uses.

WHY (2026-10-11, owner: "nfl engine has lowest win % by a good amount ... improve nfl engine"; evidence tool: scripts/backtest_nfl_engine.py, which re-derives every number below)
    * The NFL spread / O-U probability came from the RAW model margin (each team's season point differential per game + home field + capped terms) and the raw model total, with no
      shrinkage toward the posted line.  Against the 49 completed 2026 games that had a rating (weeks 2-5), the model's disagreement with the closing spread had a standard deviation of 15.6
      points, its error against the final margin was 20.7 (the market's: 11.9), and the slope of (final margin - spread) on (model margin - spread) was -0.10 (bootstrap 95% CI -0.31..+0.11):
      no usable information beyond the line.  The model's side covered 47.8% and its over/under lean hit 34.7% (Wilson 23-49%).  The stated 70-93% cover probabilities were noise.
    * _NFL_SIGMA_TOTAL was 10.0; the final total misses the posted total by an sd of 13.6 (bootstrap 10.7-16.3), and ALT_LINE_CFG already used 13.0 for NFL totals.
    * The surviving NFL spread / O-U product is the alternate-line cushion (a +6 point cushion covers ~68-71% of closing lines whichever side is taken), so it is kept as an explicit LANE: a spread /
      O-U leg whose anchored lean is on its side (>= NFL_LANE_MIN_P) is locked ONLY at its alternate line, never at the posted line, and its stated probability is the empirical cushion curve's.

THE RULES (keep identical to docs/app.html: NFL_MKT_ANCHOR / _nflAnchorW / _NFL_SIGMA_*; scripts/test_nfl_engine_anchor.py runs the JS and this module on a grid and fails if they disagree)
    w(kind, n)        = NFL_MKT_ANCHOR[kind].max * n / (n + NFL_MKT_ANCHOR[kind].half),  n = games played by the LESSER team (1.0 when the anchor is switched off)
    anchored margin   = market margin + w_margin * (model margin - market margin)
    anchored total    = market total  + w_total  * (model total  - market total)
    cover probability = P(normal(mean = anchored value, sd = sigma) beats the line)
The moneyline is not anchored (owner: it is 14-3).
"""
from __future__ import annotations

import math

# ── the constants (mirror docs/app.html) ─────────────────────────────────────────────────────────────────────────────────────────────────────────────
NFL_SIGMA_MARGIN = 13.5
NFL_SIGMA_TOTAL = 13.0
NFL_MKT_ANCHOR = {"on": True, "margin": {"max": 0.30, "half": 8}, "total": {"max": 0.10, "half": 8}}

# ── the lock-pipeline lane (Python only: the card shows the honest probability, the pipeline decides what is locked) ────────────────────────────────────
NFL_ALT_LANE = True          # False: NFL spread / O-U legs qualify only through tier() (with the anchor on that is almost never) -- i.e. NFL becomes moneyline-only
NFL_LANE_MIN_LEGACY_TIER = 2  # SELECTOR, deliberately unchanged: a spread / O-U leg the OLD raw-model funnel graded OPTIMAL+ (card field legacyTierN) is still sent to its alternate line. The audit found no
                              # evidence this selector has skill (the model's side covered 47.8%), but also none for a better one, and changing it would change which games' moneylines are crowded out.
NFL_LANE_MARKETS = ("SPREAD", "OU")


def _cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def anchor_weight(kind: str, n_games, cfg: dict | None = None) -> float:
    """Weight kept on the model's disagreement with the posted line ('margin' | 'total') after n_games games by the lesser team. 1.0 when the anchor is switched off."""
    cfg = NFL_MKT_ANCHOR if cfg is None else cfg
    if not cfg.get("on"):
        return 1.0
    c = cfg.get(kind)
    if not c:
        return 1.0
    n = max(0.0, float(n_games or 0))
    return c["max"] * n / (n + c["half"])


def anchored(model_value: float, market_value: float | None, w: float) -> float:
    """market + w * (model - market); the model value alone when there is no posted line to anchor to."""
    if market_value is None:
        return model_value
    return market_value + w * (model_value - market_value)


def cover_prob(mean: float, sigma: float, line: float) -> float:
    """P(outcome > line) for a normal(mean, sigma) -- docs/app.html _cfbCoverProb."""
    return 1.0 - _cdf((line - mean) / sigma)


def clamp_p(p: float) -> float:
    return min(0.95, max(0.05, p))


def spread_cover(model_margin: float, market_margin: float, n_games, anchor: bool = True, sigma: float = NFL_SIGMA_MARGIN) -> float:
    """P(HOME covers the posted spread) from the (anchored) model margin; market_margin is the home margin the line implies (= -spread)."""
    mean = anchored(model_margin, market_margin, anchor_weight("margin", n_games)) if anchor else model_margin
    return clamp_p(cover_prob(mean, sigma, market_margin))


def over_prob(model_total: float, market_total: float, n_games, anchor: bool = True, sigma: float = NFL_SIGMA_TOTAL) -> float:
    """P(OVER the posted total) from the (anchored) model total."""
    mean = anchored(model_total, market_total, anchor_weight("total", n_games)) if anchor else model_total
    return clamp_p(cover_prob(mean, sigma, market_total))


def lane_qualifies(sport: str, mkt: str, legacy_tier) -> bool:
    """A NFL spread / O-U leg that qualifies for the alternate-line lane: the card graded it OPTIMAL+ on the OLD raw-model path (legacyTierN >= NFL_LANE_MIN_LEGACY_TIER). Legs from a card that did not
    anchor (no posted line / anchor switched off) carry no legacyTierN and qualify only through tier(), exactly as before."""
    return bool(NFL_ALT_LANE and sport == "NFL" and mkt in NFL_LANE_MARKETS and legacy_tier is not None and int(legacy_tier) >= NFL_LANE_MIN_LEGACY_TIER)
