#!/usr/bin/env python3
"""ANALYSIS ONLY -- does knowing the actual starting goalie beat the team-level goalie rating the model uses today?

  python3 scripts/backtest_goalie_starter.py            # fetch (cached) + report
  python3 scripts/backtest_goalie_starter.py --cache /tmp/cv_goalie_bt --workers 6

It changes nothing: docs/app.html (nhlMC / nhlEns / _nhlApplyGoalieStats), the ledger and Supabase are never touched.

What the live model does today (docs/app.html _nhlApplyGoalieStats): NHL[team].gsax = the gsaa of the team's highest-games-played
goalie in MoneyPuck's season file -- the PRIMARY goalie, whoever is actually in net tonight.  The question is whether replacing that
by the quality of the goalie who actually started adds out-of-sample predictive value, and how often it would even matter.

Data (all public; cached under --cache):
  * NHL API  club-schedule-season  (results, same helper as backtest_hockey_models.py)
  * NHL API  gamecenter/{id}/boxscore  -> each goalie's `starter` flag, TOI, shots against, saves   (~1,300 games per season)
  * MoneyPuck playerData/seasonSummary/{year}/regular/goalies.csv  -> per-goalie GSAx = xGoals - goals, ice time (the listed,
    downloadable season-summary file the app already uses)

Everything is point-in-time (no look-ahead):
  * base win probability = the SAME point-in-time Poisson goal-rate model backtest_hockey_models.pointintime() uses (75% current + 25%
    prior season goals for/against, home-ice 5.5%), i.e. a baseline with NO goalie term;
  * goalie quality r (goals per 60 minutes saved above average) comes from information available before puck drop:
        A "prior-season GSAx":  last season's MoneyPuck GSAx / (TOI_hours + 20)           [shrunk toward 0; no history -> 0]
        B "running save-based": all earlier NHL-API games, (saves - shots*lg_sv)/(shots + 1500) * 30 shots/60, season-to-date counts
                                carried with weight 1 within a season and 0.5 across the season boundary
  * PRIMARY goalie of a team before game t = the goalie with most starts so far this season (previous season's most-starts goalie
    until the team has started a game) -- the same rule as the live "highest GP" pick;
  * STARTER = the goalie flagged `starter` in that game's boxscore (the actual starter, i.e. an ORACLE of perfect pre-game knowledge).
Then for a given quality definition, P(home win) = sigmoid(logit(p_base) + b * (r_home_goalie - r_away_goalie)) with ONE coefficient b
fitted by maximum likelihood on the TRAIN seasons only and scored on the held-out season (walk-forward: train < test).  Log loss and
the paired per-game difference (with a standard error) are reported for: no goalie term, team-primary goalie, actual starter.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import math
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
import backtest_hockey_models as B  # noqa: E402  (results loader + point-in-time baseline)

UA = {"User-Agent": "Mozilla/5.0 (compatible; ClairvoyanceEngine/1.0; +https://clairvoyanceengine.info)"}
MP_GOALIES = "https://moneypuck.com/moneypuck/playerData/seasonSummary/{y}/regular/goalies.csv"
SEASONS = ["20222023", "20232024", "20242025", "20252026"]
LG_SV_DEFAULT = 0.905
K_SHOTS = 1500.0          # shrink for the save-based rating (~50 starts of shots)
K_HOURS = 20.0            # shrink for the prior-season GSAx rating
SHOTS_PER_60 = 30.0


def _get_json(url, tries=4):
    for a in range(tries):
        try:
            r = requests.get(url, headers=UA, timeout=25)
            if r.status_code == 200:
                return r.json()
            if r.status_code == 404:
                return None
        except Exception:
            pass
        time.sleep(1 + a)
    return None


def _toi(s):
    try:
        m, sec = str(s).split(":")
        return int(m) * 60 + int(sec)
    except Exception:
        return 0


def load_boxscores(cache: Path, results: dict, workers: int) -> dict:
    """{game id (str): {"H": [[pid, toi_s, starter, shots_against, saves], ...], "A": [...]}} -- cached incrementally."""
    f = cache / "goalie_boxscores.json"
    box = json.loads(f.read_text()) if f.exists() else {}
    ids = [str(g["id"]) for s in SEASONS for g in results[s] if str(g["id"]) not in box]
    print(f"  boxscores cached {len(box)}, to fetch {len(ids)}", file=sys.stderr)

    def one(gid):
        j = _get_json(f"https://api-web.nhle.com/v1/gamecenter/{gid}/boxscore")
        if not j:
            return gid, None
        pb = j.get("playerByGameStats") or {}
        out = {}
        for side, key in (("H", "homeTeam"), ("A", "awayTeam")):
            out[side] = [[g["playerId"], _toi(g.get("toi")), 1 if g.get("starter") else 0, g.get("shotsAgainst") or 0, g.get("saves") or 0]
                         for g in (pb.get(key) or {}).get("goalies", [])]
        return gid, out

    done = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for gid, out in ex.map(one, ids):
            if out is not None:
                box[gid] = out
            done += 1
            if done % 500 == 0:
                print(f"    {done}/{len(ids)}", file=sys.stderr)
                cache.mkdir(parents=True, exist_ok=True)
                f.write_text(json.dumps(box))
    cache.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(box))
    return box


def load_mp_goalies(cache: Path) -> dict:
    """{start_year: {playerId: (gsax, toi_hours, name)}} from MoneyPuck's season summaries (situation 'all')."""
    f = cache / "mp_goalies.json"
    if f.exists():
        raw = json.loads(f.read_text())
        return {int(y): {int(k): tuple(v) for k, v in d.items()} for y, d in raw.items()}
    out = {}
    for y in sorted({int(s[:4]) - 1 for s in SEASONS} | {int(s[:4]) for s in SEASONS}):
        r = requests.get(MP_GOALIES.format(y=y), headers=UA, timeout=40)
        time.sleep(1)
        if r.status_code != 200:
            continue
        d = {}
        for row in csv.DictReader(io.StringIO(r.text)):
            if row.get("situation") != "all":
                continue
            try:
                d[int(row["playerId"])] = (float(row["xGoals"]) - float(row["goals"]), float(row["icetime"]) / 3600.0, row["name"])
            except (ValueError, KeyError):
                continue
        out[y] = d
    cache.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps({str(y): {str(k): list(v) for k, v in d.items()} for y, d in out.items()}))
    return out


def starter_of(goalies):
    """pid of the flagged starter; else the goalie with the most TOI; None if nobody has TOI."""
    fl = [g for g in goalies if g[2]]
    if fl:
        return fl[0][0]
    played = [g for g in goalies if g[1] > 0]
    return max(played, key=lambda g: g[1])[0] if played else None


class Tracker:
    """Point-in-time goalie state, advanced game by game within a season, rolled at the season boundary."""

    def __init__(self, mp, season):
        self.sy = int(season[:4])
        self.mp = mp
        self.starts = defaultdict(lambda: defaultdict(int))     # team -> pid -> starts this season
        self.prev_primary = {}                                  # team -> pid (previous season's most starts)
        self.cnt = defaultdict(lambda: [0.0, 0.0])              # pid -> [shots, saves] (decayed across seasons)
        self.lg = [0.0, 0.0]

    def roll(self, new_season):
        for t, d in self.starts.items():
            if d:
                self.prev_primary[t] = max(d.items(), key=lambda kv: kv[1])[0]
        self.starts = defaultdict(lambda: defaultdict(int))
        for pid in self.cnt:
            self.cnt[pid][0] *= 0.5
            self.cnt[pid][1] *= 0.5
        self.lg = [self.lg[0] * 0.5, self.lg[1] * 0.5]
        self.sy = int(new_season[:4])

    def primary(self, team):
        d = self.starts[team]
        if d:
            return max(d.items(), key=lambda kv: kv[1])[0]
        return self.prev_primary.get(team)

    def r_prior_gsax(self, pid):
        if pid is None:
            return 0.0
        rec = self.mp.get(self.sy - 1, {}).get(pid)
        if not rec:
            return 0.0
        gsax, hours, _ = rec
        return gsax / (hours + K_HOURS)                         # goals per 60 min, shrunk

    def r_running_save(self, pid):
        if pid is None:
            return 0.0
        sa, sv = self.cnt[pid]
        lg = (self.lg[1] / self.lg[0]) if self.lg[0] > 500 else LG_SV_DEFAULT
        return (sv - sa * lg) / (sa + K_SHOTS) * SHOTS_PER_60   # goals per 60 min saved above average, shrunk

    def update(self, game_box, home, away):
        for side, team in (("H", home), ("A", away)):
            st = starter_of(game_box[side])
            if st is not None:
                self.starts[team][st] += 1
            for pid, toi, _, sa, sv in game_box[side]:
                if sa:
                    self.cnt[pid][0] += sa
                    self.cnt[pid][1] += sv
                    self.lg[0] += sa
                    self.lg[1] += sv


def build_rows(results, box, mp):
    """One row per game with the base prob, outcome, and each quality definition for primary vs starter."""
    rows = []
    tr = None
    for si, season in enumerate(SEASONS):
        if tr is None:
            tr = Tracker(mp, season)
        else:
            tr.roll(season)
        # point-in-time baseline needs the previous season's results; the first season has none -> burn-in only
        prior = SEASONS[si - 1] if si else None
        base = {}
        if prior:
            for r in B.pointintime(results, season, prior, 10):
                base[r["g"]["id"]] = r
        for g in results[season]:
            gb = box.get(str(g["id"]))
            if gb is None:
                continue
            h, a = g["home"], g["away"]
            r = base.get(g["id"])
            if r is not None:
                ph, pa = B.pois_pmf(r["lam_h"]), B.pois_pmf(r["lam_a"])
                ks = range(30)
                hw = sum(ph[i] * pa[j] for i in ks for j in ks if i > j) + 0.5 * sum(ph[i] * pa[i] for i in ks)
                ph_h, ph_a = tr.primary(h), tr.primary(a)
                sh, sa_ = starter_of(gb["H"]), starter_of(gb["A"])
                if sh is not None and sa_ is not None and ph_h is not None and ph_a is not None:
                    rows.append(dict(
                        season=season, id=g["id"], y=1.0 if g["hs"] > g["as_"] else 0.0, pb=min(.98, max(.02, hw)),
                        prim_diff=(sh != ph_h) + (sa_ != ph_a),
                        A_prim=tr.r_prior_gsax(ph_h) - tr.r_prior_gsax(ph_a), A_start=tr.r_prior_gsax(sh) - tr.r_prior_gsax(sa_),
                        B_prim=tr.r_running_save(ph_h) - tr.r_running_save(ph_a), B_start=tr.r_running_save(sh) - tr.r_running_save(sa_),
                        start_h_is_prim=int(sh == ph_h), start_a_is_prim=int(sa_ == ph_a)))
            tr.update(gb, h, a)
    return rows


def _ll_rows(rows, key, b):
    s = 0.0
    for r in rows:
        p = B._sig(B._logit(r["pb"]) + b * (r[key] if key else 0.0))
        p = min(1 - 1e-6, max(1e-6, p))
        s -= r["y"] * math.log(p) + (1 - r["y"]) * math.log(1 - p)
    return s


def fit_b(rows, key):
    """1-D MLE for the goalie coefficient by golden-section on [-1, 3] (log loss is convex in b)."""
    lo, hi = -1.0, 3.0
    phi = (math.sqrt(5) - 1) / 2
    c, d = hi - phi * (hi - lo), lo + phi * (hi - lo)
    fc, fd = _ll_rows(rows, key, c), _ll_rows(rows, key, d)
    for _ in range(40):
        if fc < fd:
            hi, d, fd = d, c, fc
            c = hi - phi * (hi - lo)
            fc = _ll_rows(rows, key, c)
        else:
            lo, c, fc = c, d, fd
            d = lo + phi * (hi - lo)
            fd = _ll_rows(rows, key, d)
    return (lo + hi) / 2


def per_game_ll(rows, key, b):
    out = []
    for r in rows:
        p = B._sig(B._logit(r["pb"]) + b * (r[key] if key else 0.0))
        p = min(1 - 1e-6, max(1e-6, p))
        out.append(-(r["y"] * math.log(p) + (1 - r["y"]) * math.log(1 - p)))
    return out


def paired(a, b):
    d = [x - y for x, y in zip(a, b)]
    m = sum(d) / len(d)
    sd = (sum((x - m) ** 2 for x in d) / (len(d) - 1)) ** 0.5
    return m, sd / math.sqrt(len(d))


def report(rows):
    print(f"\nGames with a point-in-time baseline, a flagged starter and a primary on both sides: {len(rows)}")
    for s in SEASONS:
        n = sum(1 for r in rows if r["season"] == s)
        if n:
            print(f"  {s}: {n}")
    # how often does the actual starter differ from the team's primary goalie?
    both = len(rows) * 2
    diff = sum(r["prim_diff"] for r in rows)
    print(f"\nStarter != team primary goalie: {diff}/{both} team-games = {diff / both:.1%}  "
          f"(games where at least one side differs: {sum(1 for r in rows if r['prim_diff']) / len(rows):.1%})")
    for variant, label in (("A", "A  prior-season MoneyPuck GSAx/60 (shrunk)"), ("B", "B  running NHL-API save%-above-average/60 (shrunk)")):
        print(f"\n=== Goalie quality {label} ===")
        sd = (sum(r[f"{variant}_start"] ** 2 for r in rows) / len(rows)) ** 0.5
        spr = (sum(r[f"{variant}_prim"] ** 2 for r in rows) / len(rows)) ** 0.5
        print(f"  RMS of (home-away) goalie rating: primary {spr:.3f}  starter {sd:.3f}  goals/60")
        pooled = {"base": [], "prim": [], "start": []}
        sub = {"base": [], "prim": [], "start": []}   # same, restricted to games where a non-primary goalie started on >= 1 side
        for test in SEASONS[2:]:  # train on every earlier season that has rows (>= the 2 seasons before the test one)
            train = [r for r in rows if r["season"] < test]
            tst = [r for r in rows if r["season"] == test]
            if not train or not tst:
                continue
            bp, bs = fit_b(train, f"{variant}_prim"), fit_b(train, f"{variant}_start")
            l0 = per_game_ll(tst, None, 0.0)
            lp = per_game_ll(tst, f"{variant}_prim", bp)
            ls = per_game_ll(tst, f"{variant}_start", bs)
            pooled["base"] += l0; pooled["prim"] += lp; pooled["start"] += ls
            for r, a0, a1, a2 in zip(tst, l0, lp, ls):
                if r["prim_diff"]:
                    sub["base"].append(a0); sub["prim"].append(a1); sub["start"].append(a2)
            n = len(tst)
            dps, se_ps = paired(lp, ls)
            d0p, se0p = paired(l0, lp)
            print(f"  test {test} (train {train[0]['season']}-{train[-1]['season']}, n_train={len(train)}, n_test={n}): "
                  f"b_primary={bp:.2f} b_starter={bs:.2f}")
            print(f"     log loss  no-goalie {sum(l0)/n:.4f} | team-primary {sum(lp)/n:.4f} | actual-starter {sum(ls)/n:.4f}"
                  f"   (primary-minus-starter {dps:+.4f} +/- {se_ps:.4f})")
        n = len(pooled["base"])
        if n:
            d_ps, se_ps = paired(pooled["prim"], pooled["start"])
            d_0s, se_0s = paired(pooled["base"], pooled["start"])
            d_0p, se_0p = paired(pooled["base"], pooled["prim"])
            print(f"  POOLED out-of-sample n={n}: no-goalie {sum(pooled['base'])/n:.4f} | team-primary {sum(pooled['prim'])/n:.4f} | "
                  f"actual-starter {sum(pooled['start'])/n:.4f}")
            print(f"     no-goalie minus primary {d_0p:+.4f} +/- {se_0p:.4f};  no-goalie minus starter {d_0s:+.4f} +/- {se_0s:.4f};  "
                  f"primary minus starter {d_ps:+.4f} +/- {se_ps:.4f}   (positive = the second is better)")
        m = len(sub["base"])
        if m:
            d_ps, se_ps = paired(sub["prim"], sub["start"])
            print(f"  ...only the {m} out-of-sample games where a non-primary goalie started: no-goalie {sum(sub['base'])/m:.4f} | "
                  f"team-primary {sum(sub['prim'])/m:.4f} | actual-starter {sum(sub['start'])/m:.4f}   (primary minus starter {d_ps:+.4f} +/- {se_ps:.4f})")
    # games where the starter really was not the primary: how much does the rating move?
    nd = [r for r in rows if r["prim_diff"]]
    if nd:
        for variant in ("A", "B"):
            mv = sum(abs(r[f"{variant}_start"] - r[f"{variant}_prim"]) for r in nd) / len(nd)
            print(f"\nIn the {len(nd)} games where a backup/other started on at least one side, mean |starter-minus-primary| rating shift "
                  f"({variant}) = {mv:.3f} goals/60 (vs RMS home-away rating spread above)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", default="/tmp/cv_goalie_bt")
    ap.add_argument("--workers", type=int, default=6)
    args = ap.parse_args()
    cache = Path(args.cache)
    results = B.load_results(cache)
    box = load_boxscores(cache, results, args.workers)
    mp = load_mp_goalies(cache)
    rows = build_rows(results, box, mp)
    report(rows)


if __name__ == "__main__":
    main()
