#!/usr/bin/env python3
"""Non-moneyline confidence ceiling -- the Python mirror of MARKET_P_CEILING in docs/app.html, plus the evidence tool that re-derives it.

WHY (2026-10-10, owner decisions: fix the CFB over/under miscalibration + a generic ceiling for spread / over-under / prop legs)
    The stated win probability of a spread / O-U / prop leg carries almost no information above ~.62-.68: on the settled ledger the picks stated at 85-95%
    win ~53% (CFB O/U: 55% at n=65; NFL spread 38% at n=8). The old repair -- the per-sport, per-probability-band shift in renderOverall (cal_adj_by_sport) -- is
    pooled across moneyline + spread + O/U for the sport (CFB moneylines win ~90% and drag the 75%+ band UP), is scaled x0.60 for O/U / x0.75 for spread, and is capped at
    +/-12 points, so it can never repair a 25-35 point error (CFB O/U 75%+: shift -0.074 x 0.60 = -4.4 points).  A ceiling needs no learning and cannot saturate.

THE RULE (clamp, not a curve): p' = clamp(p, 1-c, c) for SPREAD / OU / PROP, c from the table below; moneyline has no ceiling.  The JS (docs/app.html) is the source of truth
    that actually prices the legs; THIS FILE is a mirror used by the tests, the evidence tool and the impact report.  scripts/test_market_ceiling.py runs the JS and this module on a
    grid and fails if they ever disagree.  Retune in BOTH places (the test tells you if you forgot one).

EVIDENCE TOOL
    python3 scripts/market_ceiling.py --evidence     re-derives the table: bands, Wilson rule per group, out-of-sample Brier before/after
    python3 scripts/market_ceiling.py --impact       which settled picks would change stated p / tier, and which pending picks
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import lock_timing as lt  # noqa: E402

# ── THE TABLE (keep identical to MARKET_P_CEILING in docs/app.html) ─────────────────────────────────────────────────────────────────────────────────
MARKET_P_CEILING = {
    "default": {"SPREAD": 0.68, "OU": 0.68, "PROP": 0.68},
    "byLeague": {
        "CFB": {"SPREAD": 0.62, "OU": 0.62},
        "NFL": {"SPREAD": 0.62, "OU": 0.62, "PROP": 0.62},
    },
}
# A ceiling below the OPTIMAL probability floor would stop every spread / O-U of that league from qualifying (tier() needs p >= .62 for OPTIMAL).
OPTIMAL_P_FLOOR = 0.62


def ceiling_for(league, mkt):
    """Ceiling for a league tag ('CFB', 'SOC_PL', ...) and market ('SPREAD'|'OU'|'PROP'|'PL'|'RL'|'ML'); None = no ceiling (moneyline / unknown)."""
    lg = str(league or "").upper()
    if lg.startswith("SOC_"):
        lg = lg[4:]
    k = "SPREAD" if mkt in ("PL", "RL") else mkt
    o = MARKET_P_CEILING["byLeague"].get(lg)
    if o and o.get(k) is not None:
        return o[k]
    return MARKET_P_CEILING["default"].get(k)


def cap_prob(p, mkt, league=None):
    """clamp(p, 1-c, c): symmetric so the two sides of one market still sum to 1. Moneyline / out-of-range probabilities pass through."""
    c = ceiling_for(league, mkt)
    if c is None or not (0 < p < 1):
        return p
    return max(1 - c, min(c, p))


def rescale_ev(ev_val, p_old, p_new):
    """EV at the SAME odds after the probability moves p_old -> p_new: e' = p'(e+1)/p - 1 (the identity tier() uses)."""
    return p_new * (ev_val + 1) / p_old - 1


# ── tier mirror (docs/app.html tier(): .55/.62/.67 + EV .01/.03/.05; hockey rows use the same floors on the real price) ───────────────────────────────
def tier_n(p, ev_val):
    return 3 if (p >= .67 and ev_val >= .05) else 2 if (p >= .62 and ev_val >= .03) else 1 if (p >= .55 and ev_val >= .01) else 0


def prop_tier_n(p):
    """Props are graded on the hit probability alone (_propGradeByConf in docs/app.html: >=67 PREMIUM, >=62 OPTIMAL, >=55 LEAN)."""
    return 3 if p >= .67 else 2 if p >= .62 else 1 if p >= .55 else 0


def _market_of(pick):
    bt = str(pick.get("betType") or "").upper()
    return {"RL": "SPREAD", "PL": "SPREAD"}.get(bt, bt)


# ── ledger ─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
ACTIVE = {"NBA", "NHL", "SHL", "LIIGA", "NLA", "EXTRALIGA", "NFL", "CFB", "PL", "LIGA", "CL", "SERIEA"}


def load_ledger(root: Path = ROOT, include_purged: bool = True):
    """Settled (win/loss), non-parlay picks with a stated probability, as plain dicts; known-late locks are tagged t='during'/'after' (excluded by the callers).
    Includes the 95 purged NFL props (data/purged) because they are the evidence for the NFL prop ceiling."""
    idx = lt.load_index(root)
    picks = json.loads((root / "docs" / "picks_backup.json").read_text())
    if include_purged:
        for f in sorted((root / "data" / "purged").glob("nfl_props_*.json")):
            for r in json.loads(f.read_text()).get("rows", []):
                raw = dict(r["raw"])
                raw["_purged"] = True
                picks.append(raw)
    out = []
    for p in picks:
        if p.get("outcome") not in ("win", "loss") or p.get("winProb") is None or lt.is_parlay(p):
            continue
        mkt = _market_of(p)
        if mkt not in ("ML", "SPREAD", "OU", "PROP"):
            continue
        try:
            wp = float(p["winProb"])
        except (TypeError, ValueError):
            continue
        out.append({"id": p["id"], "date": p.get("date"), "lg": lt.norm_sport(p), "mkt": mkt, "p": wp, "y": 1 if p["outcome"] == "win" else 0,
                    "dec": _f(p.get("decOdds")), "alt": bool(p.get("altLine")), "ps": p.get("priceSource"), "mp": _f(p.get("marketProb")),
                    "t": lt.classify(p, idx), "bet": p.get("betOn")})
    return out


def _f(x):
    try:
        return float(x) if x not in (None, "") else None
    except (TypeError, ValueError):
        return None


def clean_nonml(rows):
    """The evidence set: settled non-moneyline picks in an active league, pre-start locks (known-late excluded), alt-line picks excluded (their stated p is a curve estimate)."""
    return [r for r in rows if r["lg"] in ACTIVE and r["mkt"] != "ML" and r["t"] not in lt.LATE_CLASSES and not r["alt"]]


# ── statistics ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
def wilson(k, n, z=1.645):
    """(lower, upper) Wilson score bounds (z=1.645: one-sided 95%)."""
    if n == 0:
        return 0.0, 1.0
    ph = k / n
    d = 1 + z * z / n
    c = ph + z * z / (2 * n)
    s = z * math.sqrt(ph * (1 - ph) / n + z * z / (4 * n * n))
    return (c - s) / d, (c + s) / d


def supported_ceiling(rows, lo=0.55, hi=0.90, min_n=8):
    """THE RULE: the largest c on a .01 grid such that the one-sided 95% Wilson UPPER bound of the realised win rate of the picks stated at >= c is still >= c
    (the data cannot reject 'the picks above c win at c'). Returns (c or None, [(c, n, win, upper)])."""
    table, best = [], None
    for ci in range(int(lo * 100), int(hi * 100) + 1):
        c = ci / 100
        sub = [r for r in rows if r["p"] >= c]
        if len(sub) < min_n:
            break
        k = sum(r["y"] for r in sub)
        _, up = wilson(k, len(sub))
        table.append((c, len(sub), k / len(sub), up))
        if up >= c:
            best = c
    return best, table


def brier(rows, fn=lambda p: p):
    return sum((fn(r["p"]) - r["y"]) ** 2 for r in rows) / len(rows)


def logloss(rows, fn=lambda p: p):
    s = 0.0
    for r in rows:
        q = min(max(fn(r["p"]), .02), .98)
        s += -math.log(q) if r["y"] else -math.log(1 - q)
    return s / len(rows)


BANDS = [(0, .55), (.55, .62), (.62, .67), (.67, .75), (.75, .80), (.80, .85), (.85, 1.01)]


def band_table(rows):
    out = []
    for lo, hi in BANDS:
        b = [r for r in rows if lo <= r["p"] < hi]
        if b:
            out.append((lo, min(hi, 1.0), len(b), sum(r["p"] for r in b) / len(b), sum(r["y"] for r in b) / len(b)))
    return out


def groups(rows):
    eh = {"SHL", "LIIGA", "NLA", "EXTRALIGA"}
    return {
        "CFB O/U": [r for r in rows if r["lg"] == "CFB" and r["mkt"] == "OU"],
        "CFB spread": [r for r in rows if r["lg"] == "CFB" and r["mkt"] == "SPREAD"],
        "NFL spread + O/U": [r for r in rows if r["lg"] == "NFL" and r["mkt"] in ("SPREAD", "OU")],
        "football pooled (CFB+NFL spread/O-U)": [r for r in rows if r["lg"] in ("CFB", "NFL") and r["mkt"] in ("SPREAD", "OU")],
        "everything else (hockey, soccer, NBA props)": [r for r in rows if r["lg"] not in ("CFB", "NFL")],
        "  of which euro hockey": [r for r in rows if r["lg"] in eh],
        "ALL non-moneyline": rows,
    }


def _print_evidence():
    rows = clean_nonml(load_ledger())
    print(f"Evidence set: {len(rows)} settled non-moneyline picks (active leagues, pre-start locks, alt-line picks excluded)\n")
    for name, rs in groups(rows).items():
        if not rs:
            continue
        c, tab = supported_ceiling(rs)
        mp, wr = sum(r["p"] for r in rs) / len(rs), sum(r["y"] for r in rs) / len(rs)
        print(f"== {name}: n={len(rs)} mean stated {mp:.3f} realised {wr:.3f} -> supported ceiling (Wilson rule) = {c}")
        for lo, hi, n, mpb, w in band_table(rs):
            print(f"     stated [{lo:.2f},{hi:.2f}) n={n:3d} mean {mpb:.3f} won {w:.3f}")
    print("\nbands above are the evidence; the table in MARKET_P_CEILING rounds football to .62 (the OPTIMAL floor) and everything else to .68")


def _pending(root: Path = ROOT):
    return [p for p in json.loads((root / "docs" / "picks_backup.json").read_text()) if p.get("outcome") == "pending"]


def _print_impact():
    rows = load_ledger()
    ch = {"n": 0, "tier": 0, "stops": 0}
    by = {}
    for r in rows:
        if r["lg"] not in ACTIVE or r["mkt"] == "ML" or r["alt"] or r["mp"] is not None:
            continue
        c = cap_prob(r["p"], r["mkt"], r["lg"])
        if c >= r["p"]:
            continue
        d = r["dec"] or 1.909
        e0, e1 = r["p"] * d - 1, c * d - 1
        t0, t1 = (prop_tier_n(r["p"]), prop_tier_n(c)) if r["mkt"] == "PROP" else (tier_n(r["p"], e0), tier_n(c, e1))
        k = (r["lg"], r["mkt"])
        b = by.setdefault(k, [0, 0, 0])
        b[0] += 1
        b[1] += t0 != t1
        b[2] += (t0 >= 2) and (t1 < 2)
    for k, (n, t, s) in sorted(by.items()):
        print(f"{k[0]:10s} {k[1]:7s} settled picks whose stated p drops: {n:3d}  tier changes: {t:3d}  stop qualifying: {s:3d}")
    print("\npending picks:")
    for p in _pending():
        mkt = _market_of(p)
        if mkt == "ML" or p.get("altLine") or p.get("marketProb") is not None:
            continue
        lg = lt.norm_sport(p)
        wp = _f(p.get("winProb"))
        if wp is None:
            continue
        c = cap_prob(wp, mkt, lg)
        if c < wp:
            print(f"  {lg} {mkt} {p.get('betOn')}  stated {wp:.3f} -> {c:.3f}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--evidence", action="store_true")
    ap.add_argument("--impact", action="store_true")
    a = ap.parse_args()
    if a.evidence:
        _print_evidence()
    elif a.impact:
        _print_impact()
    else:
        ap.print_help()
