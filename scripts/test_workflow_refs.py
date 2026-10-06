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

    def test_runners_are_pinned(self):
        """`ubuntu-latest` moves to Ubuntu 26 on 2026-10-19 (browser installs / apt deps could break) -- every job stays on 24.04 until we choose to move."""
        for f in WF.glob("*.yml"):
            self.assertNotIn("ubuntu-latest", f.read_text(), f.name)

    @staticmethod
    def run_trigger_names(fname):
        text = (WF / fname).read_text()
        i = text.index("workflow_run:")
        block = text[i:]
        j = re.search(r"(?m)^  [a-z_]+:", block[len("workflow_run:"):])        # the next trigger key at the same indent ends the list
        block = block[: len("workflow_run:") + (j.start() if j else len(block))]
        return re.findall(r'(?m)^\s*-\s*"([^"]+)"\s*$', block)

    def test_workflow_run_lists_name_real_workflows_and_agree(self):
        """Bot commits (GITHUB_TOKEN) never fire `push` workflows, so Pages and the mobile mirror rely on `workflow_run` allowlists BY NAME -- which silently went stale when four workflows were
        merged/renamed (2026-10-05). Every name must be a real workflow, and the two lists must be identical."""
        known = set(n for n in names().values() if n)
        pages, mobile = self.run_trigger_names("pages-deploy.yml"), self.run_trigger_names("mobile-sync.yml")
        self.assertGreater(len(pages), 10)
        for n in pages + mobile:
            self.assertIn(n, known, f"workflow_run lists a workflow that does not exist: {n}")
        self.assertEqual(sorted(pages), sorted(mobile), "pages-deploy.yml and mobile-sync.yml must trigger on the same workflows")

    def test_every_docs_writing_workflow_triggers_a_redeploy(self):
        # Workflows that push to the repo but write nothing under docs/ (or are deliberately not redeploying) are exempt, with the reason:
        exempt = {"pages-deploy.yml": "is the deploy", "mobile-sync.yml": "is the mirror", "tests.yml": "read-only", "daily-health-check.yml": "writes data/health_alert_state.json",
                  "pick-of-day-social-daily.yml": "writes data/ markers", "send-expiry-reminders.yml": "writes the private repo", "lock-watchdog.yml": "owner decision 2026-10-03: no deploy per slot",
                  "weekly-health-digest.yml": "no push", "send-demo-emails.yml": "no push", "verify-lock-workflows.yml": "no push", "landing-performance-refresh.yml": "listed by name already"}
        listed = set(self.run_trigger_names("pages-deploy.yml"))
        for f in sorted(WF.glob("*.yml")):
            body = re.sub(r"(?m)^\s*#.*$", "", f.read_text())
            if f.name in exempt or not re.search(r"git push|--push|auto_lock_settle\.py", body):
                continue
            self.assertIn(names()[f.name], listed, f"{f.name} pushes to the repo but is not in pages-deploy.yml's workflow_run list -- its commits would not reach the site")

    def test_nfl_roster_runs_every_other_week_only(self):
        text = (WF / "nfl-weekly-refresh.yml").read_text()
        self.assertIn("WEEK % 2", text)
        self.assertIn("steps.plan.outputs.roster == '1'", text)

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
