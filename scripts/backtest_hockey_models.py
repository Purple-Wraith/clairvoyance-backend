#!/usr/bin/env python3
"""Point-in-time backtests behind the hockey-model constants in docs/app.html (pure stdlib, no numpy).

  python3 scripts/backtest_hockey_models.py form        # NHL recent-form term  -> NHL_FORM_* constants
  python3 scripts/backtest_hockey_models.py blend       # model-vs-market blend -> HOCKEY_MKT_BLEND_* constants
  python3 scripts/backtest_hockey_models.py dispersion  # Poisson dispersion / margin / empty-net shape

Data (cached under --cache, default /tmp/cv_hockey_bt): NHL regular-season results from the public NHL API
(api-web.nhle.com club-schedule-season) and, for `blend`, closing DraftKings/ESPN BET prices from ESPN's public
core API (sports.core.api.espn.com .../odds). Everything is point-in-time: a game's inputs use ONLY games played
before it (no look-ahead), with the same baseline the live model uses (75% current-season + 25% prior-season
goals/game, matching clairvoyance_update.fetch_nhl_edge's _NHL_PRIOR_SEASON_WEIGHT blend).
Read-only: never touches the ledger / Supabase.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path

import requests

TEAMS = ("ANA BOS BUF CAR CBJ CGY CHI COL DAL DET EDM FLA LAK MIN MTL NJD NSH NYI NYR OTT PHI PIT SEA SJS STL TBL "
         "TOR UTA VAN VGK WPG WSH ARI").split()
ESPN_FIX = {"LA": "LAK", "NJ": "NJD", "SJ": "SJS", "TB": "TBL", "UTAH": "UTA"}
SEASONS = ["20222023", "20232024", "20242025", "20252026"]
HOME_ICE = 0.055  # nhlMC's hfa; away gets -hfa/2 (same asymmetry as the app)


def _get(url, params=None, tries=4):
    for a in range(tries):
        try:
            r = requests.get(url, params=params, timeout=20)
            if r.status_code == 200:
                return r.json()
        except Exception:
            pass
        time.sleep(1 + a)
    return None


def load_results(cache: Path) -> dict:
    f = cache / "nhl_results.json"
    if f.exists():
        return json.loads(f.read_text())
    out = {}
    for season in SEASONS:
        games = {}
        for t in TEAMS:
            j = _get(f"https://api-web.nhle.com/v1/club-schedule-season/{t}/{season}")
            for g in (j or {}).get("games", []):
                if g.get("gameType") != 2 or g.get("gameState") not in ("OFF", "FINAL"):
                    continue
                games[g["id"]] = dict(id=g["id"], date=g["gameDate"], home=g["homeTeam"]["abbrev"],
                                      away=g["awayTeam"]["abbrev"], hs=g["homeTeam"].get("score"),
                                      as_=g["awayTeam"].get("score"),
                                      ot=(g.get("gameOutcome") or {}).get("lastPeriodType"))
        out[season] = sorted(games.values(), key=lambda x: (x["date"], x["id"]))
        print(f"  {season}: {len(games)} games", file=sys.stderr)
    cache.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(out))
    return out


def pois_pmf(lam, kmax=30):
    p = [math.exp(-lam)]
    for k in range(1, kmax + 1):
        p.append(p[-1] * lam / k)
    return p


def pointintime(results, season, prior, min_gp, wprior=0.25):
    """Yield one dict per game with point-in-time baselines for both teams."""
    pf = defaultdict(lambda: [0, 0, 0])
    for g in results[prior]:
        for t, gf, ga in ((g["home"], g["hs"], g["as_"]), (g["away"], g["as_"], g["hs"])):
            pf[t][0] += gf; pf[t][1] += ga; pf[t][2] += 1
    cur = defaultdict(list)

    def base(t):
        c = cur[t]; n = len(c); p = pf[t]
        pgf = p[0] / p[2] if p[2] else 3.05
        pga = p[1] / p[2] if p[2] else 3.05
        if n == 0:
            return pgf, pga, 0
        return ((1 - wprior) * sum(x[0] for x in c) / n + wprior * pgf,
                (1 - wprior) * sum(x[1] for x in c) / n + wprior * pga, n)

    for g in results[season]:
        h, a = g["home"], g["away"]
        bh, ba = base(h), base(a)
        if bh[2] >= min_gp and ba[2] >= min_gp:
            yield dict(g=g, bh=bh, ba=ba, hist_h=list(cur[h]), hist_a=list(cur[a]),
                       lam_h=(bh[0] + ba[1]) / 2 * (1 + HOME_ICE), lam_a=(ba[0] + bh[1]) / 2 * (1 - HOME_ICE / 2))
        cur[h].append((g["hs"], g["as_"]))
        cur[a].append((g["as_"], g["hs"]))


# ───────────────────────── form ─────────────────────────
def cmd_form(results):
    pairs = [("20232024", "20222023"), ("20242025", "20232024"), ("20252026", "20242025")]
    print("NHL recent own-scoring form: lambda *= 1 + k*(recent GF/g - baseline GF/g), Poisson likelihood grid over k")
    for N in (5, 10, 15, 20):
        obs = []
        for s, p in pairs:
            for r in pointintime(results, s, p, 10):
                for hist, bt, bo, lam_key, y, home in ((r["hist_h"], r["bh"], r["ba"], "lam_h", r["g"]["hs"], 1),
                                                       (r["hist_a"], r["ba"], r["bh"], "lam_a", r["g"]["as_"], 0)):
                    rec = hist[-N:]
                    obs.append((y, r[lam_key], sum(x[0] for x in rec) / len(rec) - bt[0]))
        def ll(k, cap=0.5):
            s = 0.0
            for y, l0, x in obs:
                l = l0 * (1 + max(-cap, min(cap, k * x)))
                s += y * math.log(l) - l
            return s
        ks = [i / 200 for i in range(-20, 41)]  # -0.10 .. 0.20
        lls = [ll(k) for k in ks]
        mx = max(lls); kb = ks[lls.index(mx)]
        inside = [k for k, v in zip(ks, lls) if v >= mx - 1.92]
        l0 = ll(0.0); l05 = ll(0.05, 0.06)
        sd = (sum(x * x for _, _, x in obs) / len(obs)) ** 0.5
        print(f"  L{N:<2} obs={len(obs)} sd(delta)={sd:.3f}  k_MLE={kb:+.3f}  95%CI=[{min(inside):+.3f},{max(inside):+.3f}]  "
              f"dLL(k=.05,cap6%)={l05 - l0:+.2f}  dLL(k_MLE)={mx - l0:+.2f}")


# ───────────────────────── main ─────────────────────────
if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["form", "blend", "dispersion"])
    ap.add_argument("--cache", default="/tmp/cv_hockey_bt")
    a = ap.parse_args()
    cache = Path(a.cache)
    res = load_results(cache)
    if a.cmd == "form":
        cmd_form(res)
    else:
        sys.exit(f"{a.cmd}: not implemented in this revision")
