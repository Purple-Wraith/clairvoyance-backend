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


# ───────────────────────── blend ─────────────────────────
def _am2dec(a):
    return 1 + a / 100 if a > 0 else 1 + 100 / abs(a)


def _novig(da, db):
    ia, ib = 1 / da, 1 / db
    return ia / (ia + ib)


def _logit(p):
    return math.log(p / (1 - p))


def _sig(z):
    return 1 / (1 + math.exp(-z))


def load_odds(cache: Path) -> dict:
    """Closing DraftKings / ESPN BET prices for every completed regular-season NHL game (ESPN core API)."""
    f = cache / "nhl_odds_hist.json"
    if f.exists():
        return json.loads(f.read_text())
    seasons = {"20242025": (date(2024, 10, 4), date(2025, 4, 18)), "20252026": (date(2025, 10, 7), date(2026, 4, 17))}

    def day(d):
        j = _get("https://site.api.espn.com/apis/site/v2/sports/hockey/nhl/scoreboard",
                 {"dates": d.strftime("%Y%m%d"), "limit": 50})
        out = []
        for e in (j or {}).get("events", []):
            c = e["competitions"][0]
            if c["status"]["type"]["state"] != "post" or (e.get("season") or {}).get("type") != 2:
                continue
            hm = [x for x in c["competitors"] if x["homeAway"] == "home"][0]
            aw = [x for x in c["competitors"] if x["homeAway"] == "away"][0]
            out.append(dict(eid=e["id"], date=e["date"], period=c["status"].get("period"),
                            home=ESPN_FIX.get(hm["team"]["abbreviation"], hm["team"]["abbreviation"]),
                            away=ESPN_FIX.get(aw["team"]["abbreviation"], aw["team"]["abbreviation"]),
                            hs=int(hm["score"]), as_=int(aw["score"])))
        return out

    def odds(eid):
        j = _get(f"https://sports.core.api.espn.com/v2/sports/hockey/leagues/nhl/events/{eid}/competitions/{eid}/odds")
        items = (j or {}).get("items") or []
        if not items:
            return None
        it = items[-1]
        for i in items:
            if (i.get("provider") or {}).get("name", "").lower().startswith("draft"):
                it = i
        return dict(ou=it.get("overUnder"), overOdds=it.get("overOdds"), underOdds=it.get("underOdds"),
                    hML=it["homeTeamOdds"].get("moneyLine"), aML=it["awayTeamOdds"].get("moneyLine"),
                    hSpreadOdds=it["homeTeamOdds"].get("spreadOdds"), aSpreadOdds=it["awayTeamOdds"].get("spreadOdds"),
                    spread=it.get("spread"))

    out = {}
    for s, (a, b) in seasons.items():
        days, d = [], a
        while d <= b:
            days.append(d); d += timedelta(days=1)
        with ThreadPoolExecutor(8) as ex:
            games = [g for l in ex.map(day, days) for g in l]
        with ThreadPoolExecutor(8) as ex:
            for g, o in zip(games, ex.map(lambda g: odds(g["eid"]), games)):
                g["odds"] = o
        out[s] = games
        print(f"  odds {s}: {len(games)} games", file=sys.stderr)
    cache.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(out))
    return out


def blend_rows(results, odds, season, prior, min_gp):
    """One row per game with a model prob (point-in-time Poisson from goal rates), the no-vig market prob and the outcome."""
    games = {(g["home"], g["away"], g["date"][:10]): g for g in odds[season]}
    rows = []
    for r in pointintime(results, season, prior, min_gp):
        g = r["g"]
        # NHL API dates are local game dates; ESPN's are UTC timestamps -> match on teams + a +-1 day window
        og = None
        for dd in (0, 1, -1):
            d = (date.fromisoformat(g["date"]) + timedelta(days=dd)).isoformat()
            og = games.get((g["home"], g["away"], d))
            if og:
                break
        o = og and og.get("odds")
        if not o or o.get("hML") is None or o.get("aML") is None:
            continue
        ks = range(30)
        ph, pa = pois_pmf(r["lam_h"]), pois_pmf(r["lam_a"])
        hw = sum(ph[i] * pa[j] for i in ks for j in ks if i > j) + 0.5 * sum(ph[i] * pa[i] for i in ks)
        row = dict(n=min(r["bh"][2], r["ba"][2]), y=1.0 if og["hs"] > og["as_"] else 0.0,
                   pm=min(.98, max(.02, hw)), mk=_novig(_am2dec(o["hML"]), _am2dec(o["aML"])), ou=None, pl=None)
        line = o.get("ou")
        if line is not None and o.get("overOdds") is not None and o.get("underOdds") is not None:
            tot = og["hs"] + og["as_"] - (1 if og.get("period") == 5 else 0)  # books exclude the shootout goal
            if tot != line:
                tl = r["lam_h"] + r["lam_a"]
                cdf = sum(math.exp(-tl) * tl ** k / math.factorial(k) for k in range(int(math.floor(line)) + 1))
                row["ou"] = (min(.98, max(.02, 1 - cdf)), _novig(_am2dec(o["overOdds"]), _am2dec(o["underOdds"])),
                             1.0 if tot > line else 0.0)
        hl = o.get("spread")
        if hl is not None and o.get("hSpreadOdds") is not None and o.get("aSpreadOdds") is not None and abs(abs(hl) - 1.5) < .01:
            phc = sum(ph[i] * pa[j] for i in ks for j in ks if (i - j) + hl > 0)
            row["pl"] = (min(.98, max(.02, phc)), _novig(_am2dec(o["hSpreadOdds"]), _am2dec(o["aSpreadOdds"])),
                         1.0 if (og["hs"] - og["as_"]) + hl > 0 else 0.0)
        rows.append(row)
    return rows


def _ll(ys, ps):
    return -sum(y * math.log(min(1 - 1e-6, max(1e-6, p))) + (1 - y) * math.log(min(1 - 1e-6, max(1e-6, 1 - p)))
                for y, p in zip(ys, ps)) / len(ys)


def _study(name, ys, pm, mk):
    def llb(a):
        return _ll(ys, [_sig((1 - a) * _logit(m) + a * _logit(k)) for m, k in zip(pm, mk)])
    grid = [i / 100 for i in range(0, 101)]
    lls = [llb(a) * len(ys) for a in grid]
    mx = min(lls); ab = grid[lls.index(mx)]
    ok = [a for a, v in zip(grid, lls) if v <= mx + 1.92]
    print(f"  {name:<34} n={len(ys):<5} logloss model {_ll(ys, pm):.4f} | market {_ll(ys, mk):.4f} | "
          f"alpha_MLE {ab:.2f}  95% CI [{min(ok):.2f},{max(ok):.2f}]")


def cmd_blend(results, odds):
    print("Logit-blend weight on the no-vig market that minimizes log loss (1.0 = market only); NHL closing lines")
    rows = blend_rows(results, odds, "20252026", "20242025", 10) + blend_rows(results, odds, "20242025", "20232024", 10)
    _study("ML (>=10 GP)", [r["y"] for r in rows], [r["pm"] for r in rows], [r["mk"] for r in rows])
    ro = [r for r in rows if r["ou"]]
    _study("O/U over (>=10 GP)", [r["ou"][2] for r in ro], [r["ou"][0] for r in ro], [r["ou"][1] for r in ro])
    rp = [r for r in rows if r["pl"]]
    _study("Puck line home covers (>=10 GP)", [r["pl"][2] for r in rp], [r["pl"][0] for r in rp], [r["pl"][1] for r in rp])
    early = blend_rows(results, odds, "20252026", "20242025", 1) + blend_rows(results, odds, "20242025", "20232024", 1)
    early = [r for r in early if r["n"] < 10]
    _study("ML (lesser team <10 GP)", [r["y"] for r in early], [r["pm"] for r in early], [r["mk"] for r in early])
    eo = [r for r in early if r["ou"]]
    _study("O/U over (lesser team <10 GP)", [r["ou"][2] for r in eo], [r["ou"][0] for r in eo], [r["ou"][1] for r in eo])


# ───────────────────────── dispersion ─────────────────────────
def _margin_stats(lh, la, kmax=25):
    ph, pa = pois_pmf(lh, kmax), pois_pmf(la, kmax)
    hw = aw = tie = hm2 = am2 = 0.0
    for i, a in enumerate(ph):
        for j, b in enumerate(pa):
            pr = a * b
            if i > j: hw += pr
            elif j > i: aw += pr
            else: tie += pr
            if i - j >= 2: hm2 += pr
            if j - i >= 2: am2 += pr
    return dict(hw=hw, aw=aw, tie=tie, hm2=hm2, am2=am2, m2=hm2 + am2)


def _alpha_nb2(ys, ls):
    """Method-of-moments NB2 dispersion: Var = mu + alpha*mu^2  ->  alpha = sum((y-mu)^2 - mu) / sum(mu^2)."""
    return sum((y - l) ** 2 - l for y, l in zip(ys, ls)) / sum(l * l for l in ls)


def _boot_alpha(games, n_boot=300):
    import random
    rnd = random.Random(1)
    out = []
    for _ in range(n_boot):
        samp = [games[rnd.randrange(len(games))] for _ in games]
        out.append(_alpha_nb2([g["hs"] for g in samp] + [g["as_"] for g in samp],
                              [g["lh"] for g in samp] + [g["la"] for g in samp]))
    m = sum(out) / len(out)
    return (sum((x - m) ** 2 for x in out) / len(out)) ** 0.5


def _dispersion_report(label, games, regulation=False):
    """games: dicts with lh, la (point-in-time lambdas), hs, as_, ot (bool: went to OT/SO, or None)."""
    if regulation:  # an OT/SO game was tied after 60 min at the loser's score
        games = [dict(g, hs=min(g["hs"], g["as_"]) if g["ot"] else g["hs"],
                      as_=min(g["hs"], g["as_"]) if g["ot"] else g["as_"]) for g in games]
    ys = [g["hs"] for g in games] + [g["as_"] for g in games]
    ls = [g["lh"] for g in games] + [g["la"] for g in games]
    n = len(games)
    ms = [_margin_stats(g["lh"], g["la"]) for g in games]
    print(f"  {label}: n={n} Pearson chi2/df={sum((y - l) ** 2 / l for y, l in zip(ys, ls)) / len(ys):.3f}  "
          f"NB2 alpha={_alpha_nb2(ys, ls):+.4f} (boot se {_boot_alpha(games):.4f})  |  "
          f"ties obs {sum(1 for g in games if g['hs'] == g['as_']) / n:.3f} vs Poisson {sum(m['tie'] for m in ms) / n:.3f}  |  "
          f"margin>=2 obs {sum(1 for g in games if abs(g['hs'] - g['as_']) >= 2) / n:.3f} vs Poisson {sum(m['m2'] for m in ms) / n:.3f}")


def cmd_dispersion(results):
    pairs = [("20232024", "20222023"), ("20242025", "20232024"), ("20252026", "20242025")]
    allg = []
    print("NHL (point-in-time Poisson lambdas, >=10 GP): is per-team scoring over-dispersed vs Poisson? (alpha>0 = yes)")
    for s, p in pairs:
        gs = [dict(lh=r["lam_h"], la=r["lam_a"], hs=r["g"]["hs"], as_=r["g"]["as_"], ot=r["g"]["ot"] in ("OT", "SO"))
              for r in pointintime(results, s, p, 10)]
        allg += gs
        _dispersion_report(f"{s} final score", gs)
    _dispersion_report("pooled final score", allg)
    _dispersion_report("pooled regulation-equivalent", allg, regulation=True)
    # favorite -1.5 cover (final score, OT games are always 1-goal margins): observed vs Poisson, and the logit shift
    rows = []
    for g in allg:
        m = _margin_stats(g["lh"], g["la"])
        home_fav = m["hw"] + m["tie"] / 2 >= .5
        diff = g["hs"] - g["as_"]
        rows.append((m["hm2"] if home_fav else m["am2"], 1.0 if (diff >= 2 if home_fav else diff <= -2) else 0.0))

    def nll(s):
        return -sum(y * math.log(_sig(_logit(p) + s)) + (1 - y) * math.log(1 - _sig(_logit(p) + s)) for p, y in rows)
    grid = [i / 200 for i in range(-40, 81)]
    lls = [nll(s) for s in grid]; mn = min(lls); sb = grid[lls.index(mn)]
    ok = [s for s, v in zip(grid, lls) if v <= mn + 1.92]
    print(f"  favorite -1.5 cover: Poisson {sum(r[0] for r in rows) / len(rows):.3f} vs observed "
          f"{sum(r[1] for r in rows) / len(rows):.3f} (n={len(rows)}); logit shift MLE {sb:+.3f}  95% CI [{min(ok):+.3f},{max(ok):+.3f}]")
    # European leagues: lambdas from PRIOR-season rates only (out of sample), completed 2026-27 games in docs/*_schedule.json
    root = Path(__file__).resolve().parent.parent / "docs"
    pool = []
    print("European leagues (completed 2026-27 games; lambdas from prior-season rates only -> no look-ahead):")
    for lg in ("liiga", "shl", "nla", "extraliga"):
        f = root / f"{lg}_schedule.json"
        if not f.exists():
            continue
        D = json.loads(f.read_text()); T = D["teams"]
        pf = [t["prevSeason"]["gf"] / t["prevSeason"]["gp"] for t in T.values() if t.get("prevSeason") and t["prevSeason"].get("gp")]
        avg = sum(pf) / len(pf)

        def rt(tid):
            pv = (T.get(tid) or {}).get("prevSeason")
            return (pv["gf"] / pv["gp"], pv["ga"] / pv["gp"]) if pv and pv.get("gp") else (avg, avg)
        gs = []
        for g in D["games"]:
            if g["state"] != "post" or g.get("homeScore") is None:
                continue
            hf, ha = rt(g["home"]); af, aa = rt(g["away"])
            gs.append(dict(lh=(hf + aa) / 2 * (1 + HOME_ICE), la=(af + ha) / 2 * (1 - HOME_ICE / 2),
                           hs=g["homeScore"], as_=g["awayScore"], ot=None))
        if gs:
            _dispersion_report(lg.upper(), gs); pool += gs
    if pool:
        _dispersion_report("EURO pooled", pool)


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
    elif a.cmd == "blend":
        cmd_blend(res, load_odds(cache))
    else:
        cmd_dispersion(res)
