#!/usr/bin/env python3
"""Unit tests for the ledger source + backup logic in scripts/auto_lock_settle.py (fake page, no browser, no network, no git).

    python3 scripts/test_degraded_ledger.py

Covers: FULL vs HYBRID vs DEGRADED loading, tombstones in the hybrid window, a second load never overwriting this run's changes, the verify count in
degraded mode, the three-way backup merge (origin newer / older), the validation gate, the meta file, and the reconcile push after a degraded period.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import auto_lock_settle as A  # noqa: E402

NOW = lambda: datetime.now(timezone.utc)  # noqa: E731
ISO = lambda d: d.strftime("%Y-%m-%dT%H:%M:%SZ")  # noqa: E731


class FakePage:
    def __init__(self, supabase_ok=True, remote=None, window_rows=None):
        self.supabase_ok, self.remote, self.window_rows = supabase_ok, remote or [], window_rows or []
        self.ledger: list = []
        self.posted: list = []

    def evaluate(self, js, arg=None):
        if js == A._JS_FULL_PULL:
            if not self.supabase_ok:
                return -1
            self.ledger = list(self.remote)
            return len(self.ledger)
        if js == A._JS_WINDOW_PULL:
            return None if not self.supabase_ok else self.window_rows
        if "saveP(preds)" in js and arg is not None:
            self.ledger = list(arg)
            return None
        if "const m = new Map(getP()" in js:
            m = {p["id"]: p for p in self.ledger}
            m.update({p["id"]: p for p in arg})
            self.ledger = list(m.values())
            return None
        if "_supabaseBetRow" in js:
            self.posted = list(arg)
            return True
        if "select=id&date=eq." in js:
            return None if not self.supabase_ok else [3 for _ in arg]
        if js.strip() == "() => getP().length":
            return len(self.ledger)
        if js.strip() == "() => getP()":
            return [dict(p) for p in self.ledger]
        if "ds.map(d => getP()" in js:
            return [len([p for p in self.ledger if p.get("date") == d and p.get("outcome") == "pending"]) for d in arg]
        raise AssertionError(f"unexpected evaluate: {js[:80]}")


def pick(i, outcome="pending", date="2026-10-03", **kw):
    return {"id": i, "date": date, "outcome": outcome, **kw}


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self._saved = (A.ROOT, A.LEDGER_DEGRADED, A.LEDGER_MODE, A._FULL_PULL_THIS_RUN, A._RECONCILED_THIS_RUN, A._LEDGER_LOADED_AT,
                       dict(A._DEGRADED_INITIAL), A._read_origin_state)
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "docs").mkdir()
        A.ROOT = self.root
        A.LEDGER_DEGRADED, A.LEDGER_MODE, A._FULL_PULL_THIS_RUN, A._RECONCILED_THIS_RUN, A._LEDGER_LOADED_AT = False, "none", False, False, None
        A._DEGRADED_INITIAL = {}
        A._read_origin_state = lambda: (None, {})
        self.backup = [pick("a"), pick("b", "win", "2026-09-01"), pick("c", "loss", "2026-08-01")]

    def tearDown(self):
        (A.ROOT, A.LEDGER_DEGRADED, A.LEDGER_MODE, A._FULL_PULL_THIS_RUN, A._RECONCILED_THIS_RUN, A._LEDGER_LOADED_AT,
         A._DEGRADED_INITIAL, A._read_origin_state) = self._saved
        self.tmp.cleanup()

    def write_backup(self, preds=None, meta=None):
        (self.root / "docs" / "picks_backup.json").write_text(json.dumps(preds if preds is not None else self.backup))
        if meta is not None:
            (self.root / "docs" / "picks_backup_meta.json").write_text(json.dumps(meta))

    # ---- loading ----
    def test_no_meta_means_a_full_pull(self):
        self.write_backup()
        pg = FakePage(remote=[pick("r1"), pick("r2")])
        self.assertEqual(A.load_bet_ledger(pg), 2)
        self.assertEqual(A.LEDGER_MODE, "full")
        self.assertTrue(A._FULL_PULL_THIS_RUN)

    def test_fresh_meta_means_hybrid_with_tombstones(self):
        self.write_backup(meta={"last_full_supabase_sync": ISO(NOW() - timedelta(hours=2))})
        rows = [{"id": "a", "outcome": "win", "raw": pick("a", "win")},
                {"id": "d", "outcome": "pending", "raw": pick("d")},
                {"id": "b", "outcome": "_removed", "raw": pick("b", "_removed")}]
        pg = FakePage(window_rows=rows)
        A.load_bet_ledger(pg)
        got = {p["id"]: p for p in pg.ledger}
        self.assertEqual(A.LEDGER_MODE, "hybrid")
        self.assertEqual(set(got), {"a", "c", "d"})          # b tombstoned, d added, c kept from the backup base
        self.assertEqual(got["a"]["outcome"], "win")          # window row overlays the base
        self.assertFalse(A._FULL_PULL_THIS_RUN)

    def test_stale_meta_or_needs_reconcile_forces_full(self):
        self.write_backup(meta={"last_full_supabase_sync": ISO(NOW() - timedelta(hours=30))})
        pg = FakePage(remote=[pick("r1")])
        A.load_bet_ledger(pg)
        self.assertEqual(A.LEDGER_MODE, "full")

    def test_failed_pull_falls_back_to_backup(self):
        self.write_backup()
        pg = FakePage(supabase_ok=False)
        self.assertEqual(A.load_bet_ledger(pg), 3)
        self.assertTrue(A.LEDGER_DEGRADED)
        self.assertEqual(A.LEDGER_MODE, "degraded")

    def test_second_load_keeps_this_runs_changes(self):
        self.write_backup()
        pg = FakePage(supabase_ok=False)
        A.load_bet_ledger(pg)
        pg.ledger.append(pick("new"))
        self.assertEqual(A.load_bet_ledger(pg), 4)

    def test_missing_backup_still_raises(self):
        with self.assertRaises(RuntimeError):
            A.load_bet_ledger(FakePage(supabase_ok=False))

    def test_verify_count_uses_in_page_ledger_when_degraded(self):
        self.write_backup()
        pg = FakePage(supabase_ok=False)
        A.load_bet_ledger(pg)
        pg.ledger.append(pick("new"))
        self.assertEqual(A.count_pending_for_dates(pg, ["2026-10-03", "2026-10-02"]), [2, 0])

    # ---- merge + validation ----
    def test_three_way_merge_origin_newer_keeps_others_changes(self):
        self.write_backup()
        pg = FakePage(supabase_ok=False)
        A.load_bet_ledger(pg)
        pg.ledger.append(pick("mine"))                                              # added by this run
        pg.ledger[1] = dict(pg.ledger[1], outcome="loss")                           # changed by this run (b)
        origin = self.backup + [pick("other")]
        origin[0] = dict(origin[0], outcome="win")                                  # someone else settled a
        merged = {p["id"]: p for p in A._three_way_merge(list(pg.ledger), origin, origin_newer=True)}
        self.assertEqual(set(merged), {"a", "b", "c", "mine", "other"})
        self.assertEqual(merged["a"]["outcome"], "win")                             # untouched here -> origin's newer copy
        self.assertEqual(merged["b"]["outcome"], "loss")                            # touched here -> this run's copy

    def test_three_way_merge_origin_older_keeps_supabase_truth(self):
        self.write_backup()
        pg = FakePage(remote=[pick("a", "win"), pick("b", "win", "2026-09-01")])
        A.load_bet_ledger(pg)                                                        # Supabase says a is already settled
        stale_origin = [pick("a"), pick("b", "win", "2026-09-01")]
        merged = {p["id"]: p for p in A._three_way_merge(list(pg.ledger), stale_origin, origin_newer=False)}
        self.assertEqual(merged["a"]["outcome"], "win")

    def test_full_pull_drops_picks_the_owner_removed(self):
        self.write_backup()
        pg = FakePage(remote=[pick("a"), pick("b", "win", "2026-09-01")])           # Supabase no longer has c (removed)
        A.load_bet_ledger(pg)
        merged = {p["id"] for p in A._three_way_merge(list(pg.ledger), self.backup, origin_newer=False)}
        self.assertEqual(merged, {"a", "b"})
        kept = {p["id"] for p in A._three_way_merge(list(pg.ledger), self.backup, origin_newer=True)}
        self.assertEqual(kept, {"a", "b", "c"})                                      # origin newer: c may be someone's new pick

    def test_manual_lock_relay_merged_only_in_degraded_mode_and_reconcile(self):
        self.write_backup()
        (self.root / "docs" / "manual_locks.json").write_text(json.dumps({"picks": [pick("m1", lockOrigin="manual"), pick("a", "win")]}))
        healthy = FakePage(remote=[pick("r1")])
        A.load_bet_ledger(healthy)
        self.assertEqual({p["id"] for p in healthy.ledger}, {"r1"})                  # healthy: relay file is NOT merged (app pushes it itself)
        A.LEDGER_DEGRADED, A.LEDGER_MODE = False, "none"
        pg = FakePage(supabase_ok=False)
        A.load_bet_ledger(pg)
        got = {p["id"]: p for p in pg.ledger}
        self.assertIn("m1", got)                                                     # degraded: merged
        self.assertEqual(got["a"]["outcome"], "win")                                 # relay's settled result beats the pending backup copy

    def test_reconcile_also_pushes_relay_picks(self):
        self.write_backup([pick("a")], meta={"needs_reconcile": True, "last_full_supabase_sync": ISO(NOW())})
        (self.root / "docs" / "manual_locks.json").write_text(json.dumps({"picks": [pick("m1", lockOrigin="manual")]}))
        pg = FakePage(remote=[pick("a")])
        A.load_bet_ledger(pg)
        self.assertEqual(set(pg.posted), {"m1"})

    def test_validation_gate(self):
        origin = [pick(str(i)) for i in range(100)]
        self.assertIsNone(A._validate_backup(list(origin), origin))
        self.assertIn("shrink", A._validate_backup(origin[:90], origin))
        self.assertIn("duplicate", A._validate_backup([pick("x"), pick("x")], None))
        self.assertIn("without an id", A._validate_backup([{"date": "d", "outcome": "pending"}], None))
        self.assertEqual(A._validate_backup([], None), "empty")

    # ---- writing + meta + reconcile ----
    def test_write_backup_sorted_with_meta_and_reconcile_flag(self):
        self.write_backup()
        pg = FakePage(supabase_ok=False)
        A.load_bet_ledger(pg)
        pg.ledger.append(pick("0first"))
        n = A.write_ledger_backup(pg)
        self.assertEqual(n, 4)
        ids = [p["id"] for p in json.loads((self.root / "docs" / "picks_backup.json").read_text())]
        self.assertEqual(ids, sorted(ids))
        meta = json.loads((self.root / "docs" / "picks_backup_meta.json").read_text())
        self.assertEqual(meta["source"], "degraded")
        self.assertTrue(meta["needs_reconcile"])

    def test_healthy_full_run_records_last_full_sync(self):
        self.write_backup()
        pg = FakePage(remote=[pick("r1"), pick("r2")])
        A.load_bet_ledger(pg)
        A.write_ledger_backup(pg)
        meta = json.loads((self.root / "docs" / "picks_backup_meta.json").read_text())
        self.assertEqual(meta["source"], "full")
        self.assertIsNotNone(meta["last_full_supabase_sync"])
        self.assertFalse(meta["needs_reconcile"])

    def test_refuses_to_shrink_the_backup(self):
        big = [pick(str(i)) for i in range(100)]
        self.write_backup(big)
        A._read_origin_state = lambda: (big, {})
        pg = FakePage(remote=[pick("only")])
        A.load_bet_ledger(pg)
        A.write_ledger_backup(pg)
        self.assertEqual(len(json.loads((self.root / "docs" / "picks_backup.json").read_text())), 100)

    def test_reconcile_pushes_backup_only_and_settled_picks_then_clears_flag(self):
        degraded_backup = [pick("a", "win"), pick("only-backup"), pick("same")]
        self.write_backup(degraded_backup, meta={"needs_reconcile": True, "last_full_supabase_sync": ISO(NOW())})
        pg = FakePage(remote=[pick("a"), pick("same"), pick("only-supabase")])      # Supabase: a still pending, no 'only-backup'
        A.load_bet_ledger(pg)                                                        # needs_reconcile forces a full pull, then reconciles
        self.assertTrue(A._RECONCILED_THIS_RUN)
        self.assertEqual(set(pg.posted), {"a", "only-backup"})
        got = {p["id"]: p for p in pg.ledger}
        self.assertEqual(got["a"]["outcome"], "win")
        self.assertIn("only-supabase", got)
        A.write_ledger_backup(pg)
        meta = json.loads((self.root / "docs" / "picks_backup_meta.json").read_text())
        self.assertFalse(meta["needs_reconcile"])
        self.assertIsNotNone(meta["last_reconcile_at"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
