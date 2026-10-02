#!/usr/bin/env python3
"""Results-only refresh of the four European hockey schedule files (docs/{liiga,shl,nla,extraliga}_schedule.json).

Why: the settle pass (docs/app.html _autoSettleFlashscoreHockey) grades a pick only against a state:'post' game WITH scores in
those files, and the files used to be as fresh as the last full refresh workflow (3x/day, landing 2-6h late) plus a Pages
redeploy.  A morning European game therefore stayed 'pending' for hours after it ended.  This script lets a settle job re-scrape
just the /results/ pages right before it settles -- ONE browser, four pages (~1-3 minutes, no standings / fixtures / odds
pages) -- and merge the finals into the checkout's files so the settle pass (started with --serve-local) reads them directly.

It reuses each league module's own fetch_results() (home/away identity verified from the DOM markers, "Show more matches"
clicked, UTC context) and the previous file's team names, and folds the rows in with _schedule_carry.merge_results_only(): only
final results are written; fixtures, odds, standings and every other game stay byte-identical, and nothing is rewritten when
nothing changed.  FAIL-OPEN: a league whose scrape errors or returns 0 rows keeps its previous file; the exit code is always 0.

    python3 scripts/refresh_hockey_results.py                       # all four leagues
    python3 scripts/refresh_hockey_results.py --leagues liiga,nla   # a subset
    python3 scripts/refresh_hockey_results.py --dry-run             # scrape + report, write nothing

Normally invoked through scripts/lock_prep.py (--jobs hockey-results): that supplies the git sync before and the combined
commit/push after (the files this writes are listed in lock_prep.JOBS).
"""
from __future__ import annotations

import argparse
import importlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _schedule_carry import load_previous, merge_results_only  # noqa: E402

LEAGUES = {"liiga": "fetch_liiga", "shl": "fetch_shl", "nla": "fetch_nla", "extraliga": "fetch_extraliga"}


def log(msg: str) -> None:
    print(f"[refresh_hockey_results] {msg}", file=sys.stderr, flush=True)


def team_names_from(prev_doc: dict) -> dict:
    """{teamId: display name} from the previous file -- the same mapping the full scrape builds from the standings page."""
    out = {}
    for tid, t in ((prev_doc or {}).get("teams") or {}).items():
        if isinstance(t, dict) and t.get("name"):
            out[tid] = t["name"]
    return out


def season_start_year(now: datetime) -> int:
    return now.year if now.month >= 7 else now.year - 1


def apply_results(out_path: Path, results: list[dict], now: datetime, dry_run: bool = False) -> dict:
    """Merge `results` into the file at out_path (written only on a real change and when not dry_run). Returns merge stats."""
    prev = load_previous(out_path)
    if not prev.get("games"):
        return {"skipped": "no previous file/games to merge into", "updated": [], "added": [], "unchanged": 0}
    if not results:
        return {"skipped": "scrape returned 0 final results", "updated": [], "added": [], "unchanged": 0}
    doc, stats = merge_results_only(prev, results, now)
    if (stats["updated"] or stats["added"]) and not dry_run:
        out_path.write_text(json.dumps(doc, indent=2))
    return stats


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--leagues", default=",".join(LEAGUES), help="comma list of: " + ", ".join(LEAGUES))
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    wanted = [x.strip() for x in a.leagues.split(",") if x.strip() in LEAGUES]
    if not wanted:
        log("no valid leagues requested")
        return 0
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:  # fail-open: no browser available
        log(f"playwright unavailable ({exc}) -- nothing refreshed")
        return 0
    now = datetime.now(timezone.utc)
    ssy = season_start_year(now)
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            context = browser.new_context(timezone_id="UTC")  # same UTC pin as the full scrapers (date text has no zone)
            page = context.new_page()
            for lg in wanted:
                try:
                    mod = importlib.import_module(LEAGUES[lg])
                    prev = load_previous(mod.OUT)
                    results = mod.fetch_results(page, ssy, team_names_from(prev))
                    stats = apply_results(mod.OUT, results, now, a.dry_run)
                    if stats.get("skipped"):
                        log(f"{lg}: {stats['skipped']} -- file untouched")
                    else:
                        log(f"{lg}: {len(results)} finals scraped; {len(stats['updated'])} updated, {len(stats['added'])} added, "
                            f"{stats['unchanged']} unchanged" + (" [dry-run, nothing written]" if a.dry_run else ""))
                except Exception as exc:  # per-league fail-open
                    log(f"{lg}: FAILED ({exc}) -- file untouched")
            browser.close()
    except Exception as exc:
        log(f"browser session failed ({exc}) -- files untouched")
    return 0


if __name__ == "__main__":
    sys.exit(main())
