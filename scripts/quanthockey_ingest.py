#!/usr/bin/env python3
"""Merge QuantHockey team tables (this season + last season) into docs/{league}_quanthockey.json.

quanthockey.com blocks unattended runners (Cloudflare), but the same-origin page fetch inside a normal browser works, so the refresh is: in a browser tab on quanthockey.com fetch each
league's /seasons/<season>/ page, keep the #statistics rows, and write them in the compact text form below. This script turns that text into the JSON the app reads:

    ## shl|2026-27
    <slug>~gp~g~sog~sh%~sv~sv%~ppo~ppg~pp%~pko~ppg-a~pk%
    ...
    ## shl|2025-26
    ...

Each team keeps its existing display name/slug (the stats columns below replace everything else); the current season fills the top-level stats and LAST season goes under `prev` (a team with no row last season -- promoted / returning --
simply has no `prev`, and the model then runs on this season alone). Usage:  python3 scripts/quanthockey_ingest.py compact.txt [--season 2026-27]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
FIELDS = ["gp", "g", "sog", "shPct", "sv", "svPct", "ppo", "ppg", "ppPct", "pko", "ppgAllowed", "pkPct"]
URL = {"liiga": "liiga", "shl": "shl", "nla": "nla", "extraliga": "czech-extraliga"}


def _num(s):
    s = (s or "").strip().replace(",", "").rstrip("%")
    if not s or s == "-":
        return None
    try:
        return int(s) if re.fullmatch(r"-?\d+", s) else float(s)
    except ValueError:
        return None


def parse_compact(text: str) -> dict:
    """-> {(league, season): {slug: {field: number}}}"""
    out: dict = {}
    cur = None
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("##"):
            lg, season = line[2:].strip().split("|")
            cur = out.setdefault((lg.strip(), season.strip()), {})
            continue
        parts = line.split("~")
        if cur is None or len(parts) != 1 + len(FIELDS):
            raise ValueError(f"bad row ({len(parts)} cells): {line[:80]}")
        cur[parts[0]] = {k: _num(v) for k, v in zip(FIELDS, parts[1:])}
    return out


def _derive(rec: dict) -> dict:
    gp = rec.get("gp") or 0
    if gp:
        for src, dst in (("g", "gPerGp"), ("sog", "shotsPerGp"), ("sv", "svPerGp")):
            if rec.get(src) is not None:
                rec[dst] = round(rec[src] / gp, 3)
    return rec


def merge(league: str, season: str, cur: dict, prev: dict | None, existing: dict | None) -> dict:
    existing = existing or {}
    old_teams = existing.get("teams") or {}
    teams = {}
    for slug, stats in cur.items():
        old = old_teams.get(slug) or {}
        base = {"name": old.get("name") or slug.replace("-", " ").title(), "slug": slug}   # name/slug only: a/p/pim from the old table would be stale
        base.update(_derive(dict(stats)))
        if prev and slug in prev:
            base["prev"] = _derive(dict(prev[slug]))
        else:
            base.pop("prev", None)
        teams[slug] = base
    if not teams:
        raise RuntimeError(f"no teams for {league}")
    return {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "league": league, "season": season, "source": "quanthockey.com",
        "source_url": f"https://www.quanthockey.com/{URL[league]}/en/seasons/{season}/",
        "teams": teams,
    }


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("compact")
    ap.add_argument("--season", default=None, help="current season label (default: the newest in the file)")
    a = ap.parse_args(argv)
    data = parse_compact(Path(a.compact).read_text())
    seasons = sorted({s for (_lg, s) in data})
    season = a.season or seasons[-1]
    prev_season = max((s for s in seasons if s < season), default=None)
    for league in URL:
        cur = data.get((league, season))
        if not cur:
            continue
        path = ROOT / "docs" / f"{league}_quanthockey.json"
        existing = json.loads(path.read_text()) if path.exists() else None
        out = merge(league, season, cur, data.get((league, prev_season)) if prev_season else None, existing)
        out["prev_season"] = prev_season
        path.write_text(json.dumps(out, indent=2, ensure_ascii=False) + "\n")
        n_prev = sum(1 for t in out["teams"].values() if "prev" in t)
        print(f"{league}: {len(out['teams'])} teams ({n_prev} with last season) -> {path.name}")


if __name__ == "__main__":
    main()
