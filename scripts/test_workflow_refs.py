#!/usr/bin/env python3
"""Static checks that workflows and their references agree (no network, no YAML library): a workflow that was merged or renamed must not leave a dangling
reference behind -- a `gh workflow run "<name>"` that matches nothing fails silently in the live-tracker watchdog.

PUBLISH RULE (2026-10-10, replaces the old `workflow_run` allowlist audit): GITHUB_TOKEN pushes never fire `push` workflows, so every workflow that writes to the repo must END with the
shared publish step (`uses: ./.github/actions/publish`), which dispatches pages-deploy.yml (workflow_dispatch IS allowed with GITHUB_TOKEN, and needs `actions: write`).  The test below
flags any workflow that pushes without it -- the default for a NEW workflow is "fail", and the only way out is an entry in PUSH_WITHOUT_DEPLOY with a reason."""
import re, sys, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WF = ROOT / ".github" / "workflows"
ACTION = ROOT / ".github" / "actions" / "publish"
PUBLISH_USES = "uses: ./.github/actions/publish"

# Workflows that push (or run a script that pushes) but are deliberately NOT publish-step callers, with the reason.
PUSH_WITHOUT_DEPLOY = {
    "pages-deploy.yml": "is the deploy",
    "mobile-sync.yml": "pushes to the separate mobile repo, not this one",
    "tests.yml": "read-only",
    "pick-of-day-social-daily.yml": "writes only data/ markers",
    "send-expiry-reminders.yml": "writes the private subscriber repo",
    "lock-watchdog.yml": "owner decision 2026-10-03: no deploy per watchdog slot (its --auto-lock pass hands over to the normal lock workflows)",
    "weekly-health-digest.yml": "no push",
    "send-demo-emails.yml": "no push",
    "verify-lock-workflows.yml": "no push",
}
PUSH_PATTERN = r"git push|--push|auto_lock_settle\.py|lock_prep\.py(?![^\n]*--no-push)"


def names():
    out = {}
    for f in WF.glob("*.yml"):
        m = re.search(r"(?m)^name:\s*(.+?)\s*$", f.read_text())
        out[f.name] = m.group(1).strip("'\"") if m else None
    return out


def strip_comments(text):
    return re.sub(r"(?m)^\s*#.*$", "", text)


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

    # ── the deploy trigger ──────────────────────────────────────────────────────────────────────────────────────────
    def test_pages_deploy_triggers_are_small_and_have_no_workflow_run_allowlist(self):
        text = strip_comments((WF / "pages-deploy.yml").read_text())
        self.assertNotIn("workflow_run", text, "the workflow_run name allowlist is gone: bot commits deploy via the publish step's workflow_dispatch")
        on = text[text.index("\non:"):text.index("\npermissions:")]
        keys = re.findall(r"(?m)^  ([a-z_]+):", on)
        self.assertEqual(sorted(keys), ["push", "schedule", "workflow_dispatch"])
        self.assertIn("- 'docs/**'", on)
        self.assertRegex(on, r"cron: '23 \*/4 \* \* \*'")                     # the scheduled fallback is kept
        self.assertIn('group: "pages"', text)                                  # concurrency semantics untouched
        self.assertIn("cancel-in-progress: false", text)
        self.assertIn("kickoffs.json", text)                                   # self-heal + kickoff build untouched
        self.assertIn("scripts/build_kickoffs.py", text)

    def test_a_manual_lock_relay_commit_never_redeploys_the_site(self):
        """The app commits docs/manual_locks.json / relay_ping.json from the owner's phone on every lock; each commit used to start a 2.8 MB Pages redeploy whose cache purge made the site
        503 / time out mid-use (owner report 2026-10-20). Both files are excluded from the push trigger AND from the 'docs/ changed' check; the site reads manual_locks.json only in the Supabase-down fallback."""
        text = (WF / "pages-deploy.yml").read_text()
        for f in ("manual_locks.json", "relay_ping.json"):
            self.assertIn(f"- '!docs/{f}'", text, f)
            self.assertIn(f"':!docs/{f}'", text, f)
        app = (ROOT / "docs" / "app.html").read_text()
        # the only read from the Pages origin is the Supabase-down fallback (manual locks made on another device); it rides along with the next deploy for any OTHER change, so it is at most a few
        # deploys stale. Pin that it stays inside that fallback rather than becoming a normal-path dependency:
        self.assertEqual(app.count("fetch('manual_locks.json"), 1)
        self.assertIn("Supabase unreachable", app[app.index("fetch('manual_locks.json") - 900:app.index("fetch('manual_locks.json")])

    def test_big_data_files_use_shared_cache_buckets_not_millisecond_busters(self):
        """A ?t=Date.now() query is a unique URL every time, so GitHub Pages serves it cold from origin (measured 2026-10-20: cold app.html 34 s, cold data.json 4 s, cached 0.4 s) and a phone launch
        fired a dozen of them. The multi-hundred-KB files use _cb(minutes), one value per time window shared by every device, and the deploy warms those URLs."""
        app = (ROOT / "docs" / "app.html").read_text()
        for f in ("data.json", "picks_backup.json", "soccer_fbref.json", "ledger_archive.json", "nba_schedule.json"):
            self.assertNotRegex(app, r"fetch\(['\"`]" + re.escape(f) + r"\?[^)]*Date\.now\(\)", f)
            self.assertRegex(app, r"fetch\(['\"`]" + re.escape(f) + r"\?[^)]*_cb\(\d+\)", f)
        self.assertIn("window._cb=function(minutes)", app)
        wf = (WF / "pages-deploy.yml").read_text()
        self.assertIn("Warm the CDN", wf); self.assertIn("steps.deployment.outcome == 'success'", wf)
        for f in ("data.json?$((now/180))", "picks_backup.json?t=$((now/300))"):
            self.assertIn(f, wf)                                                  # same bucket maths as _cb(3) / _cb(5)

    def test_mobile_sync_workflow_run_names_are_real_workflows(self):
        """mobile-sync.yml still mirrors on `workflow_run` (not part of the publish step): every name it lists must be a real workflow."""
        text = (WF / "mobile-sync.yml").read_text()
        block = text[text.index("workflow_run:"):]
        block = block[: re.search(r"(?m)^  schedule:", block).start()]
        listed = re.findall(r'(?m)^\s*-\s*"([^"]+)"\s*$', block)
        self.assertGreater(len(listed), 10)
        known = set(n for n in names().values() if n)
        for n in listed:
            self.assertIn(n, known, f"mobile-sync.yml's workflow_run lists a workflow that does not exist: {n}")

    # ── the publish step ────────────────────────────────────────────────────────────────────────────────────────────
    def test_publish_action_commits_pushes_and_dispatches(self):
        self.assertTrue((ACTION / "action.yml").exists())
        self.assertTrue((ACTION / "publish.sh").exists())
        action = (ACTION / "action.yml").read_text()
        sh = (ACTION / "publish.sh").read_text()
        self.assertIn("using: composite", action)
        self.assertIn("publish.sh", action)
        self.assertIn("PUB_WORKFLOW: pages-deploy.yml", action)
        self.assertIn('workflow run "$WORKFLOW"', sh)
        self.assertIn("rebase", sh)
        self.assertIn("git push", sh)
        wf_name = re.search(r"PUB_WORKFLOW: (\S+)", action).group(1)
        self.assertTrue((WF / wf_name).exists())
        self.assertRegex((WF / wf_name).read_text(), r"(?m)^  workflow_dispatch:")      # the dispatch target must accept workflow_dispatch

    @staticmethod
    def publish_problems(name, text):
        """-> list of problems for a workflow `name` with source `text` (empty = fine / exempt)."""
        body = strip_comments(text)
        if name in PUSH_WITHOUT_DEPLOY or not re.search(PUSH_PATTERN, body):
            return []
        if PUBLISH_USES not in body:
            return [f"{name} pushes to the repo but has no `{PUBLISH_USES}` step -- its docs/ changes would not reach the site until the 4-hourly fallback"]
        out = []
        if body.rindex(PUBLISH_USES) < max(m.start() for m in re.finditer(PUSH_PATTERN, body)):
            out.append(f"{name}: the publish step must come after the last step that pushes")
        perms = re.search(r"(?m)^permissions:\n((?:[ \t]+.*\n)+)", body)
        if not perms:
            return out + [f"{name}: no top-level permissions block"]
        if not re.search(r"(?m)^\s+actions:\s*write\b", perms.group(1)):
            out.append(f"{name}: the publish step needs `actions: write` to dispatch pages-deploy.yml")
        if not re.search(r"(?m)^\s+contents:\s*write\b", perms.group(1)):
            out.append(f"{name}: the publish step needs `contents: write` to push")
        return out

    def test_every_pushing_workflow_ends_with_the_publish_step_and_can_dispatch(self):
        """A workflow that pushes to the repo (git push, a --push helper, auto_lock_settle.py / lock_prep.py which push themselves) must call the publish step AFTER its last push, and its
        `permissions` must include `actions: write` (workflow_dispatch needs it).  Add a reason to PUSH_WITHOUT_DEPLOY to opt out -- that is the only way to forget it."""
        for f in sorted(WF.glob("*.yml")):
            self.assertEqual(self.publish_problems(f.name, f.read_text()), [], f.name)

    def test_the_check_itself_catches_a_new_workflow_that_forgets_the_publish_step(self):
        """Mutation checks on synthetic workflows: the guard that replaced the allowlist audit must actually fail for the mistakes it exists to catch."""
        step = "      - uses: ./.github/actions/publish\n"
        good = ("name: New data\non:\n  workflow_dispatch: {}\npermissions:\n  contents: write\n  actions: write\njobs:\n  j:\n    steps:\n"
                "      - run: python3 scripts/fetch_x.py --push\n" + step)
        self.assertEqual(self.publish_problems("new-data.yml", good), [])
        # 1. the new docs-writing workflow forgot the publish step entirely
        self.assertTrue(self.publish_problems("new-data.yml", good.replace(step, "")))
        # 2. ... only mentions it in a comment
        self.assertTrue(self.publish_problems("new-data.yml", good.replace(step, "      # uses: ./.github/actions/publish\n")))
        # 3. publish step placed BEFORE the push
        swapped = good.replace("      - run: python3 scripts/fetch_x.py --push\n" + step, step + "      - run: python3 scripts/fetch_x.py --push\n")
        self.assertTrue(self.publish_problems("new-data.yml", swapped))
        # 4. missing `actions: write`
        self.assertTrue(self.publish_problems("new-data.yml", good.replace("  actions: write\n", "")))
        # 5. a plain push step (no helper script) is caught too
        self.assertTrue(self.publish_problems("new-data.yml", good.replace("python3 scripts/fetch_x.py --push", "git push origin main").replace(step, "")))
        # 6. a workflow that does not push needs nothing
        self.assertEqual(self.publish_problems("reader.yml", "name: r\npermissions:\n  contents: read\njobs: {}\n"), [])

    def test_exempt_workflows_are_real_and_still_need_their_exemption(self):
        for name in PUSH_WITHOUT_DEPLOY:
            self.assertTrue((WF / name).exists(), f"{name} is exempt but no longer exists -- drop it from PUSH_WITHOUT_DEPLOY")
        for name in ("pick-of-day-social-daily.yml",):             # daily-health-check.yml now writes docs/calibration_watch.json and uses the publish step instead
            body = strip_comments((WF / name).read_text())
            self.assertNotIn("docs/", re.sub(r"https?://\S+", "", body.replace("docs/app.html", "")), f"{name} now touches docs/ -- it needs the publish step instead of an exemption")

    def test_the_publish_step_never_hides_a_failure_it_can_see(self):
        """Every caller uses it as a plain step (no continue-on-error on the `uses:` step: the script itself never exits non-zero for an operational problem, so a red step means the action is broken)."""
        for f in sorted(WF.glob("*.yml")):
            text = f.read_text()
            for m in re.finditer(re.escape(PUBLISH_USES), text):
                head = text[max(0, text.rfind("      - name:", 0, m.start())):m.start()]
                self.assertNotIn("continue-on-error", head, f.name)

    # ── the rest ────────────────────────────────────────────────────────────────────────────────────────────────────
    def test_no_step_has_an_empty_with_or_env_block(self):
        """GitHub rejects a workflow whose step has `with:` / `env:` with nothing under it ("workflow file issue": the run fails on every push and scheduled runs never start).
        PyYAML parses it as null (and is not installed on the CI runner), so this is a plain text check: a `with:` / `env:` key line that is not followed by a deeper-indented line.
        It is exactly what an R2-removal edit left behind on 2026-10-10 in 7 workflows."""
        files = sorted((ROOT / ".github" / "workflows").glob("*.yml")) + [ROOT / ".github" / "actions" / "publish" / "action.yml"]
        for f in files:
            lines = f.read_text().split("\n")
            for i, line in enumerate(lines):
                m = re.match(r"^(\s*)(with|env):\s*(#.*)?$", line)
                if not m:
                    continue
                ind = len(m.group(1))
                nxt = next((x for x in lines[i + 1:] if x.strip() and not x.strip().startswith("#")), "")
                nind = len(nxt) - len(nxt.lstrip()) if nxt else -1
                self.assertGreater(nind, ind, f"{f.name}:{i + 1}: empty `{m.group(2)}:` block")

    def test_the_empty_block_check_itself_catches_the_mistake(self):
        bad = "steps:\n  - name: x\n    uses: ./.github/actions/publish\n    with:\n  - name: y\n    run: echo hi\n"
        lines = bad.split("\n")
        i = next(n for n, l in enumerate(lines) if re.match(r"^\s*with:\s*$", l))
        nxt = next(x for x in lines[i + 1:] if x.strip())
        self.assertLessEqual(len(nxt) - len(nxt.lstrip()), len(lines[i]) - len(lines[i].lstrip()))

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
