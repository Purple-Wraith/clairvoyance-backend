"""Shared scraper-health logging for the hockey Flashscore scrapers
(fetch_shl.py/fetch_liiga.py/fetch_nla.py/fetch_extraliga.py).

Added 2026-10-02 after a real incident: the SHL/Liiga/NLA/Extraliga
scrapers silently captured far fewer completed games than they should
have, for weeks, with zero error raised (one run captured 27 results,
the pagination fix brought it to 34 -- see fetch_shl.py's
_extract_match_row/fetch_results comments for the underlying bug). There
was no historical record of "how many rows did this scrape capture" to
have caught that sooner. This module is that record: a flat rolling log
the Engine Health tab (docs/app.html, renderEngineHealth) reads to show
a per-league trend and flag an anomalous drop.

Kept as its own tiny module (not inlined in each fetch_*.py) since all
4 callers need the exact same append-and-trim logic -- a copy-pasted
version in each file is just 4 chances for the trim logic to drift.

2026-10-03: the log was written on the runner but NEVER COMMITTED -- each fetch script's git_push() only `git add`s its own
schedule file, so docs/scraper_health.json never reached the repo and the Engine Health tab always 404'd.  commit_and_push() below
is the fix: each fetch script calls it right after its schedule-file push.  It is a SEPARATE best-effort commit (not folded into
the schedule commit) on purpose: four leagues share this one file, so a concurrent push by another league's workflow is a real
conflict, and that must never be able to fail the schedule push itself.  On a rebase conflict the two lists are merged (union by
league+timestamp) instead of one side losing.  Writes are atomic + flock-guarded so lock_prep.py's four parallel scrapers (which
share a checkout) cannot truncate or lose each other's entries.
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

try:  # POSIX only; the GitHub runners and the dev Mac both have it
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None

ROOT = Path(__file__).resolve().parent.parent
LOG_PATH = ROOT / "docs" / "scraper_health.json"
HEALTH_REL = "docs/scraper_health.json"
LOCK_PATH = os.path.join(tempfile.gettempdir(), "cv_scraper_health.lock")  # outside the repo tree
ROLLING_WINDOW = 60  # keep the last N entries per league, not unbounded


def _read_list(path: Path) -> list:
    try:
        data = json.loads(path.read_text()) if path.exists() else []
        return data if isinstance(data, list) else []
    except Exception:
        return []


def trim_records(records: list) -> list:
    """Last ROLLING_WINDOW entries PER LEAGUE (not a flat trim of the whole file, which could let one noisy league's frequent
    runs push another league's history out entirely), oldest first."""
    by_league: dict[str, list[dict]] = {}
    for rec in records:
        if isinstance(rec, dict):
            by_league.setdefault(rec.get("league", "?"), []).append(rec)
    trimmed: list[dict] = []
    for recs in by_league.values():
        trimmed.extend(recs[-ROLLING_WINDOW:])
    trimmed.sort(key=lambda r: r.get("ts", ""))
    return trimmed


def merge_health(upstream: list, mine: list) -> list:
    """Union of two copies of the log (identity = league + ts), trimmed.  Used when a push races another league's push."""
    seen = set()
    out = []
    for rec in list(upstream) + list(mine):
        if not isinstance(rec, dict):
            continue
        k = (rec.get("league"), rec.get("ts"))
        if k in seen:
            continue
        seen.add(k)
        out.append(rec)
    out.sort(key=lambda r: r.get("ts", ""))
    return trim_records(out)


def log_scrape(league: str, fixtures: int, results: int) -> None:
    """Append one record for this run to docs/scraper_health.json.

    Best-effort only -- this must never be the thing that breaks a real
    scrape/data-write it's piggybacking on. Callers also wrap their own
    call to this in try/except (belt-and-suspenders, explicit ask), but
    this function never raises on its own either.
    """
    lock_fh = None
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        if fcntl is not None:
            lock_fh = open(LOCK_PATH, "w")
            fcntl.flock(lock_fh, fcntl.LOCK_EX)
        existing = _read_list(LOG_PATH)
        existing.append({
            "league": league,
            "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "fixtures": fixtures,
            "results": results,
        })
        tmp = LOG_PATH.with_name(LOG_PATH.name + f".tmp{os.getpid()}")
        tmp.write_text(json.dumps(trim_records(existing), indent=2))
        os.replace(tmp, LOG_PATH)
    except Exception:
        pass
    finally:
        if lock_fh is not None:
            try:
                lock_fh.close()  # releases the flock (the lock file itself is left in place: unlinking it would let two processes lock different inodes)
            except Exception:
                pass


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)


def commit_and_push(message: str = "chore: scraper health log", root: Path | None = None, attempts: int = 4,
                    remote: str = "origin", branch: str = "main", sleep=time.sleep) -> bool:
    """Commit + push ONLY docs/scraper_health.json (best effort, never raises).  -> True when the file is on the remote (or had
    nothing new), False if it could not be pushed.  Call it right after the fetch script's own schedule-file push.

    On a push race it first rebases; if that conflicts (another league changed the same file) it fast-forwards to the remote copy,
    re-applies this run's records on top (merge_health) as a fresh commit and pushes that."""
    root = Path(root) if root else ROOT
    path = root / HEALTH_REL
    try:
        if not path.exists():
            return False
        _git(root, "add", HEALTH_REL)
        if _git(root, "diff", "--cached", "--quiet", "--", HEALTH_REL).returncode == 0:
            return True  # nothing new to record
        if _git(root, "commit", "-q", "-m", message, "--", HEALTH_REL).returncode != 0:
            return False
        for attempt in range(attempts):
            if _git(root, "pull", "--rebase", "--autostash", remote, branch).returncode == 0:
                if _git(root, "push", remote, branch).returncode == 0:
                    return True
            else:
                # Conflict: another league changed this same file.  Drop our commit, fast-forward to the remote, then re-apply OUR
                # records on top of the remote's list (union) as a fresh commit; the next loop pass pushes it.
                _git(root, "rebase", "--abort")
                mine = _read_list(path)
                _git(root, "reset", "--soft", "HEAD~1")          # undo only OUR commit
                _git(root, "reset", "-q", "--", HEALTH_REL)      # unstage the file ...
                path.unlink()                                    # (nothing local is lost: `mine`; also clears an untracked copy that would block the pull)
                _git(root, "checkout", "--", HEALTH_REL)         # restore the tracked pre-commit version, if there is one
                if _git(root, "pull", "--rebase", "--autostash", remote, branch).returncode != 0:
                    _git(root, "rebase", "--abort")
                    sleep(2 + attempt * 2)
                    continue
                merged = merge_health(_read_list(path), mine)
                tmp = path.with_name(path.name + f".tmp{os.getpid()}")
                tmp.write_text(json.dumps(merged, indent=2))
                os.replace(tmp, path)
                _git(root, "add", HEALTH_REL)
                if _git(root, "commit", "-q", "-m", message, "--", HEALTH_REL).returncode != 0:
                    return False
            sleep(2 + attempt * 2)
        return False
    except Exception:
        return False
