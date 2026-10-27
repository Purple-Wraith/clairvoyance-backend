"""
ad_perf.py -- the live record the ads quote (October + all-time, every sport and league the paid products cover).

Stdlib only.  Reads the SAME subscriber-scope files the landing page's Live Track Record uses, so an ad can never
quote a different number from the site:
  docs/sport_performance.json                 per-league W-L for THIS MONTH and ALL TIME (the 12 covered leagues only)
  docs/engine_performance_subscriber.json     the overall THIS MONTH / ALL TIME tiles

Records and win % only -- units are deliberately not advertised (soccer moneylines and early euro-hockey spreads were
priced at placeholder odds, so unit figures are not comparable with real-market units).
scripts/test_ad_perf.py pins this module: leagues add up to the overall tiles, every covered league is present, and
nothing outside ad_coverage.ALL_LEAGUES can appear.
"""
from __future__ import annotations

import json
from pathlib import Path

import ad_coverage as ac

ROOT = Path(__file__).resolve().parent.parent
SPORT_JSON = ROOT / "docs" / "sport_performance.json"
ENGINE_JSON = ROOT / "docs" / "engine_performance_subscriber.json"

# sport_performance.json label -> ad_coverage display name
_LABEL_TO_NAME = {"NFL": "NFL", "CFB": "CFB", "NBA": "NBA", "NHL": "NHL", "SHL": "SHL", "NLA": "NLA", "LIIGA": "Liiga",
                  "EXTRALIGA": "Extraliga", "Premier League": "Premier League", "La Liga": "La Liga", "Serie A": "Serie A",
                  "Champions League": "Champions League"}

# Product blocks in the order the cards group them: a single-league product is one row, a bundle is a header + its leagues.
BLOCKS = tuple((product.upper() if product in ("nfl", "cfb", "nba") else product.capitalize(), leagues) for product, leagues in ac.LEAGUES_BY_PRODUCT.items())


def _rec(w: int, l: int, n: int | None = None) -> dict:
    """pct = wins / ALL settled picks (pushes count as not-wins), the landing page's own definition; a push is shown as a third record number."""
    n = w + l if n is None else n
    return {"w": w, "l": l, "p": n - w - l, "n": n, "pct": (w / n) if n else None}


def _sum(recs) -> dict:
    return _rec(sum(r["w"] for r in recs), sum(r["l"] for r in recs), sum(r["n"] for r in recs))


def load(sport_json: Path = SPORT_JSON, engine_json: Path = ENGINE_JSON) -> dict:
    """-> {'generated_at', 'month_label', 'leagues': {name: {'month': rec, 'all': rec}}, 'overall': {'month': rec, 'all': rec}}"""
    sp = json.loads(Path(sport_json).read_text())
    eng = json.loads(Path(engine_json).read_text())
    leagues: dict[str, dict] = {}
    for key, out in (("thisMonth", "month"), ("allTime", "all")):
        for row in sp[key]["leagues"]:
            name = _LABEL_TO_NAME.get(row["lbl"])
            if name is None:
                raise ValueError(f"unexpected league in {key}: {row['lbl']!r} (not a covered league)")
            leagues.setdefault(name, {})[out] = _rec(int(row["w"]), int(row["l"]), int(row["n"]))
    periods = {p["key"]: p for p in eng["periods"]}
    overall = {"month": _rec(int(periods["THIS_MONTH"]["w"]), int(periods["THIS_MONTH"]["l"]), int(periods["THIS_MONTH"]["n"])),
               "all": _rec(int(periods["ALL_TIME"]["w"]), int(periods["ALL_TIME"]["l"]), int(periods["ALL_TIME"]["n"]))}
    return {"generated_at": sp["generated_at"], "month_label": sp["thisMonth"]["label"], "leagues": leagues, "overall": overall,
            "month_name": _month_name(sp["thisMonth"]["label"])}


def _month_name(label: str) -> str:
    """'OCT 2026' -> 'OCTOBER'"""
    names = {"JAN": "JANUARY", "FEB": "FEBRUARY", "MAR": "MARCH", "APR": "APRIL", "MAY": "MAY", "JUN": "JUNE", "JUL": "JULY",
             "AUG": "AUGUST", "SEP": "SEPTEMBER", "OCT": "OCTOBER", "NOV": "NOVEMBER", "DEC": "DECEMBER"}
    return names.get(label.split()[0].upper(), label.split()[0].upper())


def block_rows(data: dict | None = None) -> list[dict]:
    """Display rows, best month win % first at both levels: single-league products one row; Hockey / Soccer a bundle row
    (the sum of its leagues) followed by its leagues.  Every row: {'name','kind':'single'|'bundle'|'league','month','all'}."""
    data = data or load()
    L = data["leagues"]
    blocks = []
    for title, leagues in BLOCKS:
        members = [{"name": n, "kind": "league", "month": L[n]["month"], "all": L[n]["all"]} for n in leagues]
        members.sort(key=lambda r: (-(r["month"]["pct"] or 0), -r["month"]["n"]))
        if len(leagues) == 1:
            blocks.append((members[0]["month"], [{**members[0], "kind": "single"}]))
        else:
            head = {"name": title.upper(), "kind": "bundle", "month": _sum([m["month"] for m in members]), "all": _sum([m["all"] for m in members])}
            blocks.append((head["month"], [head] + members))
    blocks.sort(key=lambda b: -(b[0]["pct"] or 0))
    return [r for _, rows in blocks for r in rows]


# Scorecard grouping: by SPORT (not by paid product) -- Football holds NFL and CFB, even though they are sold separately.
SPORT_GROUPS = (("FOOTBALL", ("NFL", "CFB")), ("BASKETBALL", ("NBA",)), ("HOCKEY", ac.LEAGUES_BY_PRODUCT["hockey"]), ("SOCCER", ac.LEAGUES_BY_PRODUCT["soccer"]))


def sport_rows(data: dict | None = None) -> list[dict]:
    """Display rows grouped by sport: a sport header (the sum of its leagues, kind 'bundle') followed by its leagues (kind 'league'),
    leagues best month win % first within a sport and sports best month win % first."""
    data = data or load()
    L = data["leagues"]
    blocks = []
    for title, leagues in SPORT_GROUPS:
        members = [{"name": n, "kind": "league", "month": L[n]["month"], "all": L[n]["all"]} for n in leagues]
        members.sort(key=lambda r: (-(r["month"]["pct"] or 0), -r["month"]["n"]))
        head = {"name": title, "kind": "bundle", "month": _sum([m["month"] for m in members]), "all": _sum([m["all"] for m in members])}
        blocks.append((head["month"], [head] + members))
    blocks.sort(key=lambda b: -(b[0]["pct"] or 0))
    return [r for _, rows in blocks for r in rows]


def fmt_pct(rec: dict) -> str:
    return "--" if rec["pct"] is None else f"{rec['pct'] * 100:.1f}%"


def fmt_rec(rec: dict) -> str:
    return f"{rec['w']}-{rec['l']}" + (f"-{rec['p']}" if rec["p"] else "")
