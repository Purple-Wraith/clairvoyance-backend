#!/usr/bin/env python3
"""Deterministic odds/schedule freshness for the lock workflows -- run INSIDE each lock workflow, right before the lock pass.

Why: hockey legs only qualify on a REAL posted price younger than HOCKEY_ODDS_MAX_AGE_H (18h), and the schedule/odds JSON the lock
reads used to be whatever the separate *-schedule-refresh workflows had last committed AND GitHub Pages had last redeployed. Those
workflows are delayed 5-12h by GitHub (see the 2026-10-02 run-history audit), so freshness was cron-order luck: a lock that landed
at 4 AM MT read odds scraped 10-17h earlier, one landing after the next refresh was fine, and a stale snapshot silently turned every
hockey leg into "no real price" -> zero picks. This script makes the lock job refresh what it is about to read:

  1. sync the checkout to origin/main (so a refresh another workflow just pushed is not lost),
  2. run the SAME scrapers the refresh workflows run, for the relevant jobs, in parallel (each writes its docs/*.json),
  3. commit + push the changed JSON with a rebase-retry (same pattern as the other workflows; best effort),
  4. print a freshness report (newest odds timestamp, how many upcoming games have a real price).

The lock pass that follows is started with `--serve-local`: it serves THIS checkout's docs/ and drives the app from it, so it reads
exactly the files written in step 2 -- no wait for a Pages redeploy, no dependence on whether the push succeeded. (GitHub Pages serves
docs/ verbatim -- pages-deploy.yml uploads `path: docs` -- so the local copy is byte-for-byte what would be deployed, one commit fresher.)

FAIL-OPEN BY DESIGN: every step is best effort and the script always exits 0. A scraper that times out or errors leaves the previous
file in place (the lock then behaves exactly as it did before this script existed); the freshness report says so in the job log.

    python3 scripts/lock_prep.py --jobs hockey,nhl,data-nhl   # which scrapers (see JOBS / GROUPS below)
    python3 scripts/lock_prep.py --jobs hockey --dry-run      # print what would run, touch nothing
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable
GIT = ["git", "-C", str(ROOT), "-c", "user.name=clairvoyance-bot", "-c", "user.email=bot@clairvoyance.local"]

# job -> (argv after python, file(s) it writes). No --push anywhere: ONE combined commit is made below (parallel scrapers each
# running their own `git pull --rebase` + push in one working tree would race each other).
JOBS: dict[str, tuple[list[str], list[str]]] = {
    "shl": (["scripts/fetch_shl.py"], ["docs/shl_schedule.json"]),
    "liiga": (["scripts/fetch_liiga.py"], ["docs/liiga_schedule.json"]),
    "nla": (["scripts/fetch_nla.py"], ["docs/nla_schedule.json"]),
    "extraliga": (["scripts/fetch_extraliga.py"], ["docs/extraliga_schedule.json"]),
    # RESULTS-ONLY re-scrape of the four hockey leagues' /results/ pages, merged into their schedule files (finals only; fixtures,
    # odds and standings untouched). Run by the settle-capable slots of auto-lock-settle.yml right before the settle logic, so the
    # settle pass reads scores minutes old instead of whatever the last full refresh + Pages redeploy left. ~1-3 min, one browser.
    "hockey-results": (["scripts/refresh_hockey_results.py"],
                       ["docs/shl_schedule.json", "docs/liiga_schedule.json", "docs/nla_schedule.json", "docs/extraliga_schedule.json"]),
    "nhl": (["scripts/fetch_nhl.py", "--flashscore-odds"], ["docs/nhl_schedule.json"]),
    "soccer": (["scripts/scrape_soccer_schedule.py"], ["docs/soccer_schedule.json"]),
    "soccer-tomorrow": (["scripts/scrape_soccer_schedule.py", "--tomorrow"], ["docs/soccer_schedule_tomorrow.json"]),
    "cfb": (["scripts/fetch_cfb.py", "--mode", "schedule"], ["docs/cfb_schedule.json"]),
    "nfl": (["scripts/fetch_nfl.py", "--mode", "schedule"], ["docs/nfl_schedule.json"]),
    # NBA rolling schedule (yesterday + next 10 days: states, scores, ESPN market lines; ~6 s, plain requests, no browser). Preseason
    # games are in the file flagged seasonType 1 / preseason:true -- the lock pass skips those (auto_lock_settle.py NBA block).
    "nba": (["scripts/fetch_nba.py"], ["docs/nba_schedule.json"]),
    # The FAST NHL pieces of docs/data.json (standings, edge, MoneyPuck, skaterValue, NHL injuries), merged into the committed file
    # (read-modify-write, every other key untouched, atomic, fail-open) -- scheduled-refresh.yml lands 3x/day and 3-6h late, so without
    # this a lock pass inherits standings that can miss a quarter of the finished games. ~5-15 s, no browser. Writes data.json only
    # (never version.json / app.html), and stamps top-level `nhlCoreAt`; the full refresh's `generated` is left alone.
    "data-nhl": (["scripts/clairvoyance_update.py", "--only-nhl-core"], ["docs/data.json"]),
}
# Earliest-kickoff league first: if the total budget runs out, the leagues that matter soonest were refreshed.
GROUPS: dict[str, list[str]] = {
    "hockey": ["shl", "liiga", "nla", "extraliga"],
    "soccer-all": ["soccer", "soccer-tomorrow"],
}
ODDS_FILES = {"shl": "docs/shl_schedule.json", "liiga": "docs/liiga_schedule.json", "nla": "docs/nla_schedule.json",
              "extraliga": "docs/extraliga_schedule.json", "nhl": "docs/nhl_schedule.json",
              "nba": "docs/nba_schedule.json"}


def log(msg: str) -> None:
    print(f"[lock_prep {datetime.now(timezone.utc).strftime('%H:%M:%S')}] {msg}", flush=True)


def expand(spec: str) -> list[str]:
    out: list[str] = []
    for tok in [t.strip() for t in spec.split(",") if t.strip()]:
        for j in GROUPS.get(tok, [tok]):
            if j not in JOBS:
                log(f"unknown job '{j}' ignored")
            elif j not in out:
                out.append(j)
    return out


def sync_checkout() -> None:
    """Bring the checkout up to origin/main (autostash: the tree may carry a half-written marker/status). Best effort."""
    r = subprocess.run(GIT + ["pull", "--rebase", "--autostash", "origin", "main"], capture_output=True, text=True, timeout=120)
    log(f"git sync: {'ok' if r.returncode == 0 else 'FAILED (continuing on the checked-out commit): ' + (r.stderr or r.stdout).strip()[:200]}")


def run_jobs(jobs: list[str], per_job_s: int, total_s: int) -> dict[str, dict]:
    """Run every scraper concurrently (separate processes, separate output files), each bounded by per_job_s; the whole batch is
    bounded by total_s. Returns {job: {"rc": int|None, "secs": float, "tail": str}}."""
    start = time.time()
    procs: dict[str, tuple[subprocess.Popen, float]] = {}
    env = dict(os.environ, TZ=os.environ.get("TZ", "America/Denver"))
    for j in jobs:
        argv, _files = JOBS[j]
        procs[j] = (subprocess.Popen([PY, *argv], cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True),
                    time.time())
        log(f"started {j}: {' '.join(argv)}")
    res: dict[str, dict] = {}
    while procs:
        for j, (p, t0) in list(procs.items()):
            rc = p.poll()
            over = (time.time() - t0 > per_job_s) or (time.time() - start > total_s)
            if rc is None and over:
                p.kill()
                rc = -9
                log(f"{j}: TIMED OUT after {time.time() - t0:.0f}s -- killed (previous file stays in place)")
            if rc is not None:
                out = (p.stdout.read() if p.stdout else "") or ""
                res[j] = {"rc": rc, "secs": round(time.time() - t0, 1), "tail": out.strip().splitlines()[-3:]}
                log(f"{j}: exit {rc} in {res[j]['secs']}s" + (f" | {' / '.join(res[j]['tail'])[:240]}" if rc else ""))
                del procs[j]
        time.sleep(1.0)
    return res


def guard_empty_soccer(jobs: list[str]) -> None:
    """scrape_soccer_schedule.py writes its snapshot even when EVERY league's ESPN call failed (an all-empty file). Overwriting a
    snapshot that had games for the same date with that would turn a transient outage into "no games" for the lock. If a soccer
    job produced zero games where the committed file (same date) had some, restore the committed file."""
    for j in jobs:
        if not j.startswith("soccer"):
            continue
        f = JOBS[j][1][0]
        try:
            new = json.loads((ROOT / f).read_text())
            old = json.loads(subprocess.run(["git", "-C", str(ROOT), "show", f"HEAD:{f}"], capture_output=True, text=True).stdout or "{}")
        except Exception:
            continue
        n_new = sum(len(v) for v in (new.get("leagues") or {}).values())
        n_old = sum(len(v) for v in (old.get("leagues") or {}).values())
        if n_new == 0 and n_old > 0 and new.get("date") == old.get("date"):
            subprocess.run(["git", "-C", str(ROOT), "checkout", "--", f], capture_output=True)
            log(f"{j}: scrape returned 0 games but the committed snapshot for {new.get('date')} had {n_old} -- restored it (transient outage?)")


def commit_and_push(jobs: list[str]) -> None:
    """One commit for everything that changed; fetch+rebase(autostash) retry like the other workflows. Best effort."""
    files = sorted({f for j in jobs for f in JOBS[j][1]})
    subprocess.run(GIT + ["add", *files], capture_output=True)
    if subprocess.run(GIT + ["diff", "--cached", "--quiet"]).returncode == 0:
        log("no schedule/odds/data change to commit")
        return
    kind = "pre-settle results refresh" if jobs == ["hockey-results"] else "pre-lock odds refresh"
    msg = f"chore: {kind} ({', '.join(jobs)}) {datetime.now(timezone.utc).strftime('%H:%MZ')}"
    if subprocess.run(GIT + ["commit", "-q", "-m", msg], capture_output=True).returncode != 0:
        log("commit failed -- continuing (the lock reads the working tree, not origin)")
        return
    for attempt in range(5):
        if subprocess.run(GIT + ["push", "origin", "HEAD:main"], capture_output=True).returncode == 0:
            log("pushed refreshed JSON")
            return
        subprocess.run(GIT + ["fetch", "origin", "main"], capture_output=True)
        if subprocess.run(GIT + ["rebase", "--autostash", "origin/main"], capture_output=True).returncode != 0:
            subprocess.run(GIT + ["rebase", "--abort"], capture_output=True)
            log(f"push attempt {attempt + 1}: rebase conflict -- giving up on pushing (the lock still reads the local files)")
            return
        time.sleep(2 + attempt * 2)
    log("push still failing after 5 retries -- continuing (the lock reads the working tree)")


def freshness_report(jobs: list[str]) -> None:
    """What the lock is about to see: per league, upcoming games, how many have a real price, newest scrape age."""
    now = datetime.now(timezone.utc)
    if "data-nhl" in jobs:
        try:
            d = json.loads((ROOT / "docs" / "data.json").read_text())
            at = datetime.fromisoformat(str(d.get("nhlCoreAt") or d.get("generated")).replace("Z", "+00:00"))
            log(f"freshness data.json NHL core: stamped {(now - at).total_seconds() / 60:.0f} min ago "
                f"({'nhlCoreAt' if d.get('nhlCoreAt') else 'generated -- core refresh did not land'})")
        except Exception as exc:
            log(f"freshness data.json NHL core: unreadable ({exc})")
    for j in jobs:
        path = ODDS_FILES.get(j)
        if not path:
            continue
        try:
            games = json.loads((ROOT / path).read_text()).get("games", [])
        except Exception as exc:
            log(f"freshness {j}: unreadable ({exc})")
            continue
        up = [g for g in games if g.get("state") == "pre"]
        # hockey files carry g["odds"]["ml"/"at"]; the NBA file carries flat homeML/awayML + oddsAt (ESPN single-book line)
        priced = [g for g in up if (g.get("odds") or {}).get("ml") or (g.get("homeML") is not None and g.get("awayML") is not None)]
        ats = []
        for g in priced:
            try:
                ats.append(datetime.fromisoformat((g.get("odds") or {}).get("at", g.get("oddsAt")).replace("Z", "+00:00")))
            except Exception:
                pass
        newest = max(ats) if ats else None
        age = f"{(now - newest).total_seconds() / 3600:.1f}h" if newest else "n/a"
        log(f"freshness {j}: {len(up)} upcoming, {len(priced)} with a real price, newest odds age {age}"
            + ("  <-- STALE (> 18h cap)" if newest and (now - newest).total_seconds() > 18 * 3600 else ""))


def kickoff_in_window(ahead_h: float, behind_h: float) -> tuple[bool, str]:
    """-> (any game starts within [now - behind_h, now + ahead_h], description). Reads the committed schedule JSON only (no network,
    no browser) so a watchdog slot can decide in bash-cost whether a full pass is worth running. Unreadable files are skipped."""
    now = datetime.now(timezone.utc).timestamp()
    lo, hi = now - behind_h * 3600, now + ahead_h * 3600
    docs = ROOT / "docs"
    best = None

    def consider(iso, tag):
        nonlocal best
        try:
            t = datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp()
        except Exception:
            return
        if lo <= t <= hi and (best is None or t < best[0]):
            best = (t, tag)

    for fn, tag in (("shl_schedule.json", "SHL"), ("liiga_schedule.json", "LIIGA"), ("nla_schedule.json", "NLA"),
                    ("extraliga_schedule.json", "EXTRALIGA"), ("nhl_schedule.json", "NHL")):
        try:
            for g in json.loads((docs / fn).read_text()).get("games", []):
                if g.get("state") != "post":
                    consider(g.get("date"), tag)
        except Exception:
            pass
    for fn, tag in (("cfb_schedule.json", "CFB"), ("nfl_schedule.json", "NFL")):
        try:
            for wk in json.loads((docs / fn).read_text()).get("weeks", {}).values():
                for g in wk or []:
                    if g.get("state") != "post":
                        consider(g.get("date"), tag)
        except Exception:
            pass
    for fn in ("soccer_schedule.json", "soccer_schedule_tomorrow.json"):
        try:
            for gs in json.loads((docs / fn).read_text()).get("leagues", {}).values():
                for g in gs or []:
                    if g.get("status") != "post":
                        consider(g.get("date"), "SOCCER")
        except Exception:
            pass
    if best is None:
        return False, f"no game starts within [-{behind_h:g}h, +{ahead_h:g}h]"
    return True, f"{best[1]} game at {datetime.fromtimestamp(best[0], timezone.utc).strftime('%Y-%m-%d %H:%MZ')}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--kickoff-window", type=float, default=None, metavar="AHEAD_H",
                    help="Instead of scraping: exit 0 if any game starts within the next AHEAD_H hours (or started within the last "
                         "--kickoff-behind hours), else exit 1. Reads the committed schedule JSON only.")
    ap.add_argument("--kickoff-behind", type=float, default=3.0)
    ap.add_argument("--jobs", default="", help="comma list of jobs/groups: " + ", ".join(list(JOBS) + list(GROUPS)))
    ap.add_argument("--per-job-timeout", type=int, default=420, help="seconds before one scraper is killed")
    ap.add_argument("--total-timeout", type=int, default=540, help="seconds for the whole scrape batch")
    ap.add_argument("--no-push", action="store_true", help="refresh the local files only (skip the commit/push)")
    ap.add_argument("--no-sync", action="store_true", help="skip the git pull")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    if a.kickoff_window is not None:
        found, why = kickoff_in_window(a.kickoff_window, a.kickoff_behind)
        log(("kickoff in window: " if found else "") + why)
        return 0 if found else 1
    jobs = expand(a.jobs)
    log(f"jobs: {jobs}")
    if a.dry_run or not jobs:
        for j in jobs:
            log(f"[dry-run] {PY} {' '.join(JOBS[j][0])}")
        return 0
    try:
        if not a.no_sync:
            sync_checkout()
        run_jobs(jobs, a.per_job_timeout, a.total_timeout)
        guard_empty_soccer(jobs)
        freshness_report(jobs)
        if not a.no_push:
            commit_and_push(jobs)
    except Exception as exc:  # fail-open: never block the lock pass that follows
        log(f"unexpected error (ignored, fail-open): {exc}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
