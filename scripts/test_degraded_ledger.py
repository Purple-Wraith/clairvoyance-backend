#!/usr/bin/env python3
"""Unit tests for the Supabase-unavailable ("degraded") ledger fallback in scripts/auto_lock_settle.py (fake page, no browser, no network).

    python3 scripts/test_degraded_ledger.py

Covers: a failed Supabase pull falls back to docs/picks_backup.json; a second load does not overwrite this run's in-page changes; the verify count
uses the in-page ledger in degraded mode; the merge into the backup applies ONLY this run's added/changed picks on top of origin's newest copy.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import auto_lock_settle as A  # noqa: E402


class FakePage:
    """evaluate() understands just the few snippets the ledger helpers send."""

    def __init__(self, supabase_ok: bool):
        self.supabase_ok = supabase_ok
        self.ledger: list = []
        self.saved: list = []

    def evaluate(self, js, arg=None):
        if "SUPABASE_URL + '/rest/v1/bets?select=raw" in js:
            if not self.supabase_ok:
                return -1
            self.ledger = [{"id": "remote-1"}]
            return 1
        if "saveP(preds)" in js and arg is not None:
            self.ledger = list(arg)
            self.saved.append(list(arg))
            return None
        if "select=id&date=eq." in js:
            return None if not self.supabase_ok else [3 for _ in arg]
        if js.strip() == "() => getP().length":
            return len(self.ledger)
        if js.strip() == "() => getP()":
            return list(self.ledger)
        if "ds.map(d => getP()" in js:
            return [len([p for p in self.ledger if p.get("date") == d and p.get("outcome") == "pending"]) for d in arg]
        raise AssertionError(f"unexpected evaluate: {js[:80]}")


class DegradedLedger(unittest.TestCase):
    def setUp(self):
        self._root, self._flag, self._init = A.ROOT, A.LEDGER_DEGRADED, dict(A._DEGRADED_INITIAL)
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        (root / "docs").mkdir()
        self.backup = [{"id": "a", "date": "2026-10-03", "outcome": "pending"}, {"id": "b", "date": "2026-10-02", "outcome": "win"}]
        (root / "docs" / "picks_backup.json").write_text(json.dumps(self.backup))
        A.ROOT = root
        A.LEDGER_DEGRADED = False
        A._DEGRADED_INITIAL = {}

    def tearDown(self):
        A.ROOT, A.LEDGER_DEGRADED, A._DEGRADED_INITIAL = self._root, self._flag, self._init
        self.tmp.cleanup()

    def test_healthy_pull_is_untouched(self):
        pg = FakePage(supabase_ok=True)
        self.assertEqual(A.load_bet_ledger(pg), 1)
        self.assertFalse(A.LEDGER_DEGRADED)

    def test_failed_pull_falls_back_to_backup(self):
        pg = FakePage(supabase_ok=False)
        self.assertEqual(A.load_bet_ledger(pg), 2)
        self.assertTrue(A.LEDGER_DEGRADED)
        self.assertEqual([p["id"] for p in pg.ledger], ["a", "b"])

    def test_second_load_keeps_this_runs_changes(self):
        pg = FakePage(supabase_ok=False)
        A.load_bet_ledger(pg)
        pg.ledger.append({"id": "new", "date": "2026-10-03", "outcome": "pending"})
        self.assertEqual(A.load_bet_ledger(pg), 3)          # not reloaded from the older backup
        self.assertEqual(len(pg.saved), 1)

    def test_missing_backup_still_raises(self):
        (A.ROOT / "docs" / "picks_backup.json").unlink()
        with self.assertRaises(RuntimeError):
            A.load_bet_ledger(FakePage(supabase_ok=False))

    def test_verify_count_uses_in_page_ledger_when_degraded(self):
        pg = FakePage(supabase_ok=False)
        A.load_bet_ledger(pg)
        pg.ledger.append({"id": "new", "date": "2026-10-03", "outcome": "pending"})
        self.assertEqual(A.count_pending_for_dates(pg, ["2026-10-03", "2026-10-02"]), [2, 0])

    def test_merge_applies_only_this_runs_diff_on_top_of_origin(self):
        pg = FakePage(supabase_ok=False)
        A.load_bet_ledger(pg)
        pg.ledger.append({"id": "new", "date": "2026-10-03", "outcome": "pending"})        # added this run
        pg.ledger[1] = dict(pg.ledger[1], outcome="loss")                                     # changed this run
        origin = self.backup + [{"id": "from-other-workflow", "date": "2026-10-03", "outcome": "pending"}]
        A._read_origin_backup = lambda: origin                                                # another workflow committed meanwhile
        merged = {p["id"]: p for p in A._merge_degraded_backup(list(pg.ledger))}
        self.assertEqual(set(merged), {"a", "b", "new", "from-other-workflow"})
        self.assertEqual(merged["b"]["outcome"], "loss")
        self.assertEqual(merged["a"]["outcome"], "pending")                                   # untouched by this run -> origin's copy

    def test_merge_without_origin_returns_the_run_ledger(self):
        pg = FakePage(supabase_ok=False)
        A.load_bet_ledger(pg)
        A._read_origin_backup = lambda: None
        self.assertEqual(A._merge_degraded_backup(list(pg.ledger)), pg.ledger)


if __name__ == "__main__":
    unittest.main(verbosity=2)
