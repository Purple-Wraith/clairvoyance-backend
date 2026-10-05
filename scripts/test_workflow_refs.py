#!/usr/bin/env python3
"""Static checks that workflows and their references agree (no network, no YAML library): a workflow that was merged or renamed must not leave a dangling
reference behind -- a `gh workflow run "<name>"` that matches nothing fails silently in the live-tracker watchdog."""
import re, sys, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WF = ROOT / ".github" / "workflows"


def names():
    out = {}
    for f in WF.glob("*.yml"):
        m = re.search(r"(?m)^name:\s*(.+?)\s*$", f.read_text())
        out[f.name] = m.group(1).strip("'\"") if m else None
    return out


class WorkflowRefs(unittest.TestCase):
    def test_merged_workflows_exist_and_originals_are_gone(self):
        for new in ("hockey-euro-refresh.yml", "nfl-weekly-refresh.yml", "cfb-refresh.yml", "soccer-refresh.yml"):
            self.assertTrue((WF / new).exists(), new)
        for old in ("shl-schedule-refresh.yml", "liiga-schedule-refresh.yml", "nla-schedule-refresh.yml", "extraliga-schedule-refresh.yml",
                    "nfl-stats-weekly.yml", "nfl-roster-weekly.yml", "cfb-stats-weekly.yml", "cfb-rankings-weekly.yml",
                    "soccer-schedule-tomorrow.yml", "opta-soccer-stats-daily.yml", "top-picks-digest.yml"):
            self.assertFalse((WF / old).exists(), old)
        self.assertTrue((WF / "cfb-roster-monthly.yml").exists())          # deliberately kept separate

    def test_every_gh_workflow_run_names_a_real_workflow(self):
        known = set(n for n in names().values() if n)
        for f in WF.glob("*.yml"):
            for ref in re.findall(r'gh workflow run "([^"]+)"', f.read_text()):
                self.assertIn(ref, known, f"{f.name} dispatches '{ref}', which is no workflow's name")

    def test_soccer_watchdog_dispatch_asks_for_the_tomorrow_part(self):
        text = (WF / "live-tracker.yml").read_text()
        self.assertIn('gh workflow run "Soccer Refresh (Tomorrow Schedule + Opta Stats)" -f which=tomorrow', text)
        self.assertEqual(names()["soccer-refresh.yml"], "Soccer Refresh (Tomorrow Schedule + Opta Stats)")

    def test_no_workflow_file_is_referenced_that_does_not_exist(self):
        files = set(names())
        pat = re.compile(r"\b([a-z0-9][a-z0-9-]*\.yml)\b")
        for src in [*(ROOT / "scripts").glob("*.py"), ROOT / "docs" / "app.html"]:
            if src.name.startswith("test_"):
                continue
            for ref in set(pat.findall(src.read_text(errors="ignore"))):
                if ref in ("docker-compose.yml",) or ref.startswith(("config", "pubspec")):
                    continue
                if ref.endswith("-refresh.yml") or ref.endswith("-weekly.yml") or ref.endswith("-daily.yml") or ref.endswith("-lock-early.yml") \
                        or ref.endswith("-lock-evening.yml") or ref.endswith("-watchdog.yml") or ref.endswith("-settle.yml"):
                    # historical mentions in comments are allowed only for the merged-away names listed above; anything else must exist
                    if ref in {"shl-schedule-refresh.yml", "liiga-schedule-refresh.yml", "nla-schedule-refresh.yml", "extraliga-schedule-refresh.yml",
                               "nfl-stats-weekly.yml", "nfl-roster-weekly.yml", "cfb-stats-weekly.yml", "cfb-rankings-weekly.yml",
                               "soccer-schedule-tomorrow.yml", "opta-soccer-stats-daily.yml", "soccer-lock-early.yml", "wnba-props-daily.yml"}:
                        continue
                    self.assertIn(ref, files, f"{src.name} mentions {ref}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
