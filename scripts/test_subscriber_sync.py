#!/usr/bin/env python3
"""The subscriber tools download GitHub's list BEFORE changing it (2026-11-25: a renewal made on a stale copy clashed with the expiry run's ended_sent flag
and never reached GitHub).  Real temporary git repos stand in for the private repo: a bare "github", the Mac's clone, and the workflow's clone.

    python3 scripts/test_subscriber_sync.py
"""
from __future__ import annotations

import http.server
import json
import os
import socketserver
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import _subscribers as S  # noqa: E402
import subscribers_admin as A  # noqa: E402

EMAIL = "fan@example.com"
OLD = (datetime.now(timezone.utc) - timedelta(days=30, hours=2)).isoformat()
ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
       "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout


class Repos(unittest.TestCase):
    def setUp(self):
        env = mock.patch.dict(os.environ, ENV)
        env.start()
        self.addCleanup(env.stop)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.origin, self.mac, self.ci = root / "origin.git", root / "mac", root / "ci"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(self.origin)], check=True)
        subprocess.run(["git", "clone", "-q", str(self.origin), str(self.mac)], check=True, capture_output=True)
        data = {p: [] for p in S.PRODUCTS}
        data["nfl"] = [{"email": EMAIL, "added": OLD, "reminder_sent": True}]
        (self.mac / "subscribers.json").write_text(json.dumps(data, indent=2) + "\n")
        (self.mac / "subscriber_events.json").write_text("[]\n")
        git(self.mac, "checkout", "-q", "-b", "main")
        git(self.mac, "add", "-A")
        git(self.mac, "commit", "-q", "-m", "seed")
        git(self.mac, "push", "-q", "-u", "origin", "main")
        subprocess.run(["git", "clone", "-q", str(self.origin), str(self.ci)], check=True, capture_output=True)
        for name, value in (("DATA_SOURCE", "private-local"), ("REPO_ROOT", self.mac), ("SUBSCRIBERS_FILE", self.mac / "subscribers.json"),
                            ("EVENTS_FILE", self.mac / "subscriber_events.json"), ("_SYNC_RELS", ["subscribers.json", "subscriber_events.json"])):
            p = mock.patch.object(S, name, value)
            p.start()
            self.addCleanup(p.stop)

    def workflow_edits(self, change):
        """the GitHub workflow changes the list and pushes it"""
        git(self.ci, "pull", "-q", "origin", "main")
        f = self.ci / "subscribers.json"
        data = json.loads(f.read_text())
        change(data)
        f.write_text(json.dumps(data, indent=2) + "\n")
        git(self.ci, "commit", "-q", "-am", "expiry-reminders: mark reminder_sent / ended_sent")
        git(self.ci, "push", "-q", "origin", "main")

    def mark_ended(self):
        self.workflow_edits(lambda d: d["nfl"][0].update(ended_sent=True))

    def on_github(self) -> dict:
        return json.loads(git(self.origin, "show", "main:subscribers.json"))


class RefreshFirst(Repos):
    def test_without_the_download_a_renewal_after_the_expiry_run_clashes(self):
        self.mark_ended()
        S.add_subscriber("nfl", EMAIL)                              # the old behaviour: change the stale copy
        ok, msg = S.sync_subscribers_to_git("Renew")
        self.assertFalse(ok)
        self.assertIn("pull --rebase failed", msg)
        self.assertEqual(self.on_github()["nfl"][0]["added"], OLD)

    def test_todays_case_now_syncs_and_clears_the_ended_flag(self):
        self.mark_ended()
        self.assertEqual(S.refresh_subscribers_from_git(), (True, "downloaded the latest list"))
        S.add_subscriber("nfl", EMAIL)
        self.assertEqual(S.sync_subscribers_to_git("Renew"), (True, "synced to GitHub"))
        row = self.on_github()["nfl"][0]
        self.assertNotEqual(row["added"], OLD)
        self.assertFalse(row["reminder_sent"])
        self.assertNotIn("ended_sent", row)

    def test_already_current_does_nothing(self):
        self.assertEqual(S.refresh_subscribers_from_git(), (True, "already current"))

    def test_a_half_finished_batch_is_synced_before_the_next_change(self):
        S.add_subscriber("nba", "second@example.com")               # saved, not yet synced
        self.workflow_edits(lambda d: d["soccer"].append({"email": "ci@example.com", "added": OLD}))
        ok, msg = S.refresh_subscribers_from_git()
        self.assertTrue(ok, msg)
        data = self.on_github()
        self.assertEqual([r["email"] for r in data["nba"]], ["second@example.com"])
        self.assertEqual([r["email"] for r in data["soccer"]], ["ci@example.com"])

    def test_offline_says_no(self):
        git(self.mac, "remote", "set-url", "origin", str(self.mac / "nowhere.git"))
        ok, msg = S.refresh_subscribers_from_git()
        self.assertFalse(ok)
        self.assertIn("could not download", msg)

    def test_an_old_stuck_change_is_reported_not_piled_onto(self):
        self.mark_ended()
        S.add_subscriber("nfl", EMAIL)
        S.sync_subscribers_to_git("Renew")                          # leaves the local commit stuck
        ok, msg = S.refresh_subscribers_from_git()
        self.assertFalse(ok)
        self.assertIn("by hand", msg)
        self.assertFalse((self.mac / ".git" / "rebase-merge").exists())
        json.loads((self.mac / "subscribers.json").read_text())     # no conflict markers left behind

    def test_a_hung_git_call_is_a_failure_not_a_crash(self):
        with mock.patch.object(S.subprocess, "run", side_effect=subprocess.TimeoutExpired("git", 30)):
            ok, msg = S.refresh_subscribers_from_git()
        self.assertFalse(ok)
        self.assertIn("timed out", msg)

    def test_other_files_and_the_legacy_layout_are_never_pulled(self):
        with mock.patch.object(S, "_git", side_effect=AssertionError("git must not run")):
            with mock.patch.object(S, "SUBSCRIBERS_FILE", Path("/nonexistent/elsewhere/subscribers.json")):
                self.assertTrue(S.refresh_subscribers_from_git()[0])
            with mock.patch.object(S, "DATA_SOURCE", "legacy"):
                self.assertTrue(S.refresh_subscribers_from_git()[0])


class AdminServer(Repos):
    def setUp(self):
        super().setUp()
        socketserver.TCPServer.allow_reuse_address = True
        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), A.Handler)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.shutdown)
        p = mock.patch.object(A, "_last_view_refresh", 0.0)
        p.start()
        self.addCleanup(p.stop)

    def call(self, path, body=None):
        req = urllib.request.Request(f"http://127.0.0.1:{self.srv.server_address[1]}{path}", data=None if body is None else json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_renew_through_the_server_lands_on_top_of_the_expiry_run(self):
        self.mark_ended()
        self.assertEqual(self.call("/api/add", {"product": "nfl", "email": EMAIL})[0], 200)
        status, j = self.call("/api/sync", {"message": "Renew"})
        self.assertEqual((status, j["ok"]), (200, True), j)
        self.assertNotIn("ended_sent", self.on_github()["nfl"][0])

    def test_when_github_is_unreachable_nothing_is_changed(self):
        git(self.mac, "remote", "set-url", "origin", str(self.mac / "nowhere.git"))
        before = (self.mac / "subscribers.json").read_text()
        for path, body in (("/api/add", {"product": "nfl", "email": EMAIL}), ("/api/remove", {"product": "nfl", "email": EMAIL}),
                           ("/api/referral/grant", {"referrer": EMAIL, "referred": "x@example.com", "reward": "nba"})):
            status, j = self.call(path, body)
            self.assertEqual(status, 409, (path, j))
            self.assertIn("nothing was changed", j["error"])
        self.assertEqual((self.mac / "subscribers.json").read_text(), before)

    def test_the_page_shows_githubs_list(self):
        self.workflow_edits(lambda d: d["soccer"].append({"email": "new@example.com", "added": datetime.now(timezone.utc).isoformat()}))
        status, j = self.call("/api/subscribers")
        self.assertEqual(status, 200)
        self.assertEqual([r["email"] for r in j["data"]["soccer"]], ["new@example.com"])

    def test_the_servers_own_page_stops_on_a_refusal(self):
        self.assertIn("res.status === 409", A.PAGE)
        self.assertIn("not removed", A.PAGE)


class CommandLine(Repos):
    def test_a_change_is_refused_when_the_download_fails(self):
        import manage_subscribers as M
        git(self.mac, "remote", "set-url", "origin", str(self.mac / "nowhere.git"))
        with mock.patch.object(sys, "argv", ["m", "add", "nfl", EMAIL]), mock.patch.object(M, "add_subscriber", side_effect=AssertionError("must not change")), \
                mock.patch("builtins.print") as out, self.assertRaises(SystemExit) as stop:
            M.main()
        self.assertEqual(stop.exception.code, 1)
        self.assertIn("Nothing was changed", out.call_args_list[0].args[0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
