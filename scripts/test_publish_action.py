#!/usr/bin/env python3
"""The shared publish step (.github/actions/publish/publish.sh): commit -> fetch+rebase push loop -> optional R2 mirror -> dispatch pages-deploy.yml when docs/ changed.

Runs the real script against real temporary git repos (a bare "remote" + two clones, so push races and rebase conflicts are genuine) with a fake `gh` that records its arguments and a fake
R2 uploader.  No network, no secrets.

    python3 scripts/test_publish_action.py
"""
from __future__ import annotations

import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / ".github" / "actions" / "publish" / "publish.sh"


def git(cwd, *a, check=True):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=cwd, capture_output=True, text=True, check=check)


class Publish(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = self.t = Path(self.tmp.name)
        self.remote = t / "remote.git"
        git(t, "init", "--bare", "-b", "main", str(self.remote))
        self.a, self.b = t / "a", t / "b"                      # a = the workflow runner, b = a concurrent writer
        git(t, "clone", str(self.remote), str(self.a))
        for rel, body in (("docs/live_data.json", '{"ts": "0"}'), ("docs/picks_backup.json", "[1]"), ("data/marker.txt", "0")):
            (self.a / rel).parent.mkdir(exist_ok=True)
            (self.a / rel).write_text(body)
        git(self.a, "add", "-A"); git(self.a, "commit", "-m", "init"); git(self.a, "push", "origin", "main")
        git(t, "clone", str(self.remote), str(self.b))
        self.start = git(self.a, "rev-parse", "HEAD").stdout.strip()
        self.gh_log = t / "gh.log"
        self.gh = t / "fake-gh"
        self.gh.write_text(f'#!/bin/bash\necho "$@" >> "{self.gh_log}"\n[ -n "${{FAKE_GH_FAIL:-}}" ] && exit 1\nexit 0\n')
        self.gh.chmod(self.gh.stat().st_mode | stat.S_IEXEC)
        self.r2_log = t / "r2.log"
        self.r2 = t / "fake_r2.py"
        self.r2.write_text("import sys, pathlib\nargs = sys.argv[1:]\nroot = pathlib.Path(args[args.index('--root') + 1])\n"
                           "files = [a for a in args[args.index('--root') + 2:]]\n"
                           f"open({str(self.r2_log)!r}, 'a').write('|'.join(f + '=' + (root / f).read_text() for f in files) + '\\n')\n")
        self.runner_temp = t / "rt"
        self.runner_temp.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def run_publish(self, cwd=None, **env):
        out = self.t / "gh_output"
        out.write_text("")
        e = {**os.environ, "GITHUB_OUTPUT": str(out), "RUNNER_TEMP": str(self.runner_temp), "PUB_SLEEP_BASE": "0", "PUB_GH": str(self.gh),
             "PUB_START_SHA": self.start, "PUB_R2_SCRIPT": str(self.r2)}
        for k in ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET"):
            e.pop(k, None)
        e.update({k: v for k, v in env.items() if v is not None})
        r = subprocess.run(["bash", str(SCRIPT)], cwd=cwd or self.a, env=e, capture_output=True, text=True, timeout=60)
        outputs = dict(l.split("=", 1) for l in out.read_text().splitlines() if "=" in l)
        return r, outputs

    def gh_calls(self):
        return self.gh_log.read_text().strip().splitlines() if self.gh_log.exists() else []

    def remote_file(self, rel):
        return git(self.t, "--git-dir", str(self.remote), "show", f"main:{rel}").stdout

    # ── commit + push + dispatch ────────────────────────────────────────────────────────────────────────────────────
    def test_docs_change_is_committed_pushed_and_dispatched(self):
        (self.a / "docs/live_data.json").write_text('{"ts": "1"}')
        r, o = self.run_publish(PUB_PATHS="docs/live_data.json", PUB_MESSAGE="live: {MT_HHMM} MT scores")
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertEqual((o["committed"], o["pushed"], o["docs_changed"], o["dispatched"]), ("1", "1", "1", "1"))
        self.assertEqual(self.remote_file("docs/live_data.json"), '{"ts": "1"}')
        self.assertEqual(self.gh_calls(), ["workflow run pages-deploy.yml --ref main"])
        msg = git(self.t, "--git-dir", str(self.remote), "log", "-1", "--format=%s%n%an").stdout.splitlines()
        self.assertRegex(msg[0], r"^live: \d\d:\d\d MT scores$")            # {MT_HHMM} token expanded
        self.assertEqual(msg[1], "clairvoyance-bot")

    def test_data_only_change_is_pushed_but_does_not_deploy(self):
        (self.a / "data/marker.txt").write_text("1")
        r, o = self.run_publish(PUB_PATHS="data/")
        self.assertEqual((o["committed"], o["pushed"], o["docs_changed"], o["dispatched"]), ("1", "1", "0", "0"))
        self.assertEqual(self.gh_calls(), [])

    def test_nothing_changed_does_nothing(self):
        r, o = self.run_publish(PUB_PATHS="docs/live_data.json docs/does_not_exist.json")      # a missing path must not stop anything
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual((o["committed"], o["pushed"], o["docs_changed"], o["dispatched"]), ("0", "1", "0", "0"))
        self.assertEqual(self.gh_calls(), [])

    def test_python_helper_that_pushed_itself_still_triggers_the_deploy(self):
        """The --push scripts commit + push on their own: the step is then called with NO paths and must notice docs/ changed since the run started."""
        (self.a / "docs/liiga_schedule.json").write_text("{}")
        git(self.a, "add", "-A"); git(self.a, "commit", "-m", "script commit"); git(self.a, "push", "origin", "main")
        r, o = self.run_publish()
        self.assertEqual((o["committed"], o["pushed"], o["docs_changed"], o["dispatched"]), ("0", "1", "1", "1"))
        self.assertEqual(len(self.gh_calls()), 1)

    def test_a_second_publish_step_for_the_same_docs_state_does_not_dispatch_again(self):
        (self.a / "docs/live_data.json").write_text('{"ts": "2"}')
        self.run_publish(PUB_PATHS="docs/live_data.json")
        r, o = self.run_publish()                                   # the catch-all at the end of a job
        self.assertEqual(o["dispatched"], "0")
        self.assertEqual(len(self.gh_calls()), 1)
        (self.a / "docs/liiga_schedule.json").write_text("{}")          # ... but new docs/ content afterwards does
        git(self.a, "add", "-A"); git(self.a, "commit", "-m", "later script commit"); git(self.a, "push", "origin", "main")
        r, o = self.run_publish()
        self.assertEqual(o["dispatched"], "1")
        self.assertEqual(len(self.gh_calls()), 2)

    def test_dispatch_modes(self):
        r, o = self.run_publish(PUB_DISPATCH="always")
        self.assertEqual(o["dispatched"], "1")
        (self.a / "docs/live_data.json").write_text('{"ts": "3"}')
        r, o = self.run_publish(PUB_PATHS="docs/live_data.json", PUB_DISPATCH="never")
        self.assertEqual((o["pushed"], o["dispatched"]), ("1", "0"))
        self.assertEqual(len(self.gh_calls()), 1)

    def test_a_failing_dispatch_never_fails_the_job(self):
        (self.a / "docs/live_data.json").write_text('{"ts": "4"}')
        r, o = self.run_publish(PUB_PATHS="docs/live_data.json", FAKE_GH_FAIL="1")
        self.assertEqual(r.returncode, 0)
        self.assertEqual((o["pushed"], o["dispatched"]), ("1", "0"))
        self.assertIn("::warning::publish: could not dispatch", r.stdout)
        self.assertEqual(len(self.gh_calls()), 3)                         # retried

    # ── push races ──────────────────────────────────────────────────────────────────────────────────────────────────
    def test_push_race_with_a_different_file_rebases_and_pushes(self):
        (self.b / "docs/other.json").write_text("{}")
        git(self.b, "add", "-A"); git(self.b, "commit", "-m", "other writer"); git(self.b, "push", "origin", "main")
        (self.a / "docs/live_data.json").write_text('{"ts": "5"}')
        r, o = self.run_publish(PUB_PATHS="docs/live_data.json")
        self.assertEqual((o["committed"], o["pushed"], o["dispatched"]), ("1", "1", "1"))
        self.assertEqual(self.remote_file("docs/live_data.json"), '{"ts": "5"}')
        self.assertEqual(self.remote_file("docs/other.json"), "{}")             # the other writer's commit survived

    def test_dirty_tree_does_not_block_the_rebase(self):
        """Python byproducts (card.png, scraper_health.json ...) left modified must not make `rebase` refuse to run (it silently skipped marker writes before)."""
        (self.b / "docs/other.json").write_text("{}")
        git(self.b, "add", "-A"); git(self.b, "commit", "-m", "other writer"); git(self.b, "push", "origin", "main")
        (self.a / "docs/picks_backup.json").write_text("[1, 2]")              # tracked, modified, NOT in PUB_PATHS
        (self.a / "docs/live_data.json").write_text('{"ts": "6"}')
        r, o = self.run_publish(PUB_PATHS="docs/live_data.json")
        self.assertEqual(o["pushed"], "1", r.stdout + r.stderr)
        self.assertEqual((self.a / "docs/picks_backup.json").read_text(), "[1, 2]")      # the byproduct is back, untouched

    def test_conflict_is_skipped_cleanly_by_default(self):
        (self.b / "docs/live_data.json").write_text('{"ts": "theirs"}')
        git(self.b, "add", "-A"); git(self.b, "commit", "-m", "theirs"); git(self.b, "push", "origin", "main")
        (self.a / "docs/live_data.json").write_text('{"ts": "mine"}')
        r, o = self.run_publish(PUB_PATHS="docs/live_data.json")
        self.assertEqual(r.returncode, 0)
        self.assertEqual((o["committed"], o["pushed"], o["dispatched"]), ("1", "0", "0"))
        self.assertEqual(self.remote_file("docs/live_data.json"), '{"ts": "theirs"}')
        self.assertEqual(self.gh_calls(), [])                                   # nothing pushed -> nothing to deploy
        self.assertFalse((self.a / ".git" / "rebase-merge").exists())            # no half-finished rebase left behind
        self.assertFalse((self.a / ".git" / "rebase-apply").exists())
        self.assertIn("::warning::publish: rebase onto origin/main conflicted", r.stdout)

    def test_prefer_mine_wins_the_conflict(self):
        (self.b / "docs/live_data.json").write_text('{"ts": "theirs"}')
        git(self.b, "add", "-A"); git(self.b, "commit", "-m", "theirs"); git(self.b, "push", "origin", "main")
        (self.a / "docs/live_data.json").write_text('{"ts": "mine"}')
        r, o = self.run_publish(PUB_PATHS="docs/live_data.json", PUB_ON_CONFLICT="prefer-mine")
        self.assertEqual((o["pushed"], o["dispatched"]), ("1", "1"))
        self.assertEqual(self.remote_file("docs/live_data.json"), '{"ts": "mine"}')

    def test_depth_one_checkout_with_a_depth_one_fetch_in_the_job(self):
        """What the lock workflows really look like: actions/checkout is a depth-1 clone AND an early step ran `git fetch --depth=1 origin main`, which leaves origin/main and HEAD as two
        unrelated shallow roots.  The step must still push (deepening until they join) instead of replaying the root commit."""
        s = self.t / "shallow"
        git(self.t, "clone", "-q", "--depth=1", f"file://{self.remote}", str(s))
        start = git(s, "rev-parse", "HEAD").stdout.strip()
        for i in range(3):                                                        # other workflows push while this job runs
            (self.b / f"docs/other{i}.json").write_text("{}")
            git(self.b, "add", "-A"); git(self.b, "commit", "-m", f"other {i}"); git(self.b, "push", "origin", "main")
        git(s, "fetch", "-q", "--depth=1", "origin", "main")                      # the marker-read step
        (s / "docs/live_data.json").write_text('{"ts": "shallow"}')
        r, o = self.run_publish(cwd=s, PUB_PATHS="docs/live_data.json", PUB_START_SHA=start)
        self.assertEqual((o["committed"], o["pushed"], o["dispatched"]), ("1", "1", "1"), r.stdout + r.stderr)
        self.assertEqual(self.remote_file("docs/live_data.json"), '{"ts": "shallow"}')
        self.assertEqual(self.remote_file("docs/other2.json"), "{}")
        self.assertEqual(git(self.t, "--git-dir", str(self.remote), "rev-list", "--count", "main").stdout.strip(), "5")      # init + 3 others + ours: nothing replayed twice

    def test_unchanged_depth_one_checkout_never_tries_to_push(self):
        s = self.t / "shallow"
        git(self.t, "clone", "-q", "--depth=1", f"file://{self.remote}", str(s))
        start = git(s, "rev-parse", "HEAD").stdout.strip()
        (self.b / "docs/other.json").write_text("{}")
        git(self.b, "add", "-A"); git(self.b, "commit", "-m", "other"); git(self.b, "push", "origin", "main")
        git(s, "fetch", "-q", "--depth=1", "origin", "main")
        r, o = self.run_publish(cwd=s, PUB_START_SHA=start)
        self.assertEqual((o["pushed"], o["docs_changed"], o["dispatched"]), ("1", "0", "0"))
        self.assertEqual(self.gh_calls(), [])

    def test_leftover_local_commit_from_an_earlier_step_gets_pushed(self):
        (self.a / "docs/liiga_schedule.json").write_text("{}")
        git(self.a, "add", "-A"); git(self.a, "commit", "-m", "script committed but its push was rejected")      # never pushed
        self.assertEqual(git(self.a, "rev-list", "--count", "origin/main..HEAD").stdout.strip(), "1")
        r, o = self.run_publish()
        self.assertEqual((o["pushed"], o["dispatched"]), ("1", "1"))
        self.assertEqual(self.remote_file("docs/liiga_schedule.json"), "{}")

    # ── R2 mirror ───────────────────────────────────────────────────────────────────────────────────────────────────
    def test_r2_mirrors_the_origin_copy_not_a_stale_working_tree(self):
        """Phase 1: R2 must equal what is on origin/main.  A runner whose checkout is hours old must not overwrite newer data."""
        (self.b / "docs/picks_backup.json").write_text("[1, 2, 3]")             # newer ledger on origin
        git(self.b, "add", "-A"); git(self.b, "commit", "-m", "newer ledger"); git(self.b, "push", "origin", "main")
        r, o = self.run_publish(PUB_R2_FILES="docs/picks_backup.json")           # this runner still has "[1]" in its working tree
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertEqual(self.r2_log.read_text().strip(), "docs/picks_backup.json=[1, 2, 3]")

    def test_r2_workdir_source_uploads_this_runners_file(self):
        (self.a / "docs/picks_backup.json").write_text("[9]")
        self.run_publish(PUB_R2_FILES="docs/picks_backup.json", PUB_R2_SOURCE="workdir")
        self.assertEqual(self.r2_log.read_text().strip(), "docs/picks_backup.json=[9]")

    def test_a_failing_r2_upload_never_fails_the_job(self):
        self.r2.write_text("import sys\nsys.exit(1)\n")
        (self.a / "docs/live_data.json").write_text('{"ts": "7"}')
        r, o = self.run_publish(PUB_PATHS="docs/live_data.json", PUB_R2_FILES="docs/live_data.json")
        self.assertEqual(r.returncode, 0)
        self.assertEqual((o["pushed"], o["dispatched"]), ("1", "1"))              # the deploy still happens
        self.assertIn("R2 mirror failed", r.stdout)

    def test_real_r2_script_without_credentials_is_a_logged_noop(self):
        (self.a / "docs/live_data.json").write_text('{"ts": "8"}')
        r, o = self.run_publish(PUB_PATHS="docs/live_data.json", PUB_R2_FILES="docs/live_data.json", PUB_R2_SCRIPT=str(ROOT / "scripts" / "r2_publish.py"))
        self.assertEqual(r.returncode, 0)
        self.assertIn("R2 not configured", r.stdout)
        self.assertEqual(o["dispatched"], "1")


if __name__ == "__main__":
    unittest.main(verbosity=2)
