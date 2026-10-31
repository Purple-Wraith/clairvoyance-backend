#!/usr/bin/env python3
"""Backtest: soccer UNDER totals are overconfident (2026-10-31). Read-only; nothing here touches the ledger, the engine or Supabase.

QUESTION
    Soccer (PL / LIGA / CL / SERIEA) O/U picks: stated probability vs realised win rate, split by side, and which fix (if any) helps out of sample.

WHAT IT PRINTS
    1. Stated-probability buckets for UNDER legs, and the z-score of UNDER vs OVER legs stated at >= .68 (the current ceiling for a non-CFB/NFL O/U: MARKET_P_CEILING in docs/app.html).
    2. Candidate rules scored by Brier / log-loss on the settled ledger, with a date-cluster bootstrap CI of the change vs the rule as sold, plus leave-one-date-out for the fitted ceiling:
         R1 UNDER-only ceiling c       (overs untouched)
         R2 symmetric ceiling c        (what MARKET_P_CEILING does today, at a lower c)
         R3 UNDER ceiling at lines >= 3.5
         R4 UNDER shrink toward .5
    3. Projection check (picks whose reasoning text carries "Projected a-b goals"): projected vs actual total goals by side, and a Poisson re-price with the projected total shrunk toward a league mean.

EVIDENCE SET (same basis as every published figure): settled win/loss, pre-start locks only (known-late manual locks excluded), alt-line picks excluded (their stated p is a curve estimate), active soccer leagues.

    python3 scripts/backtest_soccer_unders.py
"""
from __future__ import annotations

import json
import math
import random
import re
import statistics as st
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import lock_timing as lt  # noqa: E402
import market_ceiling as mc  # noqa: E402

SOCCER = {"PL", "LIGA", "CL", "SERIEA"}
CEILING_NOW = 0.68      # MARKET_P_CEILING.default.OU (docs/app.html) -- the OVER ceiling; soccer UNDERs moved to .64 on 2026-10-31 (MARKET_P_UNDER_CEILING), so the "as sold" rows
                        # of picks settled after that date already carry it: re-run on the pre-change picks (before 2026-11-01) to reproduce the table in the memory note.


def load(root: Path = ROOT) -> list[dict]:
    picks = {p["id"]: p for p in json.loads((root / "docs" / "picks_backup.json").read_text())}
    out = []
    for r in mc.load_ledger(root, include_purged=False):
        if r["lg"] not in SOCCER or r["mkt"] != "OU" or r["alt"] or r["t"] in lt.LATE_CLASSES:
            continue
        p = picks[r["id"]]
        bo = str(p.get("betOn") or "").upper()
        m = re.search(r"(\d+\.\d)", bo)
        if not m:
            continue
        row = {"id": r["id"], "date": r["date"], "lg": r["lg"], "side": "U" if "UNDER" in bo else "O", "line": float(m.group(1)),
               "p": r["p"], "y": r["y"], "dec": r["dec"] or 1.91, "proj": None, "act": None}
        pr = re.search(r"Projected ([\d.]+)[–-]([\d.]+) goals", str(p.get("reasoning") or ""))
        try:
            row["act"] = int(p["hScore"]) + int(p["aScore"])
        except (KeyError, TypeError, ValueError):
            pass
        if pr and row["act"] is not None:
            row["proj"] = float(pr.group(1)) + float(pr.group(2))
        out.append(row)
    return out


def brier(rs, f):
    return sum((f(d) - d["y"]) ** 2 for d in rs) / len(rs)


def logloss(rs, f):
    return -sum(math.log(min(max(f(d) if d["y"] else 1 - f(d), 1e-4), 1 - 1e-4)) for d in rs) / len(rs)


def _cdf(lam: float, k: int) -> float:
    t = math.exp(-lam)
    s = t
    for i in range(1, k + 1):
        t *= lam / i
        s += t
    return s


def p_side(lam: float, d: dict) -> float:
    pu = _cdf(lam, int(math.floor(d["line"])))
    return pu if d["side"] == "U" else 1 - pu


def rules() -> list[tuple[str, callable]]:
    cap_u = lambda c: (lambda d: min(d["p"], c) if d["side"] == "U" else d["p"])
    cap_sym = lambda c: (lambda d: min(max(d["p"], 1 - c), c))
    cap_u_hi = lambda c: (lambda d: min(d["p"], c) if d["side"] == "U" and d["line"] >= 3.5 else d["p"])
    shrink_u = lambda k: (lambda d: .5 + (d["p"] - .5) * k if d["side"] == "U" else d["p"])
    out = [("R0 as sold", lambda d: d["p"])]
    out += [(f"R1 UNDER ceiling {c}", cap_u(c)) for c in (.66, .64, .62, .60)]
    out += [(f"R2 symmetric ceiling {c}", cap_sym(c)) for c in (.66, .64, .62)]
    out += [(f"R3 UNDER line>=3.5 ceiling {c}", cap_u_hi(c)) for c in (.64, .62)]
    out += [(f"R4 UNDER shrink x{k}", shrink_u(k)) for k in (.75, .5)]
    return out


def main() -> None:
    D = load()
    U = [d for d in D if d["side"] == "U"]
    O = [d for d in D if d["side"] == "O"]
    print(f"soccer O/U evidence set: n={len(D)}  (UNDER {len(U)}, OVER {len(O)})")

    print("\nUNDER legs by stated probability")
    for lo, hi, nm in [(0, .62, "< .62"), (.62, .6799, ".62-.68"), (.6799, .6801, "= .68 (ceiling bound)"), (.6801, 1.01, "> .68")]:
        b = [d for d in U if lo <= d["p"] < hi]
        if b:
            k = sum(d["y"] for d in b)
            lo_w, hi_w = mc.wilson(k, len(b))
            print(f"  {nm:<22} n={len(b):>3}  won {k:>3} ({100 * k / len(b):3.0f}%)  stated {100 * sum(d['p'] for d in b) / len(b):3.0f}%  wilson95 [{lo_w:.2f},{hi_w:.2f}]")
    for nm, rs in (("UNDER stated >= .68", [d for d in U if d["p"] >= CEILING_NOW - 1e-9]), ("OVER  stated >= .68", [d for d in O if d["p"] >= CEILING_NOW - 1e-9])):
        if rs:
            k, n = sum(d["y"] for d in rs), len(rs)
            p = sum(d["p"] for d in rs) / n
            print(f"  {nm}: n={n} won {k} ({100 * k / n:.0f}%) stated {100 * p:.0f}%  z={(k / n - p) / math.sqrt(p * (1 - p) / n):+.2f}")

    dates = sorted({d["date"] for d in D})
    by = defaultdict(list)
    for d in D:
        by[d["date"]].append(d)
    random.seed(7)
    cands = rules()
    f0 = cands[0][1]
    base = brier(D, f0)

    def boot(f, B=2000):
        diffs = []
        for _ in range(B):
            rs = [d for x in (random.choice(dates) for _ in dates) for d in by[x]]
            diffs.append(brier(rs, f) - brier(rs, f0))
        diffs.sort()
        return diffs[int(.025 * B)], diffs[int(.975 * B)]

    print(f"\n{'rule':<34}{'Brier':>8}{'change':>9}   95% CI of change (date bootstrap)   logloss")
    for nm, f in cands:
        lo, hi = (0.0, 0.0) if f is f0 else boot(f)
        print(f"{nm:<34}{brier(D, f):8.4f}{brier(D, f) - base:+9.4f}   [{lo:+.4f}, {hi:+.4f}]{'':>14}{logloss(D, f):.4f}")

    grid = [round(.56 + .01 * i, 2) for i in range(14)]
    cap_u = lambda c: (lambda d: min(d["p"], c) if d["side"] == "U" else d["p"])
    oos = sum(sum((cap_u(min(grid, key=lambda c: brier([d for d in D if d["date"] != x], cap_u(c))))(d) - d["y"]) ** 2 for d in by[x]) for x in dates)
    print(f"\nleave-one-date-out (UNDER ceiling fitted on the other dates): Brier {base:.4f} -> {oos / len(D):.4f}")

    print("\nunits if UNDER legs above a ceiling were instead SKIPPED (win/loss at the stored price) -- a ceiling does NOT skip picks, it lowers the stated p / grade")
    for c in (.68, .66, .64, .62):
        ex = [d for d in U if d["p"] >= c - 1e-9] if c == .68 else [d for d in U if d["p"] > c + 1e-9]
        k = sum(d["y"] for d in ex)
        print(f"  UNDER stated {'>=' if c == .68 else '>'} {c}: n={len(ex)} won {k} ({100 * k / max(len(ex), 1):.0f}%) units {sum((d['dec'] - 1) if d['y'] else -1 for d in ex):+.1f}")

    R = [d for d in D if d["proj"] is not None]
    if len(R) >= 20:
        print(f"\nPROJECTION CHECK (picks whose reasoning carries the projected total, n={len(R)})")
        for side, nm in (("U", "UNDER"), ("O", "OVER")):
            r = [d for d in R if d["side"] == side]
            if r:
                print(f"  {nm}: n={len(r)} projected {st.mean(d['proj'] for d in r):.2f} goals, actual {st.mean(d['act'] for d in r):.2f}, won {sum(d['y'] for d in r)}")
        print(f"  corr(projected, actual) = {st.correlation([d['proj'] for d in R], [d['act'] for d in R]):.2f}   |Poisson(proj) - stated| = {st.mean(abs(p_side(d['proj'], d) - d['p']) for d in R):.3f}")
        print(f"  Brier as sold {brier(R, lambda d: d['p']):.4f}; Poisson(projected total) with the total shrunk toward a league mean m:  lam = m + k*(proj - m)")
        for m in (2.9, 3.2):
            print(f"    m={m}: " + "  ".join(f"k={k}: {brier(R, lambda d: p_side(m + k * (d['proj'] - m), d)):.4f}" for k in (1, .75, .5, .25, 0)))


if __name__ == "__main__":
    main()
