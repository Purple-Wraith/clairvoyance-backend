#!/usr/bin/env python3
"""Calibration data for the alternate-line shift (ALT_LINE_CFG in scripts/auto_lock_settle.py) on NBA and NHL.

  python3 scripts/backtest_alt_lines.py nhl     # NHL totals: win rate of OVER L-1 / UNDER L+1 against the closing total L
  python3 scripts/backtest_alt_lines.py nba     # NBA totals + spreads: win rate vs cushion k, fitted sigma
  python3 scripts/backtest_alt_lines.py euro    # LIIGA / SHL / NLA / EXTRALIGA: each league's OWN total-goals distribution (2025-26 regular season + 2026-27 so far)

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


EURO = {  # league -> (fetch module, Flashscore archive slug of last season, regular-season games = sum of prevSeason gp / 2)
    "liiga": ("fetch_liiga", "finland/liiga-2025-2026"),
    "shl": ("fetch_shl", "sweden/shl-2025-2026"),
    "nla": ("fetch_nla", "switzerland/national-league-2025-2026"),
    "extraliga": ("fetch_extraliga", "czech-republic/extraliga-2025-2026"),
}
DOCS = Path(__file__).resolve().parent.parent / "docs"


def load_euro(cache: Path, league: str) -> list[dict]:
    """Final scores of last season's regular season, scraped from Flashscore's archive with the league fetcher's own row parser (cached)."""
    f = cache / f"euro_{league}_prev.json"
    if f.exists():
        return json.loads(f.read_text())
    import importlib
    from playwright.sync_api import sync_playwright
    mod = importlib.import_module(EURO[league][0])
    url = f"https://www.flashscore.com/hockey/{EURO[league][1]}/results/"
    rows: list[dict] = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_context(timezone_id="UTC").new_page()
        page.goto(url, wait_until="networkidle", timeout=45000)
        page.wait_for_timeout(1500)
        stall = 0
        last = -1
        for _ in range(400):
            n = len(page.query_selector_all("[class*='event__match']"))
            stall = stall + 1 if n == last else 0
            if stall >= 4:
                break
            last = n
            more = page.query_selector("a.event__more, .event__more")
            if more:
                try:
                    more.scroll_into_view_if_needed()
                    more.click()
                except Exception:
                    pass
            page.wait_for_timeout(1500)
        for row in page.query_selector_all("[class*='event__match']"):
            parsed = mod._extract_match_row(row)
            if not parsed or len(parsed["scores"]) < 2:
                continue
            iso = mod._date_txt_to_iso(parsed["dateTxt"], 2025)
            if iso:
                rows.append({"date": iso, "hs": int(parsed["scores"][0]), "as_": int(parsed["scores"][1])})
        browser.close()
    print(f"  {league}: {len(rows)} results scraped from {url}", file=sys.stderr)
    cache.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(rows))
    return rows


def pmf(totals: list[int]) -> dict[int, float]:
    n = len(totals)
    out: dict[int, float] = defaultdict(float)
    for t in totals:
        out[t] += 1.0 / n
    return dict(out)


def over_under(p: dict[int, float], x: float) -> tuple[float, float]:
    return sum(v for t, v in p.items() if t > x), sum(v for t, v in p.items() if t < x)


def table_from(p: dict[int, float]) -> tuple[float, dict[int, float]]:
    """Shift-invariant table: anchor L0 = the half-line whose OVER probability is closest to 50%; j<0 -> P(OVER L0+j), j>0 -> P(UNDER L0+j)."""
    l0 = min((x + 0.5 for x in range(2, 10)), key=lambda x: abs(over_under(p, x)[0] - 0.5))
    tab = {}
    for j in (-3, -2, -1, 1, 2, 3):
        o, u = over_under(p, l0 + j)
        tab[j] = o if j < 0 else u
    return l0, tab


def cmd_euro(cache: Path) -> None:
    # 1) method check on the NHL, where the conditional (per-posted-line) table is known
    odds = H.load_odds(cache)
    nhl_tot = [g["hs"] + g["as_"] for s in odds.values() for g in s]
    l0, tab = table_from(pmf(nhl_tot))
    print(f"NHL check: unconditional table anchored on {l0} (n={len(nhl_tot)}, mean {statistics.mean(nhl_tot):.2f}):")
    print("   j:  " + "  ".join(f"{j:+d}:{tab[j]:.3f}" for j in tab))
    print("   conditional (posted 5.5):  -1:0.754  +1:0.585  +2:0.769 | (posted 6.5): -1:0.589  +1:0.736")
    # 2) each European league: last season's regular season + this season so far
    print()
    for lg in EURO:
        prev = load_euro(cache, lg)
        d = json.loads((DOCS / f"{lg}_schedule.json").read_text())
        reg_n = sum(t["prevSeason"]["gp"] for t in d["teams"].values() if t.get("prevSeason")) // 2
        prev = sorted(prev, key=lambda r: r["date"])[:reg_n]          # regular season only: the first reg_n games (playoffs come after)
        cur = [g for g in d["games"] if g.get("state") == "post" and g.get("homeScore") is not None]
        for name, rows in (("last season", [r["hs"] + r["as_"] for r in prev]),
                           ("this season", [g["homeScore"] + g["awayScore"] for g in cur])):
            if rows:
                print(f"{lg:10s} {name}: n={len(rows)} mean {statistics.mean(rows):.2f} sd {statistics.pstdev(rows):.2f}")
        allt = [r["hs"] + r["as_"] for r in prev] + [g["homeScore"] + g["awayScore"] for g in cur]
        l0, tab = table_from(pmf(allt))
        print(f"   COMBINED n={len(allt)} anchor {l0}:  " + "  ".join(f"{j:+d}:{tab[j]:.3f}" for j in tab))
        print(f"   table = {json.dumps({str(j): round(v, 3) for j, v in tab.items()})}\n")


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
            rows.append((float(o["ou"]), g["hs"] + g["as_"], g.get("period")))
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
    ap.add_argument("cmd", choices=["nhl", "nba", "euro"])
    ap.add_argument("--cache", default="/tmp/cv_alt_bt")
    a = ap.parse_args()
    cache = Path(a.cache)
    {"nhl": cmd_nhl, "nba": cmd_nba, "euro": cmd_euro}[a.cmd](cache)
