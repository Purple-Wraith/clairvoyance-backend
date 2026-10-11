#!/usr/bin/env python3
"""NFL engine evidence tool (2026-10-11 audit). Re-derives every number quoted in docs/app.html's NFL_MKT_ANCHOR comment and scripts/nfl_engine.py.

    python3 scripts/backtest_nfl_engine.py            # everything
    python3 scripts/backtest_nfl_engine.py ledger     # settled NFL ledger diagnosis (win rate / calibration by type, cohort, alt vs posted, stated band, fav/dog, ...)
    python3 scripts/backtest_nfl_engine.py anchor     # model vs market on completed games: slope, direction hit rates, log-loss before/after, sigma
    python3 scripts/backtest_nfl_engine.py lane       # the alternate-line cushion curve on closing lines
    python3 scripts/backtest_nfl_engine.py --refresh-archive   # (full clone) add newly completed games to data/nfl_closing_lines_2026.json -- run weekly so the sample keeps growing

DATA. docs/nfl_schedule.json is overwritten every refresh and keeps a game's spread / over-under only until it is played, so the pre-kickoff lines of completed games were recovered from the committed
history of that file (the last snapshot of each game taken BEFORE kickoff -- the lines were 1-9 hours stale at worst, i.e. close to the close) and archived in data/nfl_closing_lines_2026.json. Final
scores come from the same file. Ratings are rebuilt
AS OF each kickoff from the completed games before it (no look-ahead): point differential per game + the 1.8-point home edge for the margin, the mean of the two teams' average game totals for the total. The live
card ALSO adds capped yardage / turnover / injury terms that were not archived per date -- they are ignored here (each is capped at +/-3, +/-1.5, +/-3.5 points), so the raw model below is a faithful but not
identical stand-in. n is small (65 completed games, 49 with a rating): every figure carries an interval and none of this is an edge claim.
"""
from __future__ import annotations

import json
import math
import random
import re
import statistics as st
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import lock_timing as lt  # noqa: E402
import nfl_engine as NE  # noqa: E402

HFA = 1.8                 # docs/app.html _NFL_HFA_DEFAULT
OLD_SIGMA_TOTAL = 10.0    # the value before the audit
SIGMA_MARGIN = NE.NFL_SIGMA_MARGIN


# ── statistics ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
def phi(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def wilson(w: int, n: int, z: float = 1.96):
    if n == 0:
        return 0.0, 1.0
    p = w / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    a = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (c - a) / d, (c + a) / d


def fmt_rate(w: int, n: int) -> str:
    lo, hi = wilson(w, n)
    return f"{w}/{n} = {100 * w / max(n, 1):.1f}% [{100 * lo:.0f},{100 * hi:.0f}]"


def ll(p: float, o: int) -> float:
    p = min(0.999, max(0.001, p))
    return -(o * math.log(p) + (1 - o) * math.log(1 - p))


def boot_ci(vals, fn, reps: int = 4000, seed: int = 7):
    rng = random.Random(seed)
    out = []
    for _ in range(reps):
        out.append(fn([vals[rng.randrange(len(vals))] for _ in vals]))
    out.sort()
    return out[int(0.025 * reps)], out[int(0.975 * reps)]


# ── completed games with their pre-kickoff lines and as-of ratings ─────────────────────────────────────────────────────────────────────────────────
def _git(args, root: Path = ROOT) -> str:
    return subprocess.check_output(["git", *args], cwd=str(root), text=True, stderr=subprocess.DEVNULL)


def load_history(root: Path = ROOT) -> dict:
    """{game id: {'meta': {...}, 'snaps': [(commit time, spread, total, homeML, awayML, state), ...]}} from every committed snapshot of docs/nfl_schedule.json (regular season only)."""
    commits = [l.split() for l in _git(["log", "--format=%H %ct", "--", "docs/nfl_schedule.json"], root).splitlines() if l.strip()]
    commits.reverse()
    games: dict = {}
    for h, ct in commits:
        try:
            sched = json.loads(_git(["show", f"{h}:docs/nfl_schedule.json"], root))
        except Exception:
            continue
        for _w, gs in (sched.get("weeks") or {}).items():
            for g in gs:
                if g.get("seasonType") == 1:
                    continue
                e = games.setdefault(g["id"], {"meta": {}, "snaps": []})
                e["meta"].update({k: g.get(k) for k in ("date", "home", "away", "week", "neutralSite", "homeScore", "awayScore", "state")})
                e["snaps"].append((int(ct), g.get("spread"), g.get("overUnder"), g.get("homeML"), g.get("awayML"), g.get("state")))
    return games


def _ts(date: str) -> float:
    return datetime.strptime(date, "%Y-%m-%dT%H:%MZ").replace(tzinfo=timezone.utc).timestamp()


ARCHIVE = ROOT / "data" / "nfl_closing_lines_2026.json"


def archive_rows(hist: dict) -> list[dict]:
    """Completed regular-season games with a pre-kickoff spread AND total: the LAST snapshot taken before kickoff supplies the lines (1-9 hours before kickoff in this data)."""
    rows = []
    for gid, g in hist.items():
        m = g["meta"]
        if m.get("week") is None or m.get("state") != "post" or m.get("homeScore") is None or not m.get("date"):
            continue
        pre = [s for s in g["snaps"] if s[0] < _ts(m["date"]) and s[1] is not None and s[2] is not None]
        if not pre:
            continue
        s = pre[-1]
        rows.append({"id": gid, "week": m["week"], "date": m["date"], "home": m["home"], "away": m["away"], "neutral": bool(m.get("neutralSite")),
                     "homeScore": m["homeScore"], "awayScore": m["awayScore"], "spread": s[1], "overUnder": s[2], "homeML": s[3], "awayML": s[4], "lineAt": s[0]})
    rows.sort(key=lambda r: (r["date"], r["id"]))
    return rows


def load_archive(root: Path = ROOT) -> list[dict]:
    f = root / "data" / "nfl_closing_lines_2026.json"
    return json.loads(f.read_text()).get("games", []) if f.exists() else []


def refresh_archive(root: Path = ROOT) -> int:
    """Merge the lines recoverable from the committed history into data/nfl_closing_lines_2026.json (existing rows are kept; a row is only added for a game not archived yet)."""
    have = {r["id"]: r for r in load_archive(root)}
    for r in archive_rows(load_history(root)):
        have.setdefault(r["id"], r)
    rows = sorted(have.values(), key=lambda r: (r["date"], r["id"]))
    (root / "data").mkdir(exist_ok=True)
    (root / "data" / "nfl_closing_lines_2026.json").write_text(json.dumps(
        {"note": "Pre-kickoff spread / over-under of completed 2026 NFL regular-season games (the last archived schedule snapshot before kickoff) + final scores. docs/nfl_schedule.json drops a game's "
                 "lines once it is played, so this is the only durable copy. Built by scripts/backtest_nfl_engine.py --refresh-archive from the committed history of docs/nfl_schedule.json.",
         "games": rows}, indent=1) + "\n")
    return len(rows)


def build_games(rows_in: list[dict], hfa: float = HFA) -> list[dict]:
    """Archive rows -> games oldest first, each with as-of ratings built ONLY from games that kicked off earlier (every archived game feeds the ratings of the later ones)."""
    rows = []
    for r in rows_in:
        rows.append(dict(id=r["id"], week=r["week"], st=_ts(r["date"]), home=r["home"], away=r["away"], hs=r["homeScore"], as_=r["awayScore"], spread=r["spread"], ou=r["overUnder"],
                         neutral=bool(r.get("neutral"))))
    rows.sort(key=lambda r: (r["st"], r["id"]))
    for i, g in enumerate(rows):
        for side in ("home", "away"):
            t = g[side]
            prev = [x for x in rows[:i] if x["st"] < g["st"] - 1 and t in (x["home"], x["away"])]
            n = len(prev)
            if n == 0:
                g[side + "_n"] = 0
                g[side + "_dpg"] = g[side + "_tot"] = None
                continue
            pf = sum(x["hs"] if x["home"] == t else x["as_"] for x in prev)
            pa = sum(x["as_"] if x["home"] == t else x["hs"] for x in prev)
            g[side + "_n"], g[side + "_dpg"], g[side + "_tot"] = n, (pf - pa) / n, (pf + pa) / n
        g["M"] = -g["spread"]                       # the home margin the posted line implies
        g["y"], g["tot"] = g["hs"] - g["as_"], g["hs"] + g["as_"]
        g["nmin"] = min(g["home_n"], g["away_n"])
        if g["home_dpg"] is not None and g["away_dpg"] is not None:
            g["R"] = g["home_dpg"] - g["away_dpg"] + (0.0 if g["neutral"] else hfa)
            g["Tm"] = (g["home_tot"] + g["away_tot"]) / 2
        else:
            g["R"] = g["Tm"] = None
    return rows


# ── ANCHOR evidence ──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
def slope(xs, ys):
    sxx = sum(a * a for a in xs)
    return sum(a * b for a, b in zip(xs, ys)) / sxx if sxx else 0.0


def anchor_report(games: list[dict], out=print) -> dict:
    A = [g for g in games if g["R"] is not None]
    res: dict = {"games": len(games), "rated": len(A)}
    out(f"completed games with a pre-kickoff line: {len(games)}; with an as-of rating (weeks 2+): {len(A)}")
    errM = [g["y"] - g["M"] for g in games]
    errT = [g["tot"] - g["ou"] for g in games]
    lo, hi = boot_ci(errM, st.pstdev)
    lo2, hi2 = boot_ci(errT, st.pstdev)
    res.update(sd_margin_vs_line=st.pstdev(errM), sd_total_vs_line=st.pstdev(errT))
    out(f"final margin - spread : mean {st.mean(errM):+.2f} sd {st.pstdev(errM):.2f}  (bootstrap 95% CI {lo:.1f}-{hi:.1f})   -> NFL_SIGMA_MARGIN {NE.NFL_SIGMA_MARGIN}")
    out(f"final total  - O/U    : mean {st.mean(errT):+.2f} sd {st.pstdev(errT):.2f}  (bootstrap 95% CI {lo2:.1f}-{hi2:.1f})   -> NFL_SIGMA_TOTAL {NE.NFL_SIGMA_TOTAL} (was {OLD_SIGMA_TOTAL})")
    dis = [g["R"] - g["M"] for g in A]
    res["sd_model_vs_line"] = st.pstdev(dis)
    out(f"model margin - spread : sd {st.pstdev(dis):.1f} (by week: " + ", ".join(f"wk{w} {st.pstdev([g['R'] - g['M'] for g in A if g['week'] == w]):.1f}" for w in sorted({g['week'] for g in A}) if sum(1 for g in A if g['week'] == w) > 2) + ")")
    out(f"model error vs final margin: sd {st.pstdev([g['y'] - g['R'] for g in A]):.1f}   market error on the same games: sd {st.pstdev([g['y'] - g['M'] for g in A]):.1f}")
    xs, ys = [g["R"] - g["M"] for g in A], [g["y"] - g["M"] for g in A]
    lam = slope(xs, ys)
    pairs = list(zip(xs, ys))
    blo, bhi = boot_ci(pairs, lambda b: slope([p[0] for p in b], [p[1] for p in b]))
    res.update(slope_margin=lam, slope_margin_ci=(blo, bhi))
    out(f"SLOPE of (final margin - spread) on (model margin - spread): {lam:+.3f}  bootstrap 95% CI [{blo:+.3f}, {bhi:+.3f}]   (1 = trust the model, 0 = the line is all there is)")
    xt, yt = [g["Tm"] - g["ou"] for g in A], [g["tot"] - g["ou"] for g in A]
    mu = slope(xt, yt)
    tlo, thi = boot_ci(list(zip(xt, yt)), lambda b: slope([p[0] for p in b], [p[1] for p in b]))
    res.update(slope_total=mu, slope_total_ci=(tlo, thi))
    out(f"SLOPE of (final total - O/U) on (model total - O/U): {mu:+.3f}  bootstrap 95% CI [{tlo:+.3f}, {thi:+.3f}]")
    # direction hit rates at k=0
    def hit(rows, f):
        w = sum(1 for g in rows if f(g) > 0)
        n = sum(1 for g in rows if f(g) != 0)
        return w, n
    sw, sn = hit(A, lambda g: (g["y"] - g["M"]) * (1 if g["R"] > g["M"] else -1))
    tw, tn = hit(A, lambda g: (g["tot"] - g["ou"]) * (1 if g["Tm"] > g["ou"] else -1))
    res.update(spread_lean=(sw, sn), total_lean=(tw, tn))
    out(f"model-lean SIDE covers the posted spread : {fmt_rate(sw, sn)}")
    out(f"model-lean SIDE of the total             : {fmt_rate(tw, tn)}")
    # scoring: cover probability before vs after
    def score(rows, pfun_s, pfun_t):
        Ls, Bs, Lt, Bt, n_s, n_t = [], [], [], [], 0, 0
        for g in rows:
            if g["y"] != g["M"]:
                o = 1 if g["y"] > g["M"] else 0
                p = pfun_s(g)
                Ls.append(ll(p, o)); Bs.append((p - o) ** 2)
            if g["tot"] != g["ou"]:
                o = 1 if g["tot"] > g["ou"] else 0
                p = pfun_t(g)
                Lt.append(ll(p, o)); Bt.append((p - o) ** 2)
        return Ls, Bs, Lt, Bt
    old_s = lambda g: NE.clamp_p(NE.cover_prob(g["R"], SIGMA_MARGIN, g["M"]))
    old_t = lambda g: NE.clamp_p(NE.cover_prob(g["Tm"], OLD_SIGMA_TOTAL, g["ou"]))
    new_s = lambda g: NE.spread_cover(g["R"], g["M"], g["nmin"])
    new_t = lambda g: NE.over_prob(g["Tm"], g["ou"], g["nmin"])
    half = lambda g: 0.5
    out("cover-probability scoring on the rated games (home covers / over; pushes excluded; lower is better; a coin flip is log-loss 0.6931, Brier 0.2500):")
    tab = {}
    for name, ps, pt in (("OLD raw model (sigma_total 10)", old_s, old_t), ("NEW anchored (w ramp, sigma_total 13)", new_s, new_t), ("posted line only (p = .5)", half, half)):
        Ls, Bs, Lt, Bt = score(A, ps, pt)
        tab[name] = (Ls, Bs, Lt, Bt)
        out(f"  {name:42s} SPREAD log-loss {st.mean(Ls):.4f} Brier {st.mean(Bs):.4f} (n={len(Ls)}) | TOTAL log-loss {st.mean(Lt):.4f} Brier {st.mean(Bt):.4f} (n={len(Lt)})")
    # paired bootstrap of the improvement old - new (positive = new better)
    for mkt, i in (("SPREAD", 0), ("TOTAL", 2)):
        d = [a - b for a, b in zip(tab["OLD raw model (sigma_total 10)"][i], tab["NEW anchored (w ramp, sigma_total 13)"][i])]
        lo_, hi_ = boot_ci(d, st.mean)
        out(f"  log-loss improvement old->new, {mkt}: {st.mean(d):+.4f} per game, bootstrap 95% CI [{lo_:+.4f}, {hi_:+.4f}] (n={len(d)})")
        res[f"ll_gain_{mkt.lower()}"] = (st.mean(d), lo_, hi_)
    # stated confidence on the picked side vs what happened
    def calib(pf_s, pf_t, label):
        ws = n_ = 0; stated = 0.0
        for g in A:
            for ps, f in ((pf_s(g), (g["y"] - g["M"])), (pf_t(g), (g["tot"] - g["ou"]))):
                if f == 0:
                    continue
                side_p = max(ps, 1 - ps)
                won = (f > 0) == (ps >= 0.5)
                ws += won; n_ += 1; stated += side_p
        out(f"  {label:42s} mean stated probability of the favoured side {100 * stated / n_:.1f}% vs realised {fmt_rate(ws, n_)}")
        return ws / n_, stated / n_
    out("calibration of the favoured side (spread + total together):")
    calib(old_s, old_t, "OLD raw model")
    calib(new_s, new_t, "NEW anchored")
    # sensitivity of the slope weight
    out("sensitivity: constant weight w on (model - line), spread cover log-loss (n=%d):" % len(A))
    for w in (0.0, 0.1, 0.2, 0.3, 0.5, 1.0):
        L = [ll(NE.clamp_p(NE.cover_prob(g["M"] + w * (g["R"] - g["M"]), SIGMA_MARGIN, g["M"])), 1 if g["y"] > g["M"] else 0) for g in A if g["y"] != g["M"]]
        out(f"   w={w:.1f}  {st.mean(L):.4f}")
    # moneyline reconstruction (information only: the live moneyline is NOT changed)
    ml_raw = [ll(phi(g["R"] / SIGMA_MARGIN), 1 if g["y"] > 0 else 0) for g in A if g["y"] != 0]
    ml_mkt = [ll(phi(g["M"] / SIGMA_MARGIN), 1 if g["y"] > 0 else 0) for g in A if g["y"] != 0]
    res["ml_raw_ll"], res["ml_mkt_ll"] = st.mean(ml_raw), st.mean(ml_mkt)
    out(f"home-win log-loss from the raw rating alone {st.mean(ml_raw):.3f} vs from the posted spread {st.mean(ml_mkt):.3f} (information only; the live moneyline also blends a record prior, is calibrated, and is unchanged)")
    return res


# ── LANE evidence: the alternate-line cushion on closing lines ───────────────────────────────────────────────────────────────────────────────────────
def lane_report(games: list[dict], out=print) -> dict:
    res: dict = {}
    out("cushion curve on the pre-kickoff lines (BOTH sides of every game, so the side is irrelevant):")
    out("   k   SPREAD observed                       curve Phi(k/13.4)   TOTAL observed                        curve Phi(k/13.0)")
    for k in (0, 2, 3, 4, 5, 6, 7, 8, 10):
        ws = ns = wt = nt = 0
        for g in games:
            for d in ((g["y"] - g["M"]) + k, -(g["y"] - g["M"]) + k):
                if d != 0:
                    ns += 1; ws += d > 0
            for d in ((g["tot"] - g["ou"]) + k, -(g["tot"] - g["ou"]) + k):
                if d != 0:
                    nt += 1; wt += d > 0
        out(f"  {k:2d}   {fmt_rate(ws, ns):36s} {100 * phi(k / 13.4):5.1f}%            {fmt_rate(wt, nt):36s} {100 * phi(k / 13.0):5.1f}%")
        res[k] = (ws, ns, wt, nt)
    return res


# ── LEDGER diagnosis ─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
def load_nfl_ledger(root: Path = ROOT) -> list[dict]:
    picks = json.loads((root / "docs" / "picks_backup.json").read_text())
    idx = lt.load_index(root)
    rows = []
    for p in picks:
        if lt.norm_sport(p) != "NFL" or p.get("outcome") not in ("win", "loss") or lt.is_parlay(p):
            continue
        p = dict(p)
        p["_cls"] = lt.classify(p, idx)
        p["_late"] = lt.is_known_late(p, idx)
        rows.append(p)
    return rows


def cohort(p: dict) -> str:
    d = p.get("date") or ""
    return "wk1 (09-10..14)" if d <= "2026-09-14" else "wk2 (09-15..21)" if d <= "2026-09-21" else "wk3 (09-22..28)" if d <= "2026-09-28" else "wk4+ (10-01..)"


def table(name: str, rows: list[dict], key, out=print) -> None:
    out(f"--- {name}")
    d: dict = {}
    for r in rows:
        d.setdefault(key(r), []).append(r)
    for k in sorted(d, key=str):
        rs = d[k]
        w = sum(1 for r in rs if r["outcome"] == "win")
        out(f"  {str(k):30s} n={len(rs):3d}  {fmt_rate(w, len(rs)):28s} mean stated {100 * st.mean(r['winProb'] for r in rs):.1f}%")


def ledger_report(games: list[dict], out=print) -> dict:
    n = load_nfl_ledger()
    pre = [r for r in n if not r["_late"]]
    out(f"settled NFL picks: {len(n)} ({fmt_rate(sum(r['outcome'] == 'win' for r in n), len(n))}); known-late locks (during/after the game) {len(n) - len(pre)}; pre-start: {fmt_rate(sum(r['outcome'] == 'win' for r in pre), len(pre))}")
    table("ALL by type", n, lambda r: r["betType"], out)
    table("PRE-START by type", pre, lambda r: r["betType"], out)
    table("lock class", n, lambda r: r["_cls"], out)
    table("pre-start cohort x type", pre, lambda r: (cohort(r), r["betType"]), out)
    table("pre-start alternate-line vs posted-line", pre, lambda r: ("ALT " if r.get("altLine") else "posted ") + r["betType"], out)
    nonml = [r for r in pre if r["betType"] != "ML" and not r.get("altLine")]
    table("posted-line spread/O-U by stated band", nonml, lambda r: ">=.80" if r["winProb"] >= .8 else ".70-.80" if r["winProb"] >= .7 else ".60-.70" if r["winProb"] >= .6 else "<.60", out)
    by_game = {}
    for g in games:
        by_game.setdefault((g["home"], g["away"]), []).append(g)

    def game_of(p):
        for c in by_game.get((p.get("hA"), p.get("awA")), []):
            d = datetime.fromtimestamp(c["st"], timezone.utc)
            pd = datetime.strptime(p["date"], "%Y-%m-%d").replace(tzinfo=timezone.utc)
            if abs((d - pd).days) <= 2:
                return c
        return None
    # regrade every pick from the final scores; list disagreements with the stored outcome
    mism = []
    for p in n:
        g = game_of(p)
        if not g:
            continue
        bt, bo = p["betType"], p.get("betOn") or ""
        v = None
        if bt == "SPREAD":
            m = re.match(r"(\w+)\s*([+-][\d.]+)", bo)
            if m:
                mar = g["y"] if m.group(1) == g["home"] else -g["y"]
                v = mar + float(m.group(2))
        elif bt == "OU":
            m = re.match(r"(OVER|UNDER)\s*([\d.]+)", bo)
            if m:
                v = (g["tot"] - float(m.group(2))) if m.group(1) == "OVER" else (float(m.group(2)) - g["tot"])
        elif bt == "ML":
            team = bo.replace(" ML", "").strip()
            v = g["y"] if team == g["home"] else -g["y"]
        if v is not None and v != 0 and (v > 0) != (p["outcome"] == "win"):
            mism.append((p["id"], p["outcome"], bo, f"final {g['as_']}-{g['hs']}", p["winProb"]))
    out(f"--- ledger outcomes that disagree with the final score in nfl_schedule.json history: {mism if mism else 'none'}")
    # counterfactual: the pre-start posted-line spread/O-U picks, same side, locked at a +6 cushion instead
    cw = cn = 0
    for p in nonml:
        g = game_of(p)
        if not g:
            continue
        bt, bo = p["betType"], p.get("betOn") or ""
        if bt == "SPREAD":
            m = re.match(r"(\w+)\s*([+-][\d.]+)", bo)
            mar = g["y"] if m.group(1) == g["home"] else -g["y"]
            v = mar + float(m.group(2)) + 6
        else:
            m = re.match(r"(OVER|UNDER)\s*([\d.]+)", bo)
            v = ((g["tot"] - float(m.group(2))) if m.group(1) == "OVER" else (float(m.group(2)) - g["tot"])) + 6
        if v != 0:
            cn += 1; cw += v > 0
    pw = sum(1 for r in nonml if r["outcome"] == "win")
    out(f"--- the {len(nonml)} pre-start posted-line spread/O-U picks: as locked {fmt_rate(pw, len(nonml))} (mean stated {100 * st.mean(r['winProb'] for r in nonml):.1f}%); "
        f"same sides at a +6-point cushion (the alternate-line lane): {fmt_rate(cw, cn)} (stated ~68%)")
    return {"n": len(n), "pre": len(pre), "posted_nonml": (pw, len(nonml)), "posted_nonml_at_cushion6": (cw, cn), "mismatch": mism}


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    what = argv[0] if argv else "all"
    if what == "--refresh-archive":
        print(f"archived {refresh_archive()} games -> {ARCHIVE}")
        return 0
    games = build_games(load_archive())
    if len(games) < 30:
        print(f"only {len(games)} archived games -- run with --refresh-archive from a full (non-shallow) clone first")
        return 1
    if what in ("all", "ledger"):
        print("=== LEDGER ==="); ledger_report(games)
    if what in ("all", "anchor"):
        print("\n=== MODEL VS MARKET ==="); anchor_report(games)
    if what in ("all", "lane"):
        print("\n=== ALTERNATE-LINE CUSHION ==="); lane_report(games)
    return 0


if __name__ == "__main__":
    sys.exit(main())
