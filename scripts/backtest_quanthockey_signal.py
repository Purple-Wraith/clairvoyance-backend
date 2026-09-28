"""Phase 2 of the QuantHockey-signal investigation (see the 2026-09-28
Phase 1 correlation check for the original scoping/findings -- not
checked in as a file, see that day's commit message for the writeup).

THE PROBLEM Phase 1 couldn't solve: QuantHockey only ever gives
season-aggregate team totals, never a per-game or per-date snapshot.
Using TODAY's aggregate against a bet locked weeks ago is look-ahead
bias -- today's numbers already include games that happened after that
bet settled. Phase 1 accepted that bias (explicitly caveated) because
these are brand-new products with barely any history yet, so the
leakage was small in practice. It stops being small as more history
accumulates.

THE FIX this script implements: every docs/{league}_quanthockey.json
refresh is its own dedicated git commit (never squashed, never
amended -- this is now a hard convention, not a suggestion) recording
its own generated_at timestamp. That means the file's real git history
IS a genuine point-in-time snapshot series for free, no new storage
needed. This script walks that history: for each settled ML bet, it
finds the QuantHockey commit that was actually current AT THE BET'S
OWN lockedAt time (the latest commit dated on or before it) and reads
that file version via `git show <commit>:<path>` -- never today's
working copy -- before computing anything.

WHEN TO RUN THIS FOR REAL: right now (2026-09-28) there are only 2-3
commits per league's file, so most bets will resolve to whichever
snapshot came first -- this run will look a lot like Phase 1's single-
snapshot check, not meaningfully different. This script earns its
keep once real time-separated snapshots accumulate: several weeks of
periodic refreshes, each its own commit, giving enough distinct
before/after states to actually test whether the QuantHockey signal
predicts outcomes beyond what the current Flashscore-only Poisson
model already captures. A reasonable bar before trusting the output
here: at least ~4-6 weeks of accumulated snapshots AND at least ~50-100
settled ML bets per league (LIIGA had 20 matched in the Phase 1 run;
SHL/NLA/EXTRALIGA had single digits -- nowhere near enough yet).

OUTPUT: same correlation-and-Brier-style report Phase 1 produced, but
every QuantHockey value used is the real point-in-time snapshot for
that specific bet, not today's aggregate. A consistent, real
correlation here -- not the noise-level, direction-inconsistent numbers
Phase 1 found -- is what would justify actually wiring QuantHockey into
liigaMC/shlMC/nlaMC/extraligaMC's win-probability calc (see those
functions' own "CRITICAL, do not change without re-reading this"
comment in docs/app.html for why that boundary exists and must not be
crossed without exactly this kind of validation first).

Usage:
  python3 scripts/backtest_quanthockey_signal.py
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import unicodedata
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

SUPABASE_URL = "https://vhwkbeblforpnliowpam.supabase.co"
SUPABASE_KEY = "sb_publishable_AEgqvpaIh0MWpDdXI35BYw_lD8HzBlM"

QH_LEAGUES = {
    "LIIGA": "docs/liiga_quanthockey.json",
    "SHL": "docs/shl_quanthockey.json",
    "NLA": "docs/nla_quanthockey.json",
    "EXTRALIGA": "docs/extraliga_quanthockey.json",
}
METRICS = ["shPct", "ppPct", "pkPct", "svPct", "shotsPerGp"]
HK_NAME_ALIASES = {"zurich": "zsc lions"}


def log(msg: str) -> None:
    print(f"[backtest_quanthockey] {msg}", file=sys.stderr)


def fetch_all_bets() -> list[dict]:
    """Same paginated pull pattern every other script in this repo uses
    against the real Supabase ledger (PostgREST caps a page at 1000)."""
    rows: list[dict] = []
    offset, page_size = 0, 1000
    while True:
        req = urllib.request.Request(
            f"{SUPABASE_URL}/rest/v1/bets?select=id,raw,outcome&order=id",
            headers={
                "apikey": SUPABASE_KEY,
                "Authorization": f"Bearer {SUPABASE_KEY}",
                "Range": f"{offset}-{offset + page_size - 1}",
            },
        )
        with urllib.request.urlopen(req) as r:
            batch = json.loads(r.read())
        rows.extend(batch)
        if len(batch) < page_size:
            break
        offset += page_size
    return rows


def qh_commit_history(path: str) -> list[tuple[str, int]]:
    """Every commit that touched this file, oldest first, as
    (commit_hash, author_timestamp_ms). Uses the commit's own author
    date, not generated_at inside the file, so this works even if a
    future refresh's generated_at is ever missing/malformed."""
    out = subprocess.run(
        ["git", "log", "--format=%H %at", "--follow", "--", path],
        cwd=ROOT, capture_output=True, text=True, check=True,
    ).stdout.strip()
    commits = []
    for line in out.splitlines():
        h, ts = line.split()
        commits.append((h, int(ts) * 1000))
    commits.reverse()  # oldest first
    return commits


def qh_snapshot_at(path: str, commits: list[tuple[str, int]], at_ms: int) -> dict | None:
    """The real file content as it existed at the latest commit dated on
    or before at_ms -- i.e. what a lock at that exact moment would
    actually have seen. None if at_ms predates the first-ever commit
    (no historical snapshot exists yet for that point in time)."""
    chosen = None
    for h, ts in commits:
        if ts <= at_ms:
            chosen = h
        else:
            break
    if chosen is None:
        return None
    content = subprocess.run(
        ["git", "show", f"{chosen}:{path}"],
        cwd=ROOT, capture_output=True, text=True, check=True,
    ).stdout
    return json.loads(content)


def hk_norm_name(s: str) -> str:
    s = unicodedata.normalize("NFD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return s.lower().strip()


def hk_fuzzy_find_qh(app_name: str, qh_teams: list[dict]) -> dict | None:
    """Direct port of docs/app.html's _hkFuzzyFindQH -- keep this in sync
    if that function ever changes; it's the same matcher the live
    COMPARE panel uses, confirmed live to resolve 100% of real teams."""
    if not app_name or not qh_teams:
        return None
    k = hk_norm_name(app_name)
    aliased = HK_NAME_ALIASES.get(k)
    for t in qh_teams:
        ck = hk_norm_name(t["name"])
        if ck == k or k in ck or ck in k:
            return t
        if aliased and aliased in ck:
            return t
    k_tokens = [w for w in re.split(r"\s+", k) if len(w) >= 3]

    def strip_suffix(w: str) -> str:
        return re.sub(r"[ns]$", "", w)

    for t in qh_teams:
        c_tokens = [w for w in re.split(r"\s+", hk_norm_name(t["name"])) if len(w) >= 3]
        for kw in k_tokens:
            for cw in c_tokens:
                if strip_suffix(kw) == strip_suffix(cw) or kw.startswith(cw) or cw.startswith(kw):
                    return t
    return None


def correlate(pairs: list[tuple[float, float]]) -> float | None:
    n = len(pairs)
    if n < 5:
        return None
    xs, ys = [p[0] for p in pairs], [p[1] for p in pairs]
    mx, my = sum(xs) / n, sum(ys) / n
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / n
    sx = (sum((x - mx) ** 2 for x in xs) / n) ** 0.5
    sy = (sum((y - my) ** 2 for y in ys) / n) ** 0.5
    return cov / (sx * sy) if sx > 0 and sy > 0 else None


def main() -> None:
    all_rows = fetch_all_bets()
    log(f"pulled {len(all_rows)} raw ledger rows")

    commit_history = {lg: qh_commit_history(path) for lg, path in QH_LEAGUES.items()}
    for lg, commits in commit_history.items():
        log(f"{lg}: {len(commits)} snapshot(s) in git history")

    results: dict[str, list[dict]] = {lg: [] for lg in QH_LEAGUES}
    skipped_no_snapshot = 0
    skipped_no_match = 0

    for row in all_rows:
        raw = row.get("raw") or {}
        if row.get("outcome") == "_removed":
            continue
        sport = (raw.get("sport") or "").upper()
        league = (raw.get("league") or "").upper()
        tag = sport if sport in QH_LEAGUES else (league if league in QH_LEAGUES else None)
        if not tag or raw.get("outcome") not in ("win", "loss") or raw.get("betType") != "ML":
            continue
        win_prob, locked_at = raw.get("winProb"), raw.get("lockedAt")
        hA, awA, bet_on = raw.get("hA"), raw.get("awA"), raw.get("betOn")
        if win_prob is None or not locked_at or not hA or not awA or not bet_on:
            continue

        snapshot = qh_snapshot_at(QH_LEAGUES[tag], commit_history[tag], locked_at)
        if not snapshot:
            skipped_no_snapshot += 1
            continue

        bet_on_clean = re.sub(r"\s+ML$", "", bet_on).strip()
        if bet_on_clean == hA.strip():
            picked_name, opp_name = hA, awA
        elif bet_on_clean == awA.strip():
            picked_name, opp_name = awA, hA
        else:
            skipped_no_match += 1
            continue

        qh_teams = list(snapshot["teams"].values())
        picked_qh, opp_qh = hk_fuzzy_find_qh(picked_name, qh_teams), hk_fuzzy_find_qh(opp_name, qh_teams)
        if not picked_qh or not opp_qh:
            skipped_no_match += 1
            continue

        actual = 1.0 if raw["outcome"] == "win" else 0.0
        error = actual - win_prob
        diffs = {m: (picked_qh.get(m) - opp_qh.get(m)) if picked_qh.get(m) is not None and opp_qh.get(m) is not None else None for m in METRICS}
        results[tag].append({"error": error, "diffs": diffs, "snapshot_generated_at": snapshot.get("generated_at")})

    log(f"skipped (bet predates any snapshot): {skipped_no_snapshot}")
    log(f"skipped (team-name match failure): {skipped_no_match}")
    print()

    total_n = 0
    for lg, rows in results.items():
        total_n += len(rows)
        print(f"=== {lg}: n={len(rows)} ML bets, point-in-time snapshots ===")
        if not rows:
            continue
        snaps_used = sorted({r["snapshot_generated_at"] for r in rows})
        print(f"  snapshot(s) actually used: {snaps_used}")
        for m in METRICS:
            pairs = [(r["diffs"][m], r["error"]) for r in rows if r["diffs"][m] is not None]
            r = correlate(pairs)
            print(f"  {m:12s}: n={len(pairs):3d}  corr = {r:+.3f}" if r is not None else f"  {m:12s}: n={len(pairs)} (too few)")
    print()
    print(f"Total ML bets analyzed (point-in-time correct): {total_n}")
    print()
    print("Reminder: with only 2-3 snapshots per league right now, most/all")
    print("bets above resolve to the SAME snapshot -- this is expected and")
    print("not yet a meaningful walk-forward test. Re-run this script again")
    print("in 4-6+ weeks once more dated refreshes have landed as their own")
    print("commits.")


if __name__ == "__main__":
    main()
