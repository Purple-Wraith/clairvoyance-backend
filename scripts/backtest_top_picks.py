#!/usr/bin/env python3
"""WALK-FORWARD backtest of how the TOP PICKS tab should rank a day's picks (owner request 2026-10-23: "improve the top picks tab to have more accurate picks so we can increase win %").

QUESTION  The tab ranks a day's qualifying picks by their STATED (or adjusted-line) probability and shows the top 10 (at most 3 per league).  But stated probability is a poor guide at the top of
          the range (picks stated 75%+ win far less often than picks stated 65-70%).  Would ranking on what each KIND of pick has actually won produce a higher win rate in the top 10?

METHOD    For each test day, the day's settled game picks (the same pool the tab draws from: pre-start locks of the active leagues, alt-line picks included, props excluded) are ranked by each rule
          using ONLY picks that had settled before that day's first lock.  The top-N (league cap 3, exactly like the tab) is scored by how often it won.  Compared with the current rule by a
          paired bootstrap over DAYS (resampling days, so one big slate cannot decide the answer).

RULES     S0  stated probability (the tab today)
          S1  win rate of the pick's (bet type, alt-line or not) cell, shrunk toward the pooled rate        (k=30)
          S2  win rate of the (league, bet type, alt-line) cell, shrunk toward its (bet type, alt) rate   (k=30)
          S3  stated probability plus the pooled band correction (stated p moved toward what that band has won; k=40)
          S4  S2 and S3 averaged
          S5  random order (expected value = the overall win rate; the floor any rule has to beat)
          S6  S2 trained only on the last --recent-days days of results (default 21), so the pre-fix era cannot pollute the rates
          S7  stated probability plus a 4-point boost for adjusted-line picks (they have won more than their stated probability)
          S8  today's ranking, but a pick whose (league, bet type, alt) cell has a recent shrunk hit rate under 62% sinks to the bottom (a FILTER, not a re-rank)
          S9  the same filter at 65%

    python3 scripts/backtest_top_picks.py [--start 2026-10-04] [--boot 4000] [--seed 7] [--no-cap]
"""
from __future__ import annotations

import argparse
import bisect
import random
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import backtest_calibration_rules as bt  # noqa: E402

CAP = 3
K = 30


def load() -> list[dict]:
    rows = [r for r in bt.load_rows(True) if r["market"] != "PROP"]       # include alt-line picks: they are what the tab shows
    for r in rows:
        r["cell_m"] = (r["market"], r["alt"])
        r["cell_l"] = (r["g"], r["market"], r["alt"])
    return rows


class Stats:
    """training counts, built once per test day."""

    def __init__(self, train: list[dict]):
        self.n_all = len(train)
        self.w_all = sum(t["y"] for t in train)
        self.m = defaultdict(lambda: [0, 0])
        self.l = defaultdict(lambda: [0, 0])
        self.band = defaultdict(lambda: [0, 0.0, 0])
        for t in train:
            self.m[t["cell_m"]][0] += 1; self.m[t["cell_m"]][1] += t["y"]
            self.l[t["cell_l"]][0] += 1; self.l[t["cell_l"]][1] += t["y"]
            b = bt.band_of(t["p"])
            if b is not None:
                c = self.band[b]; c[0] += 1; c[1] += t["p"]; c[2] += t["y"]

    def pool(self) -> float:
        return self.w_all / self.n_all if self.n_all else 0.65

    def rate_m(self, r) -> float:
        n, w = self.m.get(r["cell_m"], (0, 0))
        return (w + K * self.pool()) / (n + K)

    def rate_l(self, r) -> float:
        n, w = self.l.get(r["cell_l"], (0, 0))
        prior = self.rate_m(r)
        return (w + K * prior) / (n + K)

    def band_adj(self, r) -> float:
        b = bt.band_of(r["p"])
        if b is None or b not in self.band:
            return r["p"]
        n, sp, w = self.band[b]
        return bt.clip(r["p"] + (w - sp) / n * n / (n + 40))


def rules(stats: Stats, recent: "Stats | None" = None):
    return {
        "S0 stated probability (today)": lambda r: r["p"],
        "S1 bet-type cell hit rate": stats.rate_m,
        "S2 league x bet-type cell hit rate": stats.rate_l,
        "S3 stated + band correction": stats.band_adj,
        "S4 average of S2 and S3": lambda r: (stats.rate_l(r) + stats.band_adj(r)) / 2,
        "S6 S2 on recent results only": (recent or stats).rate_l,
        "S7 stated + alt-line boost": lambda r: r["p"] + (0.04 if r["alt"] else 0.0),
        "S8 drop weak cells (<62%)": lambda r: r["p"] - (1.0 if (recent or stats).rate_l(r) < 0.62 else 0.0),
        "S9 drop weak cells (<65%)": lambda r: r["p"] - (1.0 if (recent or stats).rate_l(r) < 0.65 else 0.0),
    }


def pick_top(cands: list[dict], score, n: int, cap: bool) -> list[dict]:
    ordered = sorted(cands, key=lambda r: -score(r))
    if not cap:
        return ordered[:n]
    out, per, held = [], defaultdict(int), []
    for r in ordered:
        if len(out) >= n:
            break
        if per[r["g"]] >= CAP:
            held.append(r)
            continue
        per[r["g"]] += 1
        out.append(r)
    for r in held:
        if len(out) >= n:
            break
        out.append(r)
    return out


def run(rows, start, boot, seed, cap, recent_days=21):
    rnd = random.Random(seed)
    by_settle = sorted(rows, key=lambda r: r["settle"])
    settle_ts = [r["settle"] for r in by_settle]
    days = defaultdict(list)
    for r in rows:
        if r["date"] >= start:
            days[r["date"]].append(r)
    day_list = sorted(d for d in days if len(days[d]) >= 5)
    names = list(rules(Stats([])).keys()) + ["S5 random order"]
    res = {N: {nm: [] for nm in names} for N in (3, 5, 10)}      # per day: (wins, picks)
    for d in day_list:
        c = days[d]
        first_lock = min(r["lock"] for r in c)
        train = by_settle[:bisect.bisect_left(settle_ts, first_lock)]
        if len(train) < 50:
            continue
        st = Stats(train)
        cutoff_ms = first_lock - recent_days * 86400000
        st_recent = Stats([t for t in train if t["settle"] >= cutoff_ms])
        rl = rules(st, st_recent)
        base = sum(r["y"] for r in c) / len(c)
        for N in (3, 5, 10):
            for nm, fn in rl.items():
                top = pick_top(c, fn, N, cap)
                res[N][nm].append((sum(r["y"] for r in top), len(top)))
            k = min(N, len(c))
            res[N]["S5 random order"].append((base * k, k))
    ndays = len(res[10][names[0]])
    print(f"test days: {ndays} (days with 5+ picks since {start}); picks per day avg {sum(len(days[d]) for d in day_list)/max(len(day_list),1):.1f}; cap per league: {'3' if cap else 'none'}")
    allp = [r for d in day_list for r in days[d]]
    print(f"overall win rate of every pick in the window: {sum(r['y'] for r in allp)/len(allp)*100:.1f}% ({len(allp)} picks)\n")
    for N in (3, 5, 10):
        print(f"── TOP {N} OF THE DAY ─────────────────────────────────────────────────────────────")
        print(f"{'rule':38} {'picks':>6} {'win %':>7}   {'vs today (pts)':>15} {'95% CI over days':>22} {'P(better)':>10}")
        base = res[N][names[0]]
        for nm in names:
            v = res[N][nm]
            W = sum(a for a, _ in v); P = sum(b for _, b in v)
            rate = W / P * 100
            if nm == names[0]:
                print(f"{nm:38} {P:6.0f} {rate:6.1f}%   {'—':>15}")
                continue
            diffs = []
            for _ in range(boot):
                idx = [rnd.randrange(len(v)) for _ in range(len(v))]
                w1 = sum(v[i][0] for i in idx); p1 = sum(v[i][1] for i in idx)
                w0 = sum(base[i][0] for i in idx); p0 = sum(base[i][1] for i in idx)
                diffs.append((w1 / p1 - w0 / p0) * 100)
            diffs.sort()
            d0 = rate - sum(a for a, _ in base) / sum(b for _, b in base) * 100
            print(f"{nm:38} {P:6.0f} {rate:6.1f}%   {d0:+14.1f}  [{diffs[int(.025*boot)]:+6.1f}, {diffs[int(.975*boot)]:+6.1f}]  {sum(1 for x in diffs if x > 0)/boot:10.2f}")
        print()
    return res


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2026-10-04")
    ap.add_argument("--boot", type=int, default=3000)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--no-cap", action="store_true")
    ap.add_argument("--recent-days", type=int, default=21)
    a = ap.parse_args()
    run(load(), a.start, a.boot, a.seed, not a.no_cap, a.recent_days)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
