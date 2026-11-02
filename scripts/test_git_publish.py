#!/usr/bin/env python3
"""scripts/_git_publish.py against REAL git repositories (a bare 'origin' in a temp dir): the push race, a depth-1 clone (what actions/checkout gives the refresh workflows), a same-file conflict, a remote that
refuses pushes, and nothing to commit.  2026-11-02: Liiga (10-23) and NLA (10-28) failed with `git push failed after 5 retries`, every attempt non-fast-forward, because a plain `git pull --rebase` cannot relate
a depth-1 clone to the newer remote.

    python3 scripts/test_git_publish.py
"""
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _git_publish as G  # noqa: E402

GIT_ID = ["-c", "user.name=t", "-c", "user.email=t@t"]


def git(cwd, *a, check=True):
    return subprocess.run(["git", *GIT_ID, *a], cwd=cwd, capture_output=True, text=True, check=check)


class Repos(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.origin = self.tmp / "origin.git"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(self.origin)], check=True)
        seed = self.tmp / "seed"
        subprocess.run(["git", "clone", "-q", str(self.origin), str(seed)], check=True, capture_output=True)
        git(seed, "checkout", "-q", "-b", "main")
        (seed / "docs").mkdir()
        (seed / "docs" / "liiga_schedule.json").write_text('{"v": 0}\n')
        (seed / "docs" / "other.json").write_text('{"o": 0}\n')
        git(seed, "add", "."); git(seed, "commit", "-q", "-m", "seed"); git(seed, "push", "-q", "origin", "main")
        self.seed = seed
        self.logs = []

    def bot_push(self, name, content, path="docs/other.json"):
        """another bot lands a commit on origin/main"""
        (self.seed / path).write_text(content)
        git(self.seed, "add", "."); git(self.seed, "commit", "-q", "-m", name); git(self.seed, "push", "-q", "origin", "main")

    def clone(self, shallow=False):
        d = self.tmp / ("shallow" if shallow else "full")
        args = ["git", "clone", "-q", "--no-hardlinks"] + (["--depth", "1", f"file://{self.origin}"] if shallow else [str(self.origin)]) + [str(d)]
        subprocess.run(args, check=True, capture_output=True)
        return d

    def origin_file(self, path):
        return subprocess.run(["git", "--git-dir", str(self.origin), "show", f"main:{path}"], capture_output=True, text=True).stdout

    def publish(self, repo, content='{"v": 1}\n', **kw):
        (repo / "docs" / "liiga_schedule.json").write_text(content)
        return G.publish(["docs/liiga_schedule.json"], "chore: refresh", self.logs.append, root=repo, sleep=lambda s: None, park=lambda r: (lambda: None), **kw)

    def test_a_push_race_is_rebased_and_lands(self):
        repo = self.clone()
        self.bot_push("bot", '{"o": 1}\n')                    # origin moved after our checkout
        self.assertTrue(self.publish(repo))
        self.assertEqual(self.origin_file("docs/liiga_schedule.json"), '{"v": 1}\n')
        self.assertEqual(self.origin_file("docs/other.json"), '{"o": 1}\n')       # the other bot's commit is kept

    def test_a_depth_one_clone_far_behind_origin_lands(self):
        repo = self.clone(shallow=True)
        for i in range(3):
            self.bot_push(f"bot{i}", f'{{"o": {i + 1}}}\n')    # several commits landed since the shallow checkout
        old = subprocess.run(["git", "pull", "--rebase", "--autostash", "origin", "main"], cwd=repo, capture_output=True, text=True)
        (repo / "docs" / "liiga_schedule.json").write_text('{"probe": 1}\n')
        git(repo, "add", "."); git(repo, "commit", "-q", "-m", "probe")
        probe = subprocess.run(["git", "pull", "--rebase", "--autostash", "origin", "main"], cwd=repo, capture_output=True, text=True)
        git(repo, "reset", "-q", "--hard", "HEAD~1")
        self.assertTrue(self.publish(repo))                    # the new helper lands it from the same state
        self.assertEqual(self.origin_file("docs/liiga_schedule.json"), '{"v": 1}\n')
        self.assertEqual(self.origin_file("docs/other.json"), '{"o": 3}\n')

    def test_a_same_file_conflict_keeps_this_runs_fresh_scrape(self):
        repo = self.clone()
        self.bot_push("earlier run", '{"v": "old scrape"}\n', path="docs/liiga_schedule.json")
        self.assertTrue(self.publish(repo, '{"v": "fresh scrape"}\n'))
        self.assertEqual(self.origin_file("docs/liiga_schedule.json"), '{"v": "fresh scrape"}\n')

    def test_nothing_to_commit_is_fine(self):
        repo = self.clone()
        self.assertTrue(self.publish(repo, '{"v": 0}\n'))        # same content as origin
        self.assertTrue(any("nothing to commit" in l for l in self.logs))

    def test_a_remote_that_refuses_pushes_returns_false_and_never_raises(self):
        repo = self.clone()
        hook = self.origin / "hooks" / "pre-receive"
        hook.write_text("#!/bin/sh\necho refused >&2\nexit 1\n"); hook.chmod(0o755)
        self.assertFalse(self.publish(repo))
        self.assertTrue(any("WARNING" in l for l in self.logs))
        self.assertEqual(git(repo, "log", "-1", "--format=%s").stdout.strip(), "chore: refresh")      # the commit stays local for the workflow's Publish step
        self.assertEqual(sum("push attempt" in l for l in self.logs), 5)

    def test_every_hockey_refresh_script_uses_the_helper(self):
        for n in ("liiga", "shl", "nla", "extraliga", "nhl_ou"):
            src = (Path(__file__).resolve().parent / f"fetch_{n}.py").read_text()
            self.assertIn("from _git_publish import publish", src, n)
            self.assertNotIn("git push failed after 5 retries", src, n)


if __name__ == "__main__":
    unittest.main()
