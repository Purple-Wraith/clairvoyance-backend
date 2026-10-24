#!/usr/bin/env python3
"""Calibration data for the alternate-line shift (ALT_LINE_CFG in scripts/auto_lock_settle.py) on NBA and NHL.

  python3 scripts/backtest_alt_lines.py nhl     # NHL totals: win rate of OVER L-1 / UNDER L+1 against the closing total L
  python3 scripts/backtest_alt_lines.py nba     # NBA totals + spreads: win rate vs cushion k, fitted sigma
  python3 scripts/backtest_alt_lines.py euro    # LIIGA / SHL / NLA / EXTRALIGA: each league's OWN total-goals distribution (2025-26 regular season + 2026-27 so far)
  python3 scripts/backtest_alt_lines.py euro-archive [--refresh]   # same, but the REAL totals CDF scraped from Flashscore's per-team Over/Under standings tabs + NHL method check

Closing DraftKings / ESPN BET lines and final scores come from ESPN's public site + core APIs (the NHL half reuses
backtest_hockey_models.load_odds). Read-only; nothing touches the ledger or Supabase. Cached under --cache so reruns are free.

The ledger itself cannot calibrate this: it holds no settled NBA totals/spreads and only ~15 NHL totals. The question answered here is market-neutral --
given a posted closing line L, how often does the side win at L +/- k -- which is what the cushion curve needs (the stated model probability carried no
margin information for CFB/NFL, see the alt-line notes in auto_lock_settle.py).
"""
from __future__ import annotations

import argparse
import json
import math
import re
import statistics
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import backtest_hockey_models as H  # noqa: E402


def load_nba(cache: Path) -> list[dict]:
    f = cache / "nba_odds_hist.json"
    if f.exists():
        return json.loads(f.read_text())
    seasons = {"2024-25": (date(2024, 10, 22), date(2025, 4, 13)), "2025-26": (date(2025, 10, 21), date(2026, 4, 12))}

    def day(d):
        j = H._get("https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard",
                   {"dates": d.strftime("%Y%m%d"), "limit": 50})
        out = []
        for e in (j or {}).get("events", []):
            c = e["competitions"][0]
            if c["status"]["type"]["state"] != "post" or (e.get("season") or {}).get("type") != 2:
                continue
            hm = [x for x in c["competitors"] if x["homeAway"] == "home"][0]
            aw = [x for x in c["competitors"] if x["homeAway"] == "away"][0]
            out.append(dict(eid=e["id"], date=e["date"], home=hm["team"]["abbreviation"], away=aw["team"]["abbreviation"],
                            hs=int(hm["score"]), as_=int(aw["score"]), period=c["status"].get("period")))
        return out

    def odds(eid):
        j = H._get(f"https://sports.core.api.espn.com/v2/sports/basketball/leagues/nba/events/{eid}/competitions/{eid}/odds")
        items = (j or {}).get("items") or []
        pre = [i for i in items if "live" not in (i.get("provider") or {}).get("name", "").lower()]
        if not pre:
            return None
        it = pre[-1]
        for i in pre:
            if (i.get("provider") or {}).get("name", "").lower().startswith("draft"):
                it = i
        return dict(ou=it.get("overUnder"), spread=it.get("spread"))

    out = []
    for s, (a, b) in seasons.items():
        days, d = [], a
        while d <= b:
            days.append(d)
            d += timedelta(days=1)
        with ThreadPoolExecutor(8) as ex:
            games = [g for l in ex.map(day, days) for g in l]
        with ThreadPoolExecutor(8) as ex:
            for g, o in zip(games, ex.map(lambda g: odds(g["eid"]), games)):
                g["odds"] = o
                g["season"] = s
        print(f"  nba {s}: {len(games)} games", file=sys.stderr)
        out.extend(games)
    cache.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(out))
    return out


EURO = ("liiga", "shl", "nla", "extraliga")
DOCS = Path(__file__).resolve().parent.parent / "docs"
EURO_PRIOR_N = 60       # pseudo-games of weight given to the league's own Poisson (two-season scoring mean) next to this season's finished games


def _pois(k: int, mu: float) -> float:
    return math.exp(-mu + k * math.log(mu) - math.lgamma(k + 1))


# ───────────────────────── soccer (PL / La Liga / Serie A / Champions League) ─────────────────────────
# Closing DraftKings totals + handicaps and 90-minute results from ESPN's public APIs, two seasons (2024-25, 2025-26).  Answers the question the cushion curves need, market-neutral:
# given a posted line, how often does the side win at the line moved k goals toward safety.
SOC_LEAGUES = {"PL": "eng.1", "LIGA": "esp.1", "SERIEA": "ita.1", "CL": "uefa.champions"}
SOC_SEASONS = {"2024-25": (date(2024, 8, 10), date(2025, 6, 1)), "2025-26": (date(2025, 8, 10), date(2026, 5, 31))}


def load_soccer(cache: Path) -> list[dict]:
    f = cache / "soccer_odds_hist.json"
    if f.exists():
        return json.loads(f.read_text())
    cache.mkdir(parents=True, exist_ok=True)
    out: list[dict] = []
    for lg, code in SOC_LEAGUES.items():
        for season, (a, b) in SOC_SEASONS.items():
            days, d = [], a
            while d <= b:
                days.append(d)
                d += timedelta(days=1)

            def day(d, code=code, lg=lg, season=season):
                j = H._get(f"https://site.api.espn.com/apis/site/v2/sports/soccer/{code}/scoreboard", {"dates": d.strftime("%Y%m%d"), "limit": 100})
                res = []
                for e in (j or {}).get("events", []):
                    c = e["competitions"][0]
                    st = c["status"]["type"]
                    if st.get("name") != "STATUS_FULL_TIME":          # 90-minute results only (no extra time / penalties, no postponed)
                        continue
                    hm = [x for x in c["competitors"] if x["homeAway"] == "home"][0]
                    aw = [x for x in c["competitors"] if x["homeAway"] == "away"][0]
                    res.append(dict(lg=lg, season=season, eid=e["id"], code=code, date=e["date"], home=hm["team"]["displayName"], away=aw["team"]["displayName"], hs=int(hm["score"]), as_=int(aw["score"])))
                return res

            with ThreadPoolExecutor(6) as ex:
                games = [g for l in ex.map(day, days) for g in l]
            print(f"  {lg} {season}: {len(games)} finished games", file=sys.stderr)

            def odds(g):
                j = H._get(f"https://sports.core.api.espn.com/v2/sports/soccer/leagues/{g['code']}/events/{g['eid']}/competitions/{g['eid']}/odds")
                items = (j or {}).get("items") or []
                pre = [i for i in items if "live" not in (i.get("provider") or {}).get("name", "").lower() and i.get("overUnder") is not None]
                if not pre:
                    return None
                it = pre[0]
                for i in pre:
                    if (i.get("provider") or {}).get("name", "").lower().startswith("draft"):
                        it = i
                return dict(ou=it.get("overUnder"), overOdds=it.get("overOdds"), underOdds=it.get("underOdds"), spread=it.get("spread"),
                            homeFav=(it.get("homeTeamOdds") or {}).get("favorite"), awayFav=(it.get("awayTeamOdds") or {}).get("favorite"),
                            hML=(it.get("homeTeamOdds") or {}).get("moneyLine"), aML=(it.get("awayTeamOdds") or {}).get("moneyLine"), dML=(it.get("drawOdds") or {}).get("moneyLine"))

            with ThreadPoolExecutor(6) as ex:
                for g, o in zip(games, ex.map(odds, games)):
                    g["odds"] = o
            out += games
    f.write_text(json.dumps(out))
    return out


def over_under(p: dict, x: float) -> tuple[float, float]:
    return sum(v for t, v in p.items() if t > x), sum(v for t, v in p.items() if t < x)


def table_from(p: dict) -> tuple[float, dict]:
    """Shift-invariant table: anchor L0 = the half-line whose OVER probability is closest to 50%; j<0 -> P(OVER L0+j), j>0 -> P(UNDER L0+j)."""
    l0 = min((x + 0.5 for x in range(2, 10)), key=lambda x: abs(over_under(p, x)[0] - 0.5))
    tab = {}
    for j in (-3, -2, -1, 1, 2, 3):
        o, u = over_under(p, l0 + j)
        tab[j] = o if j < 0 else u
    return l0, tab


def cmd_euro(cache: Path) -> None:
    """Each European league's OWN totals model: this season's finished games blended with a Poisson at the league's own two-season mean total
    (last season + this season so far, from the teams' gf/gp in docs/<league>_schedule.json). No NHL numbers are borrowed. The archive results page of last
    season only lists the playoffs on Flashscore, so last season enters through the team averages, not game by game."""
    for lg in EURO:
        d = json.loads((DOCS / f"{lg}_schedule.json").read_text())
        teams = list(d["teams"].values())
        cur_gp = sum(t["gp"] for t in teams) / 2
        cur_goals = sum(t["gf"] for t in teams)
        prv = [t["prevSeason"] for t in teams if t.get("prevSeason")]
        prv_gp = sum(t["gp"] for t in prv) / 2
        prv_goals = sum(t["gf"] for t in prv)
        mean = (cur_goals + prv_goals) / (cur_gp + prv_gp)
        fin = [g["homeScore"] + g["awayScore"] for g in d["games"] if g.get("state") == "post" and g.get("homeScore") is not None]
        n = len(fin)
        emp = defaultdict(float)
        for t in fin:
            emp[t] += 1.0 / n
        pm = {k: (n * emp.get(k, 0.0) + EURO_PRIOR_N * _pois(k, mean)) / (n + EURO_PRIOR_N) for k in range(0, 25)}
        l0, tab = table_from(pm)
        print(f"{lg:10s} two-season mean total {mean:.2f} (last season {prv_goals / prv_gp:.2f} over {prv_gp:.0f} games, this season {cur_goals / cur_gp:.2f} over {cur_gp:.0f}); "
              f"finished games n={n} mean {statistics.mean(fin):.2f} sd {statistics.pstdev(fin):.2f}")
        print(f"   anchor {l0}: " + "  ".join(f"{j:+d}:{tab[j]:.3f}" for j in tab))
        print(f"   table = {json.dumps({str(j): round(v, 3) for j, v in tab.items()})}\n")


# --- euro-archive: real totals distributions from Flashscore's per-team Over/Under standings tabs ---------------------------------------------------
# Flashscore standings -> Over/Under tab: per team, MP / games OVER / games UNDER a chosen goal threshold (URL ends .../over_under/overall/<x.5>/).
# Every game involves two teams, so league-wide P(total > x) = sum(over) / sum(MP) over teams -- the full empirical CDF of regular-season totals
# even though the archive /results/ page lists only the playoffs. Stage ids (CMVpiF7T etc.) are the ones the fetch_*.py scripts already use.
FS = "https://www.flashscore.com/hockey"
ARCHIVE = {  # league -> (country/slug path, last-season stage id, current-season stage id)
    "liiga": ("finland/liiga", "SCI7qRwB", "C8KZXayI"),
    "shl": ("sweden/shl", "CMVpiF7T", "tKxnwsZa"),
    "nla": ("switzerland/national-league", "YwJVrFRr", "UmJLocZR"),
    "extraliga": ("czech-republic/extraliga", "K0tmQWEr", "WOgC3lWQ"),
}
ARCHIVE_THRESH = [x + 0.5 for x in range(0, 12)]     # 0.5 .. 11.5 ; the page only offers 0.5..8.5 -- an unknown threshold silently falls back to the default 5.5 table, so
#                                                     # _scrape_ou checks the threshold the page actually selected and the higher ones are dropped
_TEAM_ID_RE = re.compile(r"/team/([a-z0-9-]+)/([A-Za-z0-9]+)/?")


def _scrape_ou(page, url: str) -> dict:
    """{teamId: [mp, over, under]} from one Over/Under standings page (same selectors as fetch_liiga.fetch_gm_rates). {} when the page has no table."""
    page.goto(url, wait_until="networkidle", timeout=45000)
    want = url.rstrip("/").rsplit("/", 1)[-1]
    if page.url.rstrip("/").rsplit("/", 1)[-1] != want:      # redirected to the default threshold: this one does not exist
        return {}
    try:
        page.wait_for_selector(".table__cell--value", timeout=10000)
    except Exception:
        pass
    page.wait_for_timeout(800)
    out = {}
    for row in page.query_selector_all("[class*='ui-table__row']"):
        link = row.query_selector(".table__cell--participant a")
        m = _TEAM_ID_RE.search((link.get_attribute("href") if link else "") or "")
        if not m:
            continue
        plain = row.query_selector_all(".table__cell--value:not(.table__cell--over):not(.table__cell--under):not(.table__cell--score)")
        ov, un = row.query_selector(".table__cell--over"), row.query_selector(".table__cell--under")
        if not plain or not ov or not un:
            continue
        try:
            out[m.group(2)] = [int(plain[0].inner_text().strip()), int(ov.inner_text().strip()), int(un.inner_text().strip())]
        except ValueError:
            continue
    return out


def load_archive(cache: Path, refresh: bool = False) -> dict:
    """{league: {"prev": {thr: {team: [mp, over, under]}}, "cur": {...}}}, cached per league/season under cache/euro_archive_<league>_<prev|cur>.json."""
    from playwright.sync_api import sync_playwright
    res: dict = {}
    todo = [(lg, s) for lg in EURO for s in ("prev", "cur") if refresh or not (cache / f"euro_archive_{lg}_{s}.json").exists()]
    if todo:
        cache.mkdir(parents=True, exist_ok=True)
        with sync_playwright() as p:
            br = p.chromium.launch()
            page = br.new_context(timezone_id="UTC").new_page()
            for lg, s in todo:
                path, prev_id, cur_id = ARCHIVE[lg]
                base = f"{FS}/{path}-2025-2026" if s == "prev" else f"{FS}/{path}"
                sid = prev_id if s == "prev" else cur_id
                data = {}
                for thr in ARCHIVE_THRESH:
                    url = f"{base}/standings/{sid}/over_under/overall/{thr}/"
                    for attempt in range(3):
                        try:
                            rows = _scrape_ou(page, url)
                            if rows:
                                break
                        except Exception as e:  # noqa: BLE001
                            print(f"  {lg} {s} {thr}: attempt {attempt + 1} failed: {e}", file=sys.stderr)
                    n_g = sum(v[0] for v in rows.values()) / 2 if rows else 0
                    print(f"  {lg} {s} thr {thr}: {len(rows)} teams, {n_g:.0f} games, over {sum(v[1] for v in rows.values())}", file=sys.stderr)
                    if rows:
                        data[str(thr)] = rows
                (cache / f"euro_archive_{lg}_{s}.json").write_text(json.dumps({"base": base, "stage": sid, "thr": data}))
            br.close()
    for lg in EURO:
        res[lg] = {s: json.loads((cache / f"euro_archive_{lg}_{s}.json").read_text()) for s in ("prev", "cur")}
    return res


def survival(thr: dict) -> tuple[float, dict]:
    """(games, {x: P(total > x)}) pooled over teams from {thr: {team: [mp, over, under]}}. Games = sum(MP)/2; P = sum(over)/sum(MP)."""
    S, mp = {}, None
    for x, rows in thr.items():
        tot_mp = sum(v[0] for v in rows.values())
        S[float(x)] = sum(v[1] for v in rows.values()) / tot_mp
        mp = tot_mp if mp is None else max(mp, tot_mp)
    return mp / 2, S


def pmf_from_survival(S: dict, tmax: int = 25) -> tuple[dict, list[str]]:
    """pmf of integer totals from S[x] = P(total > x), x = 0.5, 1.5, ... (P(total >= x+0.5)). S(-0.5) = 1. pmf(t) = S(t-0.5) - S(t+0.5).
    Thresholds the page does not offer (above the highest one) are extrapolated geometrically from the last two observed ones. Returns (pmf, notes)."""
    notes = []
    xs = sorted(S)
    top = xs[-1]
    r = min(0.9, S[top] / S[xs[-2]]) if len(xs) > 1 and S[xs[-2]] > 0 else 0.5
    full = {-0.5: 1.0, **S}
    x = top
    while x + 1 <= tmax - 0.5:
        x += 1
        full[x] = full[x - 1] * r
    if x > top:
        notes.append(f"S above {top} extrapolated geometrically (ratio {r:.3f}); S({top + 1}) = {full[top + 1]:.4f}")
    prev = 1.0
    for xx in sorted(full):
        if full[xx] > prev + 1e-12:
            notes.append(f"non-monotone survival at {xx}: {full[xx]:.4f} > {prev:.4f}")
        prev = full[xx]
    pm = {t: max(0.0, full[t - 0.5] - full[t + 0.5]) for t in range(0, tmax)}
    return pm, notes


def table_at(p: dict, l0: float) -> dict:
    """Same table as table_from() but at a FIXED anchor (e.g. 5.5, the line most European hockey books post) instead of the 50%-closest one."""
    return {j: (over_under(p, l0 + j)[0] if j < 0 else over_under(p, l0 + j)[1]) for j in (-3, -2, -1, 1, 2, 3)}


# REGULATION -> BOOK BASIS (2026-10-24).  Flashscore's Over/Under standings count REGULATION goals only (verified: 128/128 NHL 2025-26 team x line cells; NLA and Extraliga 56/56 each), but sportsbooks settle
# totals on regulation + OVERTIME (the shootout goal excluded), and so does this app's settler.  A game level after 60 minutes has an EVEN regulation total R; if overtime decides it the book total is R+1,
# if the shootout decides it, R.  So moving the OT-decided mass from R to R+1 turns a regulation pmf into the book-basis pmf.  OT_TIED[R] = share of games with regulation total R that finished level AND were
# decided in overtime: NHL 2022-23..2025-26, 5,248 games (scripts/backtest_hockey_models.py results cache); stable by season (R=4: .343 .297 .311 .348).  The same share is used for the four European leagues: their
# measured overtime / shootout split of level games (54-74% in OT, NHL 63%) is close, and the tie probability depends on the total, not the league.
OT_TIED = {0: 0.647, 2: 0.460, 4: 0.323, 6: 0.267, 8: 0.202, 10: 0.196}


def to_book_basis(pm: dict) -> dict:
    out = dict(pm)
    for r, f in OT_TIED.items():
        m = pm.get(r, 0.0) * f
        out[r] = out.get(r, 0.0) - m
        out[r + 1] = out.get(r + 1, 0.0) + m
    return out


def _fmt_tab(l0: float, tab: dict) -> str:
    return f"anchor {l0}: " + "  ".join(f"{j:+d}:{tab[j]:.3f}" for j in tab)


def cmd_euro_archive(cache: Path, refresh: bool = False) -> None:
    arch = load_archive(cache, refresh)
    for lg in EURO:
        print(f"=== {lg} ===")
        sets, pooled = {}, {}
        for s, label in (("prev", "2025-26 regular season"), ("cur", "2026-27 so far")):
            thr = arch[lg][s]["thr"]
            n, S = survival(thr)
            # sanity: per threshold, over+under == MP for every team and the team set / MP identical across thresholds
            bad = [x for x, rows in thr.items() if any(v[1] + v[2] != v[0] for v in rows.values())]
            mps = {x: sum(v[0] for v in rows.values()) for x, rows in thr.items()}
            teams = {x: len(rows) for x, rows in thr.items()}
            pm, notes = pmf_from_survival(S)
            mean = sum(t * q for t, q in pm.items())
            sets[s] = (n, S, pm)
            pooled[s] = thr
            print(f"[{label}] n={n:.0f} games ({len(next(iter(thr.values())))} teams; thresholds {min(map(float, thr))}..{max(map(float, thr))}); pmf sums to {sum(pm.values()):.4f}; mean total {mean:.3f}")
            if bad or len(set(mps.values())) > 1 or len(set(teams.values())) > 1:
                print(f"   WARNING: over+under!=MP at {bad}; MP by thr {mps}; teams by thr {teams}")
            for nt in notes:
                print(f"   note: {nt}")
            print("   P(over): " + "  ".join(f"{x}:{S[x]:.3f}" for x in (3.5, 4.5, 5.5, 6.5, 7.5) if x in S))
            l0, tab = table_from(pm)
            print(f"   {_fmt_tab(l0, tab)}")
            print(f"   table = {json.dumps({str(j): round(v, 3) for j, v in tab.items()})}")
            if l0 != 5.5:
                print(f"   (fixed anchor) {_fmt_tab(5.5, table_at(pm, 5.5))}")
        # combined: pool the two seasons' games (weights = games played)
        (n1, S1, _), (n2, S2, _) = sets["prev"], sets["cur"]
        Sc = {x: (n1 * S1[x] + n2 * S2[x]) / (n1 + n2) for x in S1 if x in S2}
        pm, notes = pmf_from_survival(Sc)
        mean = sum(t * q for t, q in pm.items())
        l0, tab = table_from(pm)
        print(f"[combined, n={n1 + n2:.0f} games, weights {n1 / (n1 + n2):.0%}/{n2 / (n1 + n2):.0%}] mean total {mean:.3f}   (regulation basis, as Flashscore counts)")
        print(f"   {_fmt_tab(l0, tab)}")
        print(f"   table = {json.dumps({str(j): round(v, 3) for j, v in tab.items()})}")
        pb = to_book_basis(pm)
        l0b, tabb = table_from(pb)
        fixed = table_at(pb, 5.5)
        print(f"   BOOK BASIS (regulation + OT, shootout goal excluded): mean total {sum(t * q for t, q in pb.items()):.3f}; {_fmt_tab(l0b, tabb)}")
        print(f"   book table anchored at 5.5 = {json.dumps({str(j): round(v, 3) for j, v in fixed.items()})}   <- HOCKEY_OU_EURO[{lg.upper()!r}]\n")


def cmd_nhl_check(cache: Path) -> None:
    """Sanity check of the shift-invariant method on the NHL: build an UNCONDITIONAL totals pmf (all games, shootout goal removed), take table_from(), and compare to the
    conditional closing-line hit rates cmd_nhl measures (posted 5.5 -> OVER4.5 / UNDER6.5; posted 6.5 -> OVER5.5 / UNDER7.5)."""
    odds = json.loads((cache / "nhl_odds_hist.json").read_text())
    tots = [g["hs"] + g["as_"] - (1 if g.get("period") == 5 else 0) for gs in odds.values() for g in gs]
    n = len(tots)
    pm = {k: sum(1 for t in tots if t == k) / n for k in range(0, 25)}
    print(f"NHL unconditional totals (shootout goal removed): n={n} mean {statistics.mean(tots):.3f} sd {statistics.pstdev(tots):.3f}")
    l0, tab = table_from(pm)
    print(f"   {_fmt_tab(l0, tab)}")
    known = {5.5: (0.731, 0.611), 6.5: (0.589, 0.736)}
    for L, (ko, ku) in known.items():
        o = over_under(pm, L - 1)[0]
        u = over_under(pm, L + 1)[1]
        print(f"   posted {L}: unconditional OVER {L - 1} {o:.3f} vs conditional {ko:.3f} (diff {o - ko:+.3f});  UNDER {L + 1} {u:.3f} vs conditional {ku:.3f} (diff {u - ku:+.3f})")


def ncdf(z: float) -> float:
    return 0.5 * (1 + math.erf(z / math.sqrt(2)))


def cmd_nhl(cache: Path) -> None:
    odds = H.load_odds(cache)
    rows = []
    for s, games in odds.items():
        for g in games:
            o = g.get("odds") or {}
            if o.get("ou") is None:
                continue
            # ESPN's final score includes the shootout-winning goal; sportsbooks settle totals WITHOUT it (period 5 = shootout)
            rows.append((float(o["ou"]), g["hs"] + g["as_"] - (1 if g.get("period") == 5 else 0), g.get("period")))
    print(f"NHL games with a closing total: {len(rows)}")
    by = defaultdict(list)
    for line, tot, _ in rows:
        by[line].append(tot)
    print("\nclosing total -> n, mean total, sd")
    for line in sorted(by):
        if len(by[line]) >= 30:
            print(f"  {line}: n={len(by[line])} mean {statistics.mean(by[line]):.2f} sd {statistics.pstdev(by[line]):.2f}")
    print("\nWin rate of the shifted total vs the closing total L (side-neutral, all games with L in 5.5/6/6.5):")
    for L in (5.5, 6.0, 6.5):
        tots = by.get(L) or []
        if len(tots) < 30:
            continue
        n = len(tots)
        print(f"  L={L} n={n}")
        for k in (0.0, 0.5, 1.0, 1.5):
            ov = sum(1 for t in tots if t > L - k) / n   # OVER L-k
            un = sum(1 for t in tots if t < L + k) / n   # UNDER L+k
            ps_o = sum(1 for t in tots if t == L - k) / n
            ps_u = sum(1 for t in tots if t == L + k) / n
            print(f"     k={k}: OVER {L-k} wins {ov:.1%} (push {ps_o:.1%})   UNDER {L+k} wins {un:.1%} (push {ps_u:.1%})")


def cmd_soccer(cache: Path) -> None:
    """Side-neutral cushion tables for soccer totals and Asian handicaps from closing lines: win rate of the side moved k goals toward safety vs the posted half-point line."""
    games = [g for g in load_soccer(cache) if g.get("odds") and g["odds"].get("ou") is not None]
    print(f"soccer games with a closing total: {len(games)}  ({', '.join(f'{lg} {sum(1 for g in games if g['lg'] == lg)}' for lg in SOC_LEAGUES)})")
    half = lambda x: x is not None and abs(x * 2 - round(x * 2)) < 1e-9 and abs(x - round(x)) > 1e-9
    # ── totals ──
    tot = [(float(g["odds"]["ou"]), g["hs"] + g["as_"]) for g in games if half(float(g["odds"]["ou"]))]
    by = defaultdict(list)
    for L, t in tot:
        by[L].append(t)
    print("\nposted total -> n, mean goals, P(over L):")
    for L in sorted(by):
        if len(by[L]) >= 30:
            print(f"  {L}: n={len(by[L])} mean {statistics.mean(by[L]):.2f} P(over) {sum(1 for t in by[L] if t > L) / len(by[L]):.3f}")
    print("\nTOTALS: win rate at the line moved k goals toward safety (OVER L-k / UNDER L+k), by posted line:")
    pooled = defaultdict(list)
    for L in sorted(by):
        ts = by[L]
        if len(ts) < 30:
            continue
        row = []
        for k in (0, 1, 2, 3):
            ov = sum(1 for t in ts if t > L - k) / len(ts)
            un = sum(1 for t in ts if t < L + k) / len(ts)
            row.append(f"k={k}: over {ov:.3f} under {un:.3f} avg {(ov + un) / 2:.3f}")
            pooled[k] += [((1 if t > L - k else 0) + (1 if t < L + k else 0)) / 2 for t in ts]
        print(f"  L={L} n={len(ts)}  " + " | ".join(row))
    print("  POOLED side-neutral:", "  ".join(f"{k}:{statistics.mean(v):.3f}" for k, v in sorted(pooled.items())), f"(n={len(pooled[0])})")
    # ── handicaps ──
    sp = []
    for g in games:
        o = g["odds"]
        if o.get("spread") is None or not half(float(o["spread"])):
            continue
        sp.append((float(o["spread"]), g["hs"] - g["as_"]))
    if sp:
        neg = [m for s_, m in sp if s_ < 0]; pos = [m for s_, m in sp if s_ > 0]
        print(f"\nHANDICAP orientation check: spread<0 n={len(neg)} home win rate {sum(1 for m in neg if m > 0) / max(1, len(neg)):.3f};  spread>0 n={len(pos)} home win rate {sum(1 for m in pos if m > 0) / max(1, len(pos)):.3f}")
        print("HANDICAPS (home-perspective spread, half-lines): win rate of the side moved k goals toward safety, by |spread|:")
        pooled = defaultdict(list)
        for ab in sorted({abs(s_) for s_, _ in sp}):
            rows = [(s_, m) for s_, m in sp if abs(s_) == ab]
            if len(rows) < 30:
                continue
            out = []
            for k in (0, 1, 2, 3):
                # home covers (home spread s): margin + s > 0 ; moved toward safety for the HOME side: margin + s + k > 0 ; for the AWAY side: -(margin + s) + k > 0
                hw = [1 if m + s_ + k > 0 else 0 for s_, m in rows]
                aw = [1 if -(m + s_) + k > 0 else 0 for s_, m in rows]
                out.append(f"k={k}: home {statistics.mean(hw):.3f} away {statistics.mean(aw):.3f} avg {(statistics.mean(hw) + statistics.mean(aw)) / 2:.3f}")
                pooled[k] += [(h + a) / 2 for h, a in zip(hw, aw)]
            print(f"  |spread|={ab} n={len(rows)}  " + " | ".join(out))
        print("  POOLED side-neutral:", "  ".join(f"{k}:{statistics.mean(v):.3f}" for k, v in sorted(pooled.items())), f"(n={len(pooled[0])})")


def cmd_nba(cache: Path) -> None:
    games = load_nba(cache)
    tot = [(float(g["odds"]["ou"]), g["hs"] + g["as_"]) for g in games if (g.get("odds") or {}).get("ou")]
    spr = []
    for g in games:
        o = g.get("odds") or {}
        if o.get("spread") is None:
            continue
        # ESPN "spread" is the HOME line (negative when the home team is favoured)
        spr.append((float(o["spread"]), g["hs"] - g["as_"]))
    print(f"NBA games with a closing total: {len(tot)}, with a spread: {len(spr)}")
    err_t = [t - l for l, t in tot]
    err_s = [m + s for s, m in spr]          # home margin + home line: >0 means the home side covered
    print(f"totals: mean err {statistics.mean(err_t):+.2f}  sd {statistics.pstdev(err_t):.2f}")
    print(f"spread: mean err {statistics.mean(err_s):+.2f}  sd {statistics.pstdev(err_s):.2f}")
    for name, err in (("TOTAL", err_t), ("SPREAD", err_s)):
        n = len(err)
        print(f"\n{name} cushion k -> win rate (side-neutral = average of the two directions)")
        for k in (0, 2, 3, 4, 5, 6, 7, 8, 10):
            w = sum(1 for e in err if e + k > 0) / n      # the 'over'/'home' direction with cushion k
            w2 = sum(1 for e in err if -e + k > 0) / n    # the opposite direction
            print(f"   k={k}: {(w + w2) / 2:.1%}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["nhl", "nba", "euro", "euro-archive", "soccer"])
    ap.add_argument("--cache", default="/tmp/cv_alt_bt")
    ap.add_argument("--refresh", action="store_true", help="euro-archive: re-scrape Flashscore instead of using the cached JSON")
    a = ap.parse_args()
    cache = Path(a.cache)
    if a.cmd == "euro-archive":
        cmd_euro_archive(cache, a.refresh)
        cmd_nhl_check(cache)
    elif a.cmd == "soccer":
        cmd_soccer(cache)
    else:
        {"nhl": cmd_nhl, "nba": cmd_nba, "euro": cmd_euro}[a.cmd](cache)
