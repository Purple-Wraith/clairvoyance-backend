#!/usr/bin/env python3
"""Offline tests for the pre-lock `data-nhl` job: scripts/lock_prep.py JOBS entry, scripts/_nhl_core.py merge rules and
scripts/clairvoyance_update.py `--only-nhl-core` (no network: the fetch functions are replaced with fakes).

    /usr/bin/python3 scripts/test_lock_prep_data.py      # the run_nhl_core_refresh tests need bs4/lxml (else they are skipped)
"""
from __future__ import annotations

import contextlib
import copy
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
import _nhl_core as N  # noqa: E402
import lock_prep as LP  # noqa: E402


def standings(n=32):
    return {f"T{i:02d}": {"w": i, "l": 1, "otl": 0, "pts": 2 * i, "gf": 3, "ga": 2, "gd": 1, "row": i, "div": "A", "conf": "E"}
            for i in range(n)}


def base_doc():
    return {
        "generated": "2026-10-03T09:00:00+00:00",
        "nba": {"standings": {"BOS": {"w": 1}}, "today": [{"x": 1}]},
        "nhl": {"today": [{"g": 1}], "standings": {"OLD": {"w": 0}}, "edge": {"goalies": {"OLD": 1}}, "roster": {"a": 1}},
        "mp": {"teams": {"OLD": {"xgf": 1.0}}},
        "injuries": {"nhl": [{"name": "old"}], "nba": [{"name": "keep"}]},
        "bestBets": [{"pick": "keep"}],
    }


class LockPrepJob(unittest.TestCase):
    def test_job_registered(self):
        argv, files = LP.JOBS["data-nhl"]
        self.assertEqual(argv, ["scripts/clairvoyance_update.py", "--only-nhl-core"])
        self.assertEqual(files, ["docs/data.json"])
        self.assertEqual(LP.expand("nhl,nfl,data-nhl"), ["nhl", "nfl", "data-nhl"])
        self.assertEqual(LP.expand("hockey,nhl,data-nhl")[-2:], ["nhl", "data-nhl"])

    def test_dry_run_lists_it_and_touches_nothing(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), mock.patch.object(sys, "argv", ["lock_prep.py", "--jobs", "nhl,data-nhl", "--dry-run"]):
            rc = LP.main()
        self.assertEqual(rc, 0)
        self.assertIn("--only-nhl-core", buf.getvalue())

    def test_commit_file_list_includes_data_json(self):
        calls = []

        def fake_run(cmd, *a, **k):
            calls.append(list(cmd))
            r = mock.Mock()
            r.returncode = 0 if "--quiet" not in cmd else 1  # diff --cached --quiet -> 1 means "there is something staged"
            return r

        with mock.patch.object(LP.subprocess, "run", side_effect=fake_run), mock.patch.object(LP.time, "sleep"):
            LP.commit_and_push(["nhl", "data-nhl"])
        add = next(c for c in calls if "add" in c)
        self.assertIn("docs/data.json", add)
        self.assertIn("docs/nhl_schedule.json", add)
        commit = next(c for c in calls if "commit" in c)
        self.assertIn("data-nhl", commit[-1])

    def test_freshness_report_reads_core_stamp(self):
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "docs").mkdir()
            (Path(td) / "docs" / "data.json").write_text(json.dumps({"nhlCoreAt": "2026-10-03T12:00:00+00:00"}))
            out = io.StringIO()
            with mock.patch.object(LP, "ROOT", Path(td)), contextlib.redirect_stdout(out):
                LP.freshness_report(["data-nhl"])
            self.assertIn("nhlCoreAt", out.getvalue())

    def test_workflows_run_the_job(self):
        a = (ROOT / ".github/workflows/auto-lock-settle.yml").read_text()
        w = (ROOT / ".github/workflows/lock-watchdog.yml").read_text()
        self.assertIn("lock_prep.py --jobs nhl,nfl,nba,data-nhl", a)
        self.assertIn("lock_prep.py --jobs hockey,nhl,nba,data-nhl", w)
        # clairvoyance_update.py imports bs4 at module level: the runner must install it (else a pip fallback costs time / can fail)
        self.assertIn("beautifulsoup4", a)
        self.assertIn("beautifulsoup4", w)


class MergeRules(unittest.TestCase):
    def test_replaces_only_its_parts(self):
        d = base_doc()
        before = copy.deepcopy(d)
        out, changed = N.merge_nhl_core(d, standings(), {"goalies": {"NEW": 1}, "teamRates": {}}, {"teams": {"NEW": {}}},
                                        {"players": {"p": 1}}, [{"name": "new"}], "2026-10-03T20:00:00+00:00")
        self.assertEqual(changed, ["standings", "edge", "skaterValue", "mp", "injuries.nhl"])
        self.assertEqual(out["nhlCoreAt"], "2026-10-03T20:00:00+00:00")
        self.assertEqual(len(out["nhl"]["standings"]), 32)
        self.assertEqual(out["injuries"]["nhl"], [{"name": "new"}])
        # everything else untouched
        self.assertEqual(out["nba"], before["nba"])
        self.assertEqual(out["bestBets"], before["bestBets"])
        self.assertEqual(out["generated"], before["generated"])          # the full refresh's own stamp is NOT touched
        self.assertEqual(out["nhl"]["today"], before["nhl"]["today"])
        self.assertEqual(out["nhl"]["roster"], before["nhl"]["roster"])
        self.assertEqual(out["injuries"]["nba"], before["injuries"]["nba"])

    def test_short_or_empty_fetch_keeps_old_value(self):
        d = base_doc()
        before = copy.deepcopy(d)
        out, changed = N.merge_nhl_core(d, standings(N.NHL_CORE_MIN_STANDINGS - 1), {}, {}, {"players": {}}, [], "x")
        self.assertEqual(changed, [])
        self.assertEqual(out, before)           # nothing replaced, and no stamp added
        self.assertNotIn("nhlCoreAt", out)

    def test_partial_success_stamps(self):
        out, changed = N.merge_nhl_core(base_doc(), standings(), {}, {}, {}, [], "S")
        self.assertEqual(changed, ["standings"])
        self.assertEqual(out["nhlCoreAt"], "S")
        self.assertEqual(out["mp"], base_doc()["mp"])

    def test_refuses_doc_without_nhl(self):
        with self.assertRaises(ValueError):
            N.merge_nhl_core({"nba": {}}, standings(), {}, {}, {}, [], "S")

    def test_creates_injuries_dict_when_missing(self):
        d = base_doc()
        d.pop("injuries")
        out, changed = N.merge_nhl_core(d, {}, {}, {}, {}, [{"name": "n"}], "S")
        self.assertEqual(out["injuries"], {"nhl": [{"name": "n"}]})

    def test_atomic_write(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "data.json"
            p.write_text("old")
            N.atomic_write_text(p, "new")
            self.assertEqual(p.read_text(), "new")
            self.assertEqual([x.name for x in Path(td).iterdir()], ["data.json"])   # no temp file left behind

    def test_real_data_json_round_trips_byte_for_byte(self):
        """The merge is read-modify-write through json.loads/json.dumps(indent=2): the committed data.json must survive that
        unchanged, otherwise every lock slot would rewrite the whole 680 KB file (and 'every other key byte-for-byte' would be false)."""
        raw = (ROOT / "docs" / "data.json").read_text()
        self.assertEqual(json.dumps(json.loads(raw), indent=2), raw)

    def test_untouched_keys_serialize_identically(self):
        raw = (ROOT / "docs" / "data.json").read_text()
        d = json.loads(raw)
        orig = {k: json.dumps(v, indent=2) for k, v in d.items()}
        orig_nhl = {k: json.dumps(v, indent=2) for k, v in d["nhl"].items()}
        out, changed = N.merge_nhl_core(d, standings(), {"goalies": {"X": 1}}, {}, {}, [], "S")
        for k in orig:
            if k in ("nhl", "nhlCoreAt"):          # nhlCoreAt is the stamp merge_nhl_core deliberately rewrites ("S" here); the real file may already carry one
                continue
            self.assertEqual(json.dumps(out[k], indent=2), orig[k], k)
        for k in orig_nhl:
            if k in ("standings", "edge"):
                continue
            self.assertEqual(json.dumps(out["nhl"][k], indent=2), orig_nhl[k], "nhl." + k)


HAVE_BS4 = importlib.util.find_spec("bs4") is not None and importlib.util.find_spec("lxml") is not None


@unittest.skipUnless(HAVE_BS4, "bs4/lxml not installed in this interpreter (clairvoyance_update.py would pip-install them)")
class RunRefresh(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("cu_core_test", HERE / "clairvoyance_update.py")
        cls.cu = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.cu)

    def _patch(self, **fakes):
        cu = self.cu
        defaults = dict(
            fetch_nhl_standings=lambda: standings(),
            fetch_nhl_edge=lambda: {"goalies": {"NEW": 1}, "teamRates": {"A": 1}},
            fetch_moneypuck=lambda: {"teams": {"NEW": {}}},
            fetch_nhl_skater_value=lambda: {"players": {"p": 1}},
            fetch_espn_injuries=lambda path, key: [{"name": "fresh"}],
        )
        defaults.update(fakes)
        stack = contextlib.ExitStack()
        for name, fn in defaults.items():
            stack.enter_context(mock.patch.object(cu, name, fn))
        return stack

    def _doc(self, td):
        p = Path(td) / "data.json"
        p.write_text(json.dumps(base_doc(), indent=2))
        return p

    def test_refresh_merges_and_stamps(self):
        with tempfile.TemporaryDirectory() as td, contextlib.redirect_stdout(io.StringIO()):
            p = self._doc(td)
            with self._patch():
                rc = self.cu.run_nhl_core_refresh(p)
            self.assertEqual(rc, 0)
            d = json.loads(p.read_text())
            self.assertEqual(len(d["nhl"]["standings"]), 32)
            self.assertEqual(d["injuries"]["nhl"], [{"name": "fresh"}])
            self.assertEqual(d["bestBets"], base_doc()["bestBets"])
            self.assertIn("nhlCoreAt", d)
            self.assertEqual([x.name for x in Path(td).iterdir()], ["data.json"])

    def test_one_fetch_raising_does_not_block_the_others(self):
        def boom():
            raise RuntimeError("nhl api down")
        with tempfile.TemporaryDirectory() as td, contextlib.redirect_stdout(io.StringIO()):
            p = self._doc(td)
            with self._patch(fetch_nhl_edge=boom):
                self.assertEqual(self.cu.run_nhl_core_refresh(p), 0)
            d = json.loads(p.read_text())
            self.assertEqual(d["nhl"]["edge"], base_doc()["nhl"]["edge"])     # kept
            self.assertEqual(len(d["nhl"]["standings"]), 32)                   # refreshed

    def test_everything_failing_leaves_file_byte_identical(self):
        with tempfile.TemporaryDirectory() as td, contextlib.redirect_stdout(io.StringIO()):
            p = self._doc(td)
            before = p.read_bytes()
            with self._patch(fetch_nhl_standings=lambda: {}, fetch_nhl_edge=lambda: {}, fetch_moneypuck=lambda: {},
                             fetch_nhl_skater_value=lambda: {}, fetch_espn_injuries=lambda a, b: []):
                self.assertEqual(self.cu.run_nhl_core_refresh(p), 0)
            self.assertEqual(p.read_bytes(), before)

    def test_unreadable_or_missing_file_is_fail_open(self):
        with tempfile.TemporaryDirectory() as td, contextlib.redirect_stdout(io.StringIO()):
            missing = Path(td) / "nope.json"
            with self._patch():
                self.assertEqual(self.cu.run_nhl_core_refresh(missing), 0)
            self.assertFalse(missing.exists())
            bad = Path(td) / "bad.json"
            bad.write_text("{not json")
            with self._patch():
                self.assertEqual(self.cu.run_nhl_core_refresh(bad), 0)
            self.assertEqual(bad.read_text(), "{not json")

    def test_dry_run_writes_nothing(self):
        with tempfile.TemporaryDirectory() as td, contextlib.redirect_stdout(io.StringIO()):
            p = self._doc(td)
            before = p.read_bytes()
            with self._patch():
                self.assertEqual(self.cu.run_nhl_core_refresh(p, dry_run=True), 0)
            self.assertEqual(p.read_bytes(), before)

    def test_cli_entry_exits_zero(self):
        with tempfile.TemporaryDirectory() as td, contextlib.redirect_stdout(io.StringIO()):
            p = self._doc(td)
            argv = ["clairvoyance_update.py", "--only-nhl-core", "--data-json", str(p)]
            with self._patch(), mock.patch.object(sys, "argv", argv):
                with self.assertRaises(SystemExit) as cm:
                    self.cu.main()
            self.assertEqual(cm.exception.code, 0)
            self.assertIn("nhlCoreAt", json.loads(p.read_text()))


if __name__ == "__main__":
    unittest.main(verbosity=1)
