#!/usr/bin/env python3
"""
send_demo_emails.py — sends one demo locks email per sport product,
using realistic synthetic sample picks rather than live game data, so
every product shows a populated example regardless of today's real
slate (several leagues are off-season on any given day). Reuses the
exact same HTML-building/sending path (build_locks_email_html() /
send_locks_email() in auto_lock_settle.py) real subscriber emails go
through -- the only thing "fake" here is the picks themselves, not the
rendering or send mechanism, so this is a true preview of the real
product, brand header included.

Usage:
  python3 scripts/send_demo_emails.py [--to email@example.com]
"""
from __future__ import annotations
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from auto_lock_settle import send_locks_email, PRODUCT_LABEL  # noqa: E402
from _subscribers import OWNER_EMAIL  # noqa: E402


def _tier(prob: float, ev: float) -> int:
    """Exact port of app.html's tier(p,e,mktType) grading thresholds
    (calibration adjustment omitted -- that only applies to live app
    state, not static demo data). Computing this from prob/EV instead of
    hand-typing a tier number is deliberate: a hand-typed tier silently
    drifted out of sync with its own prob/EV on 3 of the 8 original demo
    legs (NFL tagged PREMIUM at 64% win prob -- PREMIUM needs >=67%;
    soccer and tennis tagged OPTIMAL below the 62% OPTIMAL floor) --
    exactly the kind of mismatch this function makes impossible."""
    if prob >= 0.67 and ev >= 0.05:
        return 3
    if prob >= 0.62 and ev >= 0.03:
        return 2
    if prob >= 0.55 and ev >= 0.01:
        return 1
    return 0


def _leg(sport, hA, awA, side, label, prob, ml, dec, ev, mc_summary):
    tier_n = _tier(prob, ev)
    return {
        "kind": "GAME", "sport": sport, "hA": hA, "awA": awA, "side": side, "label": label,
        "prob": prob, "ml": ml, "dec": dec, "tierN": tier_n, "evVal": ev,
        "mcSummary": mc_summary, "best": {"label": label, "tierN": tier_n},
    }


# 2-3 sample matchups per product (a mix of PREMIUM/OPTIMAL/LEAN, like a
# real slate would produce) mirroring exactly what build_qualifying()
# would hand send_locks_email() from a real run -- same field names,
# same mcSummary formats each sport's real _autoLockCapture() call
# actually produces. Grade tags are computed by _leg() from prob/ev,
# not hand-typed -- see _tier()'s docstring.
DEMO_LEGS = {
    "nfl": [
        _leg("NFL", "KC", "BUF", "mlFav", "KC ML", 0.64, "-145", 1.69, 0.081,
             "MC PROJ TOTAL: 47.5 · KC projected -3.5"),
        _leg("NFL", "SF", "DAL", "mlFav", "SF ML", 0.71, "-190", 1.53, 0.062,
             "MC PROJ TOTAL: 44.0 · SF projected -6.0"),
        _leg("NFL", "PHI", "GB", "under", "UNDER 43.5", 0.59, "-110", 1.91, 0.021,
             "MC PROJ TOTAL: 40.5 · PHI projected -2.5"),
    ],
    "cfb": [
        _leg("CFB", "Georgia", "Alabama", "sprdFav", "Georgia -3.5", 0.63, "-110", 1.91, 0.045,
             "MC PROJ TOTAL: 52.0 · Georgia projected -4.0"),
        _leg("CFB", "Ohio State", "Michigan", "mlFav", "Ohio State ML", 0.69, "-165", 1.61, 0.062,
             "MC PROJ TOTAL: 46.0 · Ohio State projected -5.5"),
    ],
    "nba": [
        _leg("NBA", "BOS", "MIA", "mlFav", "BOS ML", 0.76, "-220", 1.45, 0.058,
             "MC PROJ: MIA 106.2 – BOS 116.8 (Total 223.0, 25k sims)"),
        _leg("NBA", "DEN", "LAL", "over", "OVER 228.5", 0.64, "-110", 1.91, 0.038,
             "MC PROJ: LAL 114.0 – DEN 118.5 (Total 232.5, 25k sims)"),
        _leg("NBA", "PHX", "OKC", "sprdDog", "PHX +5.5", 0.57, "-105", 1.95, 0.018,
             "MC PROJ: PHX 109.0 – OKC 114.0 (Total 223.0, 25k sims)"),
    ],
    "hockey": [
        _leg("NHL", "TOR", "MTL", "mlFav", "TOR ML", 0.62, "-135", 1.74, 0.048,
             "MC PROJ: MTL 2.6 – TOR 3.3 (Total 5.9, 25k sims)"),
        _leg("NHL", "EDM", "COL", "over", "OVER 6.5", 0.67, "-115", 1.87, 0.053,
             "MC PROJ: COL 3.5 – EDM 3.8 (Total 7.3, 25k sims)"),
    ],
    "soccer": [
        _leg("SOC_PL", "Arsenal", "Chelsea", "mlFav", "Arsenal ML", 0.61, "+105", 2.05, 0.251,
             "xG PROJ: Arsenal 1.85 – Chelsea 1.10"),
        _leg("SOC_LIGA", "Real Madrid", "Barcelona", "mlFav", "Real Madrid ML", 0.68, "-120", 1.83, 0.060,
             "xG PROJ: Real Madrid 2.05 – Barcelona 1.40"),
    ],
}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--to", default=None, help="Override recipient (defaults to OWNER_EMAIL)")
    args = ap.parse_args()
    recipient = args.to or OWNER_EMAIL
    if not recipient:
        raise SystemExit("No recipient -- pass --to or set LOCKS_EMAIL_TO/SOCIAL_CARD_EMAIL_TO")

    for product, legs in DEMO_LEGS.items():
        label = f"{PRODUCT_LABEL[product]} (DEMO)"
        send_locks_email(legs, live=False, label=label, to=[recipient])
        print(f"sent demo email for {product} -> {recipient}")


if __name__ == "__main__":
    main()
