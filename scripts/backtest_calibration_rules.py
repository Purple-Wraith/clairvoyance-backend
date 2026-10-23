#!/usr/bin/env python3
"""WALK-FORWARD backtest of probability-calibration rules (owner request 2026-10-23: "run the backtest first").

QUESTION  Every league's stated probabilities look compressed the wrong way (mid bands win MORE than stated, top bands win LESS).  The adaptive cycle learns a per-league, per-band correction
          from settled picks, but trusts a band after only 5 picks.  Would a better-behaved rule have predicted FUTURE results better?

METHOD    Settled, pre-start, active-league picks (the same basis as scripts/calibration_watch.py: late manual locks out) are replayed in time order.  For each test pick the rule is
          trained ONLY on picks that had already settled BEFORE that pick was locked (no look-ahead) and the corrected probability is scored against the real outcome.
          Scored: Brier, log loss, calibration error (ECE), and a paired bootstrap of the per-pick Brier difference against "no correction" and against today's rule.
          Test window: picks locked on/after --start (the current engine); training reaches back over the whole history.

RULES     R0  none (the stated probability, as locked)
          R1  today's rule: per league x 5-point band, trusted at n>=5, learning rate .2 (n<10) / .4 (n>=10), only if the gap is >= 4 pts, capped +-12 pts
          R2  same cells, shrunk toward zero by n/(n+40)                    (no gate)
          R3  one pooled correction per band for ALL leagues, shrunk n/(n+40)
          R4  per-league band gap, partially pooled toward the pooled band gap (k=30)
          R5  global logistic recalibration p' = sigmoid(a*logit(p)+b), ridge toward (1,0)
          R6  per-league logistic recalibration, ridge toward the global fit

CAVEATS   * The ledger stores only the FINAL stated probability, which already includes whatever correction was active at lock time (and the market-ceiling cap).  So this measures whether
            ADDITIONAL learnable miscalibration remains in what was actually stated -- the question that matters -- not a replay of the raw model.
          * Alt-line picks and props are excluded by default: their stated probability comes from a side-neutral table / a normal approximation, not from the model's bands (use --include-alt).
          * R1 is today's rule WITHOUT its decay memory (it re-derives from scratch each time), so it is an approximation of the live state.

    python3 scripts/backtest_calibration_rules.py [--start 2026-09-01] [--include-alt] [--boot 2000] [--seed 7]
"""
from __future__ import annotations

import argparse
import bisect
import json
import math
import random
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
import calibration_watch as cw  # noqa: E402
import lock_timing as lt  # noqa: E402

BANDS = [(.50, .55), (.55, .60), (.60, .65), (.65, .70), (.70, .75), (.75, 1.0)]
SOC = {"PL", "LIGA", "CL", "SERIEA"}
CLAMP = (0.02, 0.98)


def band_of(p: float):
    for i, (lo, hi) in enumerate(BANDS):
        if lo <= p < hi:
            return i
    return None


def grp(league: str) -> str:
    return "SOC" if league in SOC else league


def clip(p: float) -> float:
    return min(max(p, CLAMP[0]), CLAMP[1])


def logit(p: float) -> float:
    p = clip(p)
    return math.log(p / (1 - p))


def sigmoid(z: float) -> float:
    return 1 / (1 + math.exp(-z))


# ── data ──────────────────────────────────────────────────────────────────────────────────────────────────────────────────
def load_rows(include_alt: bool) -> list[dict]:
    picks = json.loads((ROOT / "docs" / "picks_backup.json").read_text())
    byid = {p.get("id"): p for p in picks}
    idx = lt.load_index(ROOT)
    rows = []
    for r in cw.eligible_picks(picks, idx):
        p = byid.get(r["id"]) or {}
        is_alt = bool(p.get("altLine"))
        is_prop = str(p.get("betType") or "").upper() == "PROP"
        if not include_alt and (is_alt or is_prop):
            continue
        lock = p.get("lockedAt")
        try:
            lock_ms = float(lock)
        except (TypeError, ValueError):
            lock_ms = datetime.fromisoformat(str(r["date"])).timestamp() * 1000 if r.get("date") else 0.0
        rows.append({"id": r["id"], "g": grp(r["league"]), "league": r["league"], "p": r["p"], "y": r["y"], "settle": r["t"], "lock": lock_ms, "date": str(r["date"]),
                     "market": r["market"], "alt": is_alt})
    rows.sort(key=lambda x: x["lock"])
    return rows


# ── rules ─────────────────────────────────────────────────────────────────────────────────────────────────────────────────
def cells(train: list[dict]):
    """counts per (group, band), per band and per (market, band): n, sum p, wins."""
    by_gb = defaultdict(lambda: [0, 0.0, 0])
    by_b = defaultdict(lambda: [0, 0.0, 0])
    by_mb = defaultdict(lambda: [0, 0.0, 0])
    for t in train:
        b = band_of(t["p"])
        if b is None:
            continue
        for d, k in ((by_gb, (t["g"], b)), (by_b, b), (by_mb, (t["market"], b))):
            c = d[k]
            c[0] += 1
            c[1] += t["p"]
            c[2] += t["y"]
    return by_gb, by_b, by_mb


def r0(p, g, b, by_gb, by_b, ctx):
    return p


def r1(p, g, b, by_gb, by_b, ctx):
    if b is None:
        return p
    n, sp, w = by_gb.get((g, b), (0, 0.0, 0))
    if n < 5:
        return p
    lo, hi = BANDS[b]
    act, exp = w / n, (lo + min(hi, .98)) / 2
    if abs(act - exp) < 0.04:
        return p
    shift = max(-0.12, min(0.12, (act - exp) * (0.40 if n >= 10 else 0.20)))
    return clip(p + shift)


def make_r1n(min_n):
    """today's rule with a larger minimum sample per cell (the only change)."""
    def r1n(p, g, b, by_gb, by_b, ctx):
        if b is None:
            return p
        n, sp, w = by_gb.get((g, b), (0, 0.0, 0))
        if n < min_n:
            return p
        lo, hi = BANDS[b]
        act, exp = w / n, (lo + min(hi, .98)) / 2
        if abs(act - exp) < 0.04:
            return p
        return clip(p + max(-0.12, min(0.12, (act - exp) * (0.40 if n >= 10 else 0.20))))
    return r1n


def make_r2(k=40):
    def r2(p, g, b, by_gb, by_b, ctx):
        if b is None:
            return p
        n, sp, w = by_gb.get((g, b), (0, 0.0, 0))
        if n < 1:
            return p
        return clip(p + (w - sp) / n * n / (n + k))
    return r2


def make_r3(k=40):
    def r3(p, g, b, by_gb, by_b, ctx):
        if b is None:
            return p
        n, sp, w = by_b.get(b, (0, 0.0, 0))
        if n < 1:
            return p
        return clip(p + (w - sp) / n * n / (n + k))
    return r3


def make_r7(k=40):
    """pooled band correction PER BET TYPE (moneyline / spread / total), shrunk n/(n+k)."""
    def r7(p, g, b, by_gb, by_b, ctx):
        if b is None:
            return p
        n, sp, w = ctx["by_mb"].get((ctx["market"], b), (0, 0.0, 0))
        if n < 1:
            return p
        return clip(p + (w - sp) / n * n / (n + k))
    return r7


def make_r4(k=30, k0=10):
    def r4(p, g, b, by_gb, by_b, ctx):
        if b is None:
            return p
        N, SP, W = by_b.get(b, (0, 0.0, 0))
        G = ((W - SP) / N * N / (N + k0)) if N else 0.0
        n, sp, w = by_gb.get((g, b), (0, 0.0, 0))
        gl = (w - sp) / n if n else 0.0
        return clip(p + (n * gl + k * G) / (n + k))
    return r4


def fit_platt(rows: list[dict], prior=(1.0, 0.0), lam=5.0, iters=25):
    """ridge-regularised logistic regression of y on logit(p) toward `prior`; Newton's method on 2 params."""
    a, b = prior
    xs = [(logit(r["p"]), r["y"]) for r in rows]
    for _ in range(iters):
        ga = gb = haa = hab = hbb = 0.0
        for x, y in xs:
            q = sigmoid(a * x + b)
            e = q - y
            w = q * (1 - q)
            ga += e * x; gb += e
            haa += w * x * x; hab += w * x; hbb += w
        ga += lam * (a - prior[0]); gb += lam * (b - prior[1])
        haa += lam; hbb += lam
        det = haa * hbb - hab * hab
        if abs(det) < 1e-12:
            break
        da = (hbb * ga - hab * gb) / det
        db = (-hab * ga + haa * gb) / det
        a -= da; b -= db
        if abs(da) + abs(db) < 1e-7:
            break
    return a, b


def make_r5(lam=5.0):
    def r5(p, g, b, by_gb, by_b, ctx):
        a, bb = ctx["global"]
        return clip(sigmoid(a * logit(p) + bb))
    return r5


def make_r6():
    def r6(p, g, b, by_gb, by_b, ctx):
        a, bb = ctx["league"].get(g, ctx["global"])
        return clip(sigmoid(a * logit(p) + bb))
    return r6


RULES = {"R0 none": r0, "R1 today's rule": r1, "R2 shrunk cells (k=40)": make_r2(40), "R3 pooled bands (k=40)": make_r3(40), "R4 partial pooling (k=30)": make_r4(30),
         "R5 logistic global": make_r5(), "R6 logistic per league": make_r6(), "R7 pooled bands per bet type": make_r7(40),
         "R8 today's rule, min n=20": make_r1n(20), "R9 today's rule, min n=40": make_r1n(40)}


# ── scoring ───────────────────────────────────────────────────────────────────────────────────────────────────────────────
def brier(p, y):
    return (p - y) ** 2


def logloss(p, y):
    p = clip(p)
    return -(math.log(p) if y else math.log(1 - p))


def ece(ps, ys):
    bins = defaultdict(list)
    for p, y in zip(ps, ys):
        bins[min(int(p * 20), 19)].append((p, y))
    tot = len(ps)
    return sum(len(v) / tot * abs(sum(x[0] for x in v) / len(v) - sum(x[1] for x in v) / len(v)) for v in bins.values()) * 100


def boot_ci(diffs, B, rnd):
    n = len(diffs)
    means = []
    for _ in range(B):
        s = 0.0
        for _ in range(n):
            s += diffs[rnd.randrange(n)]
        means.append(s / n)
    means.sort()
    return means[int(0.025 * B)], means[int(0.975 * B)], sum(1 for m in means if m < 0) / B


def run(rows, start, boot, seed):
    rnd = random.Random(seed)
    by_settle = sorted(rows, key=lambda r: r["settle"])
    settle_ts = [r["settle"] for r in by_settle]
    test = [r for r in rows if r["date"] >= start]
    print(f"picks: {len(rows)} eligible overall | test window {start}+: {len(test)} picks (training for each = everything settled before its lock)")
    # per-day platt fits (training = settled before the day's FIRST lock)
    day_fit = {}
    out = {name: [] for name in RULES}
    for r in test:
        k = bisect.bisect_left(settle_ts, r["lock"])
        train = by_settle[:k]
        if len(train) < 30:
            continue
        if r["date"] not in day_fit:
            ctx = {"global": fit_platt(train)}
            leagues = defaultdict(list)
            for t in train:
                leagues[t["g"]].append(t)
            ctx["league"] = {g: fit_platt(rs, prior=ctx["global"], lam=20.0) for g, rs in leagues.items() if len(rs) >= 10}
            day_fit[r["date"]] = ctx
        ctx = day_fit[r["date"]]
        by_gb, by_b, by_mb = cells(train)
        b = band_of(r["p"])
        ctx = dict(ctx, market=r["market"], by_mb=by_mb)
        for name, fn in RULES.items():
            out[name].append((fn(r["p"], r["g"], b, by_gb, by_b, ctx), r))
    n = len(out["R0 none"])
    print(f"scored: {n} picks\n")
    ys = [r["y"] for _, r in out["R0 none"]]
    base_b = [brier(p, r["y"]) for p, r in out["R0 none"]]
    r1_b = [brier(p, r["y"]) for p, r in out["R1 today's rule"]]
    print(f"{'rule':28} {'Brier':>8} {'LogLoss':>8} {'ECE pts':>8}  {'d Brier vs R0 (95% CI)':>34}  {'P(beats R0)':>11}  {'d vs R1':>10}")
    summary = {}
    for name in RULES:
        ps = [p for p, _ in out[name]]
        bs = [brier(p, y) for p, y in zip(ps, ys)]
        ll = sum(logloss(p, y) for p, y in zip(ps, ys)) / n
        d0 = [x - y for x, y in zip(bs, base_b)]
        lo, hi, pb = boot_ci(d0, boot, rnd) if name != "R0 none" else (0, 0, 0)
        d1 = sum(x - y for x, y in zip(bs, r1_b)) / n
        summary[name] = (sum(bs) / n, ll, ece(ps, ys), sum(d0) / n, lo, hi, pb, d1)
        print(f"{name:28} {sum(bs)/n:8.5f} {ll:8.5f} {ece(ps, ys):8.2f}  {sum(d0)/n:+10.5f} [{lo:+.5f},{hi:+.5f}]  {pb:11.2f}  {d1:+10.5f}")
    # reliability of the stated probabilities in the test window
    print("\nReliability of the STATED probability in the test window (what the rules are trying to fix)")
    print(f"{'band':>8} {'n':>5} {'stated':>8} {'actual':>8} {'gap pts':>8}")
    tb = defaultdict(list)
    for _, r in out["R0 none"]:
        tb[band_of(r["p"])].append(r)
    for b in sorted(k for k in tb if k is not None):
        v = tb[b]
        mp, wr = sum(x["p"] for x in v) / len(v), sum(x["y"] for x in v) / len(v)
        print(f"{BANDS[b][0]*100:3.0f}-{min(BANDS[b][1],1)*100:3.0f}% {len(v):5d} {mp*100:7.1f}% {wr*100:7.1f}% {(wr-mp)*100:+8.1f}")
    print("\nStated vs actual by BET TYPE x band in the test window (cells with 15+ picks)")
    print(f"{'type':7}{'band':>9}{'n':>5}{'stated':>8}{'actual':>8}{'gap':>7}")
    mb = defaultdict(list)
    for _, r in out["R0 none"]:
        mb[(r["market"], band_of(r["p"]))].append(r)
    for (m, bi) in sorted(mb, key=lambda k: (k[0], k[1] if k[1] is not None else -1)):
        v = mb[(m, bi)]
        if bi is None or len(v) < 15:
            continue
        mp, wr = sum(x["p"] for x in v) / len(v), sum(x["y"] for x in v) / len(v)
        print(f"{m:7}{BANDS[bi][0]*100:4.0f}-{min(BANDS[bi][1],1)*100:3.0f}%{len(v):5d}{mp*100:7.1f}%{wr*100:7.1f}%{(wr-mp)*100:+7.1f}")
    # by league: Brier delta of every rule vs R0
    print("\nBrier change vs R0 by league (negative = better; n = test picks)")
    leagues = sorted({r["g"] for _, r in out["R0 none"]})
    names = [x for x in RULES if x != "R0 none"]
    print(f"{'league':10} {'n':>5} " + " ".join(f"{x.split()[0]:>7}" for x in names))
    for g in leagues:
        ix = [i for i, (_, r) in enumerate(out["R0 none"]) if r["g"] == g]
        if len(ix) < 20:
            continue
        row = []
        for name in names:
            d = sum(brier(out[name][i][0], ys[i]) - base_b[i] for i in ix) / len(ix)
            row.append(f"{d:+7.4f}")
        print(f"{g:10} {len(ix):5d} " + " ".join(row))
    # stability: first half vs second half of the test window
    half = n // 2
    print("\nStability (Brier change vs R0): first half of the window | second half")
    for name in names:
        bs = [brier(p, y) for (p, _), y in zip(out[name], ys)]
        d = [x - y for x, y in zip(bs, base_b)]
        print(f"{name:28} {sum(d[:half])/max(half,1):+9.5f} | {sum(d[half:])/max(n-half,1):+9.5f}")
    return summary


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2026-09-01")
    ap.add_argument("--include-alt", action="store_true")
    ap.add_argument("--boot", type=int, default=2000)
    ap.add_argument("--seed", type=int, default=7)
    a = ap.parse_args()
    rows = load_rows(a.include_alt)
    run(rows, a.start, a.boot, a.seed)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
