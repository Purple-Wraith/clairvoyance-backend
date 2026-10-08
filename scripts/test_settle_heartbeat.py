#!/usr/bin/env python3
"""Quiet-day LAST SETTLE heartbeat: settle_heartbeat.py stamps only lastSettle, and the workflow wires it into the gated settle-only slots (incl. the 12:37Z slot added 2026-10-08).

    python3 scripts/test_settle_heartbeat.py
"""
import json, sys, tempfile, unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import auto_lock_settle as A  # noqa: E402
import settle_heartbeat as H  # noqa: E402

WF = (HERE.parent / ".github" / "workflows" / "auto-lock-settle.yml").read_text()


class Heartbeat(unittest.TestCase):
    def test_stamps_only_last_settle(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); (root / "docs").mkdir()
            (root / "docs" / "automation_status.json").write_text(json.dumps({"lastLock": {"tsMT": "keep"}, "lockPasses": [{"k": 1}]}))
            with mock.patch.object(A, "ROOT", root):
                self.assertEqual(H.main(), 0)
            out = json.loads((root / "docs" / "automation_status.json").read_text())
            self.assertEqual(out["lastLock"], {"tsMT": "keep"})                 # untouched
            self.assertEqual(out["lockPasses"], [{"k": 1}])
            self.assertTrue(out["lastSettle"]["ok"])
            self.assertIn("nothing pending", out["lastSettle"]["detail"])
            self.assertTrue(out["lastSettle"]["tsUTC"].endswith("Z"))


class Workflow(unittest.TestCase):
    def test_new_gated_slot_is_wired_everywhere_a_gated_slot_must_be(self):
        self.assertIn("- cron: '37 12 * * *'", WF)
        self.assertEqual(WF.count("github.event.schedule == '37 12 * * *'"), 1)         # concurrency group 'gated'
        self.assertIn('[ "${{ github.event.schedule }}" = "37 12 * * *" ]', WF)         # the settle-only branch (no lock, no email)

    def test_heartbeat_runs_only_for_live_gated_noops(self):
        i = WF.index("name: Settle heartbeat")
        step = WF[i:i + 1800]
        self.assertIn("steps.flags.outputs.gated == '1'", step)
        self.assertIn("steps.flags.outputs.live_flag == '--live'", step)
        self.assertIn("steps.flags.outputs.should_run == '0' || steps.presettle.outputs.run_heavy == 'skip'", step)
        self.assertIn("python3 scripts/settle_heartbeat.py", step)
        self.assertIn("git add docs/automation_status.json", step)
        self.assertIn("continue-on-error: true", step)


if __name__ == "__main__":
    unittest.main(verbosity=2)
