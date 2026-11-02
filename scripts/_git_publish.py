"""Commit + push one data file from a fetch script, robust against the two things that made the Liiga / NLA refreshes fail (2026-10-23, 10-28): `git push failed after 5 retries`, every attempt rejected as
non-fast-forward.

Cause (reproduced with real git, scripts/test_git_publish.py): the old per-script loop ran `git pull --rebase --autostash` and threw its output away.  When another writer had also changed the same file (the lock
workflows re-scrape odds into these schedule files; an earlier refresh run), the first pull hit a rebase CONFLICT and left the rebase half-finished; every later attempt then failed with "unmerged files" and the
push was rejected as non-fast-forward, five times in a row, with nothing in the log saying why.  This helper rebases with `-X theirs` (this script's fresh scrape wins its own file), aborts a failed rebase before
retrying, logs the rebase's own error, and also deepens a depth-1 checkout first (the same guard publish.sh has) in case history cannot be related.

    publish(paths, message, log) -> True when the commit is on the remote (or there was nothing to commit), False when it could not be pushed.  NEVER raises for an operational problem: the commit stays
    local and the workflow's final Publish step (which also deepens the history) pushes it, exactly as publish.sh promises -- a missed publish self-heals on the next run.
"""
from __future__ import annotations

import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ID = ["-c", "user.name=clairvoyance-bot", "-c", "user.email=bot@clairvoyance.local"]


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True)


def _connect_history(root: Path, remote: str, branch: str) -> bool:
    """True once HEAD and remote/branch share a merge base; deepens a shallow clone a few times to get there."""
    if _git(root, "merge-base", "HEAD", f"{remote}/{branch}").returncode == 0:
        return True
    if _git(root, "rev-parse", "--is-shallow-repository").stdout.strip() != "true":
        return False
    for _ in range(4):
        if _git(root, "fetch", "--quiet", "--deepen=64", remote, branch).returncode != 0:
            return False
        if _git(root, "merge-base", "HEAD", f"{remote}/{branch}").returncode == 0:
            return True
    return False


def publish(paths: list[str], message: str, log=print, root: Path | None = None, attempts: int = 5, remote: str = "origin", branch: str = "main",
            sleep=time.sleep, park=None) -> bool:
    root = Path(root) if root else ROOT
    subprocess.run(["git", "add", *paths], cwd=root, check=True)
    c = _git(root, *ID, "commit", "-q", "-m", message)
    if c.returncode != 0:
        log(f"  nothing to commit ({(c.stdout or c.stderr).strip()[:120]})")
        return True
    restore = (park or _default_park)(root)       # a dirty docs/scraper_health.json would make the rebase refuse to run
    try:
        for attempt in range(1, attempts + 1):
            push = _git(root, "push", "-q", remote, f"HEAD:{branch}")
            if push.returncode == 0:
                log("  pushed")
                return True
            log(f"  push attempt {attempt}/{attempts} failed: {push.stderr.strip()[:160]}")
            _git(root, "fetch", "--quiet", remote, branch)
            if not _connect_history(root, remote, branch):
                log("  could not relate HEAD to the remote history (shallow checkout) -- leaving the commit for the workflow's Publish step")
                return False
            # -X theirs while rebasing = THIS commit's side wins: the file is this script's own fresh scrape, a newer remote copy of it is just an earlier run
            rb = _git(root, *ID, "rebase", "--autostash", "-X", "theirs", f"{remote}/{branch}")
            if rb.returncode == 0:
                continue                           # rebased cleanly: push again straight away
            _git(root, "rebase", "--abort")
            log(f"  rebase onto {remote}/{branch} failed (attempt {attempt}/{attempts}): {(rb.stderr or rb.stdout).strip()[:200]}")
            sleep(2 + attempt * 2)
        log("  WARNING: could not push after retries -- the commit stays local; the workflow's final Publish step retries it")
        return False
    finally:
        restore()


def _default_park(root: Path):
    try:
        from _scraper_health import park_health
        return park_health(root)
    except Exception:
        return lambda: None
