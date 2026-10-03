#!/usr/bin/env python3
"""A schedule-file push must succeed when another commit landed on main AND docs/scraper_health.json is dirty (it is rewritten by log_scrape() before the push).
Uses real temporary git repos (a bare remote + two clones)."""
from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _scraper_health as H  # noqa: E402


def git(cwd, *a, check=True):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=cwd, capture_output=True, text=True, check=check)


class PushRace(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(self.tmp.name)
        self.remote = t / "remote.git"
        git(t, "init", "--bare", "-b", "main", str(self.remote))
        self.a, self.b = t / "a", t / "b"
        git(t, "clone", str(self.remote), str(self.a))
        (self.a / "docs").mkdir()
        (self.a / "docs" / "scraper_health.json").write_text("[]")
        (self.a / "docs" / "shl_schedule.json").write_text("{}")
        git(self.a, "add", "-A"); git(self.a, "commit", "-m", "init"); git(self.a, "push", "origin", "main")
        git(t, "clone", str(self.remote), str(self.b))

    def tearDown(self):
        self.tmp.cleanup()

    def _race(self):
        # another workflow pushes a commit to main
        (self.b / "docs" / "other.json").write_text("1")
        git(self.b, "add", "-A"); git(self.b, "commit", "-m", "other"); git(self.b, "push", "origin", "main")
        # this run: schedule file changed + committed, health file rewritten (dirty)
        (self.a / "docs" / "shl_schedule.json").write_text('{"x":1}')
        git(self.a, "add", "docs/shl_schedule.json"); git(self.a, "commit", "-m", "schedule")
        (self.a / "docs" / "scraper_health.json").write_text('[{"league":"SHL"}]')

    def test_plain_pull_rebase_refuses_on_a_dirty_tree(self):
        self._race()
        r = git(self.a, "pull", "--rebase", "origin", "main", check=False)
        self.assertNotEqual(r.returncode, 0)          # the old behaviour: it never rebased, so the push below was rejected
        self.assertNotEqual(git(self.a, "push", "origin", "main", check=False).returncode, 0)

    def test_parked_health_file_lets_the_push_through_and_is_restored(self):
        self._race()
        restore = H.park_health(self.a)
        try:
            self.assertEqual(git(self.a, "pull", "--rebase", "--autostash", "origin", "main", check=False).returncode, 0)
            self.assertEqual(git(self.a, "push", "origin", "main", check=False).returncode, 0)
        finally:
            restore()
        self.assertEqual((self.a / "docs" / "scraper_health.json").read_text(), '[{"league":"SHL"}]')   # this run's record is back, ready for commit_and_push
        self.assertIn("schedule", git(self.remote, "log", "--oneline", "main").stdout)

    def test_untracked_health_file_is_also_handled(self):
        (self.a / "docs" / "scraper_health.json").unlink()
        git(self.a, "rm", "-q", "--cached", "docs/scraper_health.json", check=False)
        (self.a / "docs" / "scraper_health.json").write_text("[1]")
        restore = H.park_health(self.a)
        self.assertFalse((self.a / "docs" / "scraper_health.json").exists())
        restore()
        self.assertEqual((self.a / "docs" / "scraper_health.json").read_text(), "[1]")


if __name__ == "__main__":
    unittest.main(verbosity=2)
