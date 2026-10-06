#!/usr/bin/env python3
"""Mirrors the backend's docs/ site into the mobile repo (Purple-Wraith/Clairvoyance-backend-mobile) -- COMPLETELY.

    python3 scripts/mobile_sync.py <backend checkout> <mobile checkout>

What it does (used by .github/workflows/mobile-sync.yml):
  * every file under <backend>/docs is copied byte-for-byte into <mobile>/docs -- all the schedule / stats / engine-performance / ledger-backup / logo JSON the app fetches by
    relative URL, the images, the social assets, everything. Until 2026-10-06 the workflow copied only 8 hand-picked files, so the mobile site served the app but 404'd on ~64 of the
    data files it fetches (nhl_schedule.json, picks_backup.json, engine_performance.json, ...);
  * EXCEPT app.html / index.html, which are produced by scripts/mobile_transform.py (index.html is a copy of the transformed app.html), and CNAME, which claims the custom domain
    for the backend's own Pages site and must never reach the mobile repo;
  * files that exist in <mobile>/docs but no longer in the backend are removed, so the two cannot drift apart (a stale pinned_card.png had been left behind).
The service worker (sw.js) is copied as-is; the transform renames its cache inside app.html (see mobile_transform.py section 5).
"""
from __future__ import annotations

import filecmp
import shutil
import subprocess
import sys
from pathlib import Path

TRANSFORMED = {"app.html", "index.html"}
NEVER_COPY = {"CNAME"}


def _files(root: Path) -> set[str]:
    return {str(p.relative_to(root)) for p in root.rglob("*") if p.is_file() and ".git" not in p.relative_to(root).parts}


def sync(backend: Path, mobile: Path) -> dict:
    src, dst = backend / "docs", mobile / "docs"
    if not (src / "app.html").exists():
        raise SystemExit(f"no app.html under {src} -- wrong backend checkout?")
    dst.mkdir(parents=True, exist_ok=True)
    want = {f for f in _files(src) if f not in TRANSFORMED and f not in NEVER_COPY}
    copied = 0
    for rel in sorted(want):
        a, b = src / rel, dst / rel
        if b.exists() and filecmp.cmp(a, b, shallow=False):
            continue
        b.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(a, b)
        copied += 1
    removed = []
    for rel in sorted(_files(dst) - want - TRANSFORMED):
        (dst / rel).unlink()
        removed.append(rel)
    for d in sorted((p for p in dst.rglob("*") if p.is_dir()), reverse=True):      # drop directories the removal emptied
        if not any(d.iterdir()):
            d.rmdir()
    r = subprocess.run([sys.executable, str(Path(__file__).resolve().parent / "mobile_transform.py"), str(src / "app.html"), str(dst / "app.html")],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(f"mobile_transform failed: {r.stderr[-400:]}")
    shutil.copyfile(dst / "app.html", dst / "index.html")
    return {"files": len(want), "copied": copied, "removed": removed}


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__)
        return 2
    res = sync(Path(sys.argv[1]), Path(sys.argv[2]))
    print(f"mobile sync: {res['files']} files mirrored ({res['copied']} updated), {len(res['removed'])} stale removed {res['removed'][:5]}; app.html transformed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
