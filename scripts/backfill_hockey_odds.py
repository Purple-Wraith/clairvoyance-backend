"""One-off (re-runnable) backfill of REAL closing bookmaker prices for finished
SHL / LIIGA / NLA / EXTRALIGA games that a logged pick refers to.

Why: until 2026-10-02 these leagues' picks were logged at ASSUMED prices (ML =
the model's own probability as odds, O/U 1.91, puck line 1.87). Flashscore's
completed match pages keep their final odds (confirmed live 2026-10-02), so the
real-price record of every past pick can be reconstructed after the fact.

Reads : docs/picks_backup.json (committed ledger backup) + the four
        docs/{liiga,shl,nla,extraliga}_schedule.json
Writes: docs/hockey_odds_backfill.json = {"matches": {matchId: {league, date,
        home, away, homeScore, awayScore, odds{ml,ou[],pl,fmt,bk,at}}},
        "unmatched": [pickIds], ...}. Re-runs only fetch matches that have no
        odds yet (use --force to redo all). Scraping logic is the SAME shared
        helper the live schedule scrapers use (scripts/_flashscore_odds.py).

Pick -> game matching: teams (either orientation, normalised names) within
+/-1 day of the pick date; if two games qualify (back-to-back rematches) the
one on the pick's own date wins; otherwise the pick is reported unmatched.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
import unicodedata
from datetime import date, datetime, timezone
from pathlib import Path

from playwright.sync_api import sync_playwright

from _flashscore_odds import fetch_match_odds

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
OUT = DOCS / "hockey_odds_backfill.json"
LEAGUES = ("LIIGA", "SHL", "NLA", "EXTRALIGA")


def log(msg: str) -> None:
    print(f"[backfill_hockey_odds] {msg}", file=sys.stderr)


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]", "", s)


def _name_eq(a: str, b: str) -> bool:
    return bool(a and b and (a == b or a in b or b in a))


def match_pick(pick: dict, games: list[dict]) -> dict | None:
    ph, pa = _norm(pick.get("hA", "")), _norm(pick.get("awA", ""))
    pd = date.fromisoformat(pick["date"])
    cands = []
    for g in games:
        h, a = _norm(g["homeName"]), _norm(g["awayName"])
        same = _name_eq(h, ph) and _name_eq(a, pa)
        flip = _name_eq(h, pa) and _name_eq(a, ph)
        if not (same or flip):
            continue
        diff = abs((date.fromisoformat(g["date"][:10]) - pd).days)
        if diff <= 1:
            cands.append((diff, g))
    if not cands:
        return None
    cands.sort(key=lambda x: x[0])
    if len(cands) > 1 and cands[0][0] == cands[1][0]:
        return None  # genuinely ambiguous
    return cands[0][1]


def run(force: bool) -> None:
    picks = json.loads((DOCS / "picks_backup.json").read_text())
    sched = {lg: json.loads((DOCS / f"{lg.lower()}_schedule.json").read_text())["games"] for lg in LEAGUES}
    prev = {}
    if OUT.exists():
        try:
            prev = json.loads(OUT.read_text())
        except Exception:
            prev = {}
    matches: dict = dict(prev.get("matches") or {})
    unmatched: list[str] = []
    targets: dict[str, dict] = {}
    n_picks = 0
    for p in picks:
        lg = p.get("sport")
        if lg not in LEAGUES:
            continue
        g = match_pick(p, sched[lg])
        if g is None:
            unmatched.append(p["id"])
            continue
        if g.get("state") != "post":
            continue  # game not finished (pending pick) -- nothing to backfill yet
        n_picks += 1
        targets[g["id"]] = {"league": lg, "g": g}
    log(f"{n_picks} picks on finished games -> {len(targets)} distinct matches; "
        f"{len(unmatched)} picks could not be matched to a game: {unmatched}")

    todo = [t for mid, t in targets.items() if force or not (matches.get(mid) or {}).get("odds")]
    log(f"{len(todo)} matches to scrape ({len(targets) - len(todo)} already have odds)")

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_context(timezone_id="UTC").new_page()
        for i, t in enumerate(todo, 1):
            g = t["g"]
            res = {"ou": None, "odds": None}
            for attempt in (1, 2):
                res = fetch_match_odds(page, "home", g["home"], "away", g["away"], g["id"])
                if res["odds"]:
                    break
                log(f"  [{i}/{len(todo)}] {g['id']} attempt {attempt}: no odds")
            matches[g["id"]] = {
                "league": t["league"], "date": g["date"],
                "home": g["homeName"], "away": g["awayName"],
                "homeScore": g.get("homeScore"), "awayScore": g.get("awayScore"),
                "odds": res["odds"],
            }
            log(f"  [{i}/{len(todo)}] {t['league']} {g['homeName']} v {g['awayName']} -> "
                f"{'ok' if res['odds'] else 'NO ODDS'}")
        browser.close()

    out = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "source": "Flashscore completed-match odds pages (FT incl. OT); decimal; median across bookmakers",
        "matches": dict(sorted(matches.items())),
        "unmatched": unmatched,
    }
    OUT.write_text(json.dumps(out, indent=1))
    have = sum(1 for m in matches.values() if m.get("odds"))
    log(f"Wrote {OUT} -- {have}/{len(matches)} matches with odds")


def git_push(paths: list[str], message: str) -> None:
    subprocess.run(["git", "add", *paths], cwd=ROOT, check=True)
    r = subprocess.run(["git", "commit", "-m", message], cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        log(f"  nothing to commit ({r.stdout.strip()[:120]})")
        return
    for attempt in range(5):
        subprocess.run(["git", "pull", "--rebase", "origin", "main"], cwd=ROOT, capture_output=True)
        push = subprocess.run(["git", "push", "origin", "main"], cwd=ROOT, capture_output=True, text=True)
        if push.returncode == 0:
            log("  pushed")
            return
        time.sleep(3 + attempt * 2)
    raise RuntimeError("git push failed after 5 retries")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--push", action="store_true")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    run(a.force)
    if a.push:
        git_push(["docs/hockey_odds_backfill.json"], "chore: backfill real closing odds for past hockey picks")
