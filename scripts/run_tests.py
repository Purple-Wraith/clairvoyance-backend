#!/usr/bin/env python3
"""Runs the repo's test suite the way CI does: validate.py, then every scripts/test_*.py in its own process (so one file's
monkeypatching can't leak into another), each with a timeout. Prints a one-line result per file and exits non-zero if any failed.

    python3 scripts/run_tests.py            # everything
    python3 scripts/run_tests.py alt email  # only files whose name contains one of these words

SKIP lists tests that cannot be reproduced in a clean checkout (they depend on the live ledger's current contents)."""
from __future__ import annotations
import glob, os, subprocess, sys, time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKIP = {"test_settle_pass_sim.py": "fixture is the live ledger's 10-02 pending picks (settled since)"}
TIMEOUT_S = 240


def run(cmd: list[str], label: str) -> bool:
    t = time.time()
    try:
        r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=TIMEOUT_S)
        ok, tail = r.returncode == 0, (r.stderr or r.stdout).strip().split("\n")[-1][:90]
        out = r.stdout + r.stderr
    except subprocess.TimeoutExpired:
        ok, tail, out = False, f"TIMEOUT after {TIMEOUT_S}s", ""
    print(f"{'PASS' if ok else 'FAIL'}  {label:<38}{time.time() - t:5.0f}s  {tail}", flush=True)
    if not ok and out:
        print("\n".join("      " + l for l in out.strip().split("\n")[-25:]), flush=True)
    return ok


def main() -> int:
    words = [w.lower() for w in sys.argv[1:]]
    results = []
    if not words:
        results.append(run([sys.executable, "scripts/validate.py"], "validate.py"))
    for f in sorted(glob.glob(os.path.join(ROOT, "scripts", "test_*.py"))):
        name = os.path.basename(f)
        if words and not any(w in name for w in words):
            continue
        if name in SKIP:
            print(f"SKIP  {name:<38}       {SKIP[name]}")
            continue
        results.append(run([sys.executable, f"scripts/{name}"], name))
    failed = results.count(False)
    print(f"\n{len(results) - failed}/{len(results)} passed" + (f", {failed} FAILED" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
