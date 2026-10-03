#!/usr/bin/env python3
"""Offline tests for the data-freshness monitoring added 2026-10-03 (no network; GitHub API + e-mail are faked, git runs against
throw-away local repos):

  * scripts/daily_health_check.py  -- parse_stamp / check_data_freshness / check_refresh_workflow / main() alert policy
  * scripts/weekly_health_digest.py -- the refresh workflows are in its list
  * scripts/_scraper_health.py     -- atomic log_scrape, merge_health, commit_and_push (incl. a real push race between two clones)
  * .github/workflows/daily-player-stats-refresh.yml -- no longer swallows every failure

    python3 scripts/test_health_freshness.py
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
import daily_health_check as H  # noqa: E402
import _scraper_health as SH  # noqa: E402

NOW = datetime(2026, 10, 3, 20, 33, tzinfo=timezone.utc)      # a daily-health-check slot (14:33 MT)


def ago(h):
    return NOW - timedelta(hours=h)


def iso(h):
    return ago(h).isoformat()


def utc_txt(h):
    return ago(h).strftime("%Y-%m-%d %H:%M UTC")


class ParseStamp(unittest.TestCase):
    def test_formats(self):
        self.assertEqual(H.parse_stamp("2026-10-03 16:32 UTC"), datetime(2026, 10, 3, 16, 32, tzinfo=timezone.utc))
        self.assertEqual(H.parse_stamp("2026-10-03T18:41:28.855766+00:00"), datetime(2026, 10, 3, 18, 41, 28, 855766, tzinfo=timezone.utc))
        self.assertEqual(H.parse_stamp("2026-10-03T19:22:54Z"), datetime(2026, 10, 3, 19, 22, 54, tzinfo=timezone.utc))
        self.assertEqual(H.parse_stamp("2026-10-03T19:22:54"), datetime(2026, 10, 3, 19, 22, 54, tzinfo=timezone.utc))   # naive = UTC

    def test_unusable(self):
        for v in (None, "", "yesterday", 12345, {}):
            self.assertIsNone(H.parse_stamp(v))


class FileFreshness(unittest.TestCase):
    def docs(self, files):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        for name, doc in files.items():
            (Path(td.name) / name).write_text(doc if isinstance(doc, str) else json.dumps(doc))
        return Path(td.name)

    def run_one(self, fname, key, stamp, warn, stale, label="X", extra=None):
        doc = {key: stamp, **(extra or {})}
        d = self.docs({fname: doc})
        return H.check_data_freshness(d, NOW, [(fname, label, key, warn, stale)])

    def test_thresholds_data_json(self):
        self.assertEqual(self.run_one("data.json", "generated", iso(13.9), 14, 26), [])
        lvl, msg = self.run_one("data.json", "generated", iso(14.1), 14, 26)[0]
        self.assertEqual(lvl, "note")
        self.assertIn("aging", msg)
        lvl, msg = self.run_one("data.json", "generated", iso(26.5), 14, 26)[0]
        self.assertEqual(lvl, "alert")
        self.assertIn("STALE", msg)

    def test_schedule_and_euro_hockey_use_their_own_thresholds(self):
        self.assertEqual(self.run_one("nhl_schedule.json", "generated_at", utc_txt(19), 20, 36), [])
        self.assertEqual(self.run_one("nhl_schedule.json", "generated_at", utc_txt(21), 20, 36)[0][0], "note")
        self.assertEqual(self.run_one("shl_schedule.json", "generated_at", utc_txt(13), 12, 28)[0][0], "note")
        self.assertEqual(self.run_one("shl_schedule.json", "generated_at", utc_txt(29), 12, 28)[0][0], "alert")

    def test_missing_file_and_missing_stamp_are_notes_not_alerts(self):
        d = self.docs({})
        res = H.check_data_freshness(d, NOW, [("gone.json", "Gone", "generated_at", 1, 2)])
        self.assertEqual([r[0] for r in res], ["note"])
        d = self.docs({"a.json": {"generated_at": utc_txt(1)}})
        res = H.check_data_freshness(d, NOW, [("a.json", "A", "ts", 1, 2)])        # the key we look for is not in the file
        self.assertEqual(res[0][0], "note")
        self.assertIn("no readable", res[0][1])

    def test_truncated_json_falls_back_to_regex(self):
        d = self.docs({"big.json": '{"generated_at": "%s", "games": [ {"a": 1}, {"b"' % utc_txt(40)})
        res = H.check_data_freshness(d, NOW, [("big.json", "Big", "generated_at", 20, 36)])
        self.assertEqual(res[0][0], "alert")

    def test_live_feed_uses_tight_limits_only_when_games_are_live(self):
        live = [("live_data.json", "Live", "ts", 1.5, 4)]
        d = self.docs({"live_data.json": {"ts": iso(5), "hasLiveGames": True}})
        self.assertEqual(H.check_data_freshness(d, NOW, live)[0][0], "alert")
        d = self.docs({"live_data.json": {"ts": iso(2), "hasLiveGames": True}})
        self.assertEqual(H.check_data_freshness(d, NOW, live)[0][0], "note")
        d = self.docs({"live_data.json": {"ts": iso(5), "hasLiveGames": False}})        # idle feed: only a 14h/26h deadman
        self.assertEqual(H.check_data_freshness(d, NOW, live), [])
        d = self.docs({"live_data.json": {"ts": iso(30), "hasLiveGames": False}})
        self.assertEqual(H.check_data_freshness(d, NOW, live)[0][0], "alert")

    def test_live_feed_skipped_while_the_tracker_cron_is_idle(self):
        idle = NOW.replace(hour=8)
        d = self.docs({"live_data.json": {"ts": (idle - timedelta(hours=6)).isoformat(), "hasLiveGames": True}})
        self.assertEqual(H.check_data_freshness(d, idle, [("live_data.json", "Live", "ts", 1.5, 4)]), [])

    def test_real_docs_do_not_crash(self):
        res = H.check_data_freshness(ROOT / "docs", NOW)
        self.assertIsInstance(res, list)
        for lvl, msg in res:
            self.assertIn(lvl, ("alert", "note"))

    def test_thresholds_match_the_app_header_line(self):
        """docs/app.html _FRESH_SRC (read-only) and FRESHNESS_FILES must agree on warn/stale for the files both cover."""
        html = (ROOT / "docs" / "app.html").read_text()
        block = html[html.index("const _FRESH_SRC=["):]
        block = block[:block.index("];")]
        app = {m.group(1): (float(m.group(2)), float(m.group(3)))
               for m in re.finditer(r"\{f:'([^']+)',l:'[^']*',k:'[^']*',warn:(\d+),bad:(\d+)\}", block)}
        self.assertGreaterEqual(len(app), 9)
        ours = {f: (w, s) for f, _l, _k, w, s in H.FRESHNESS_FILES}
        for fname, th in app.items():
            self.assertIn(fname, ours, fname)
            self.assertEqual(ours[fname], th, fname)


def run(conclusion, hours_ago, status="completed", url="https://github.com/x/runs/1"):
    t = ago(hours_ago).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"status": status, "conclusion": conclusion, "created_at": t, "updated_at": t, "html_url": url}


def api_returning(runs):
    return lambda path: {"workflow_runs": runs}


class RefreshWorkflows(unittest.TestCase):
    def check(self, runs, max_h=20, fails=1):
        return H.check_refresh_workflow("wf.yml", "WF", max_h, fails, api_get=api_returning(runs), now=NOW)

    def test_healthy(self):
        self.assertIsNone(self.check([run("success", 3), run("success", 11)]))

    def test_latest_failed(self):
        lvl, msg = self.check([run("failure", 1), run("success", 9)])
        self.assertEqual(lvl, "alert")
        self.assertIn("FAILED", msg)

    def test_old_success_even_if_latest_is_fine_is_impossible_but_old_success_alerts(self):
        lvl, msg = self.check([run("success", 30)])
        self.assertEqual(lvl, "alert")
        self.assertIn("last SUCCESS was", msg)

    def test_failure_streak_and_old_success_both_reported(self):
        lvl, msg = self.check([run("failure", 2), run("timed_out", 10), run("success", 40)])
        self.assertIn("last 2 run(s) FAILED", msg)
        self.assertIn("last SUCCESS was", msg)

    def test_cancelled_and_skipped_runs_are_ignored(self):
        self.assertIsNone(self.check([run("cancelled", 1), run("skipped", 2), run("success", 4)]))

    def test_no_success_in_window(self):
        lvl, msg = self.check([run("failure", 1), run("failure", 5)])
        self.assertIn("no successful run", msg)

    def test_no_runs_at_all(self):
        self.assertEqual(self.check([])[0], "alert")

    def test_api_error_is_only_a_note(self):
        def boom(path):
            raise OSError("503")
        res = H.check_refresh_workflow("wf.yml", "WF", 20, 1, api_get=boom, now=NOW)
        self.assertEqual(res[0], "note")

    def test_frequent_workflow_tolerates_isolated_flakes(self):
        runs = [run("failure", 0.5), run("success", 1), run("success", 1.5)]
        self.assertIsNone(self.check(runs, max_h=14, fails=3))
        runs = [run("failure", 0.5), run("failure", 1), run("failure", 1.5), run("success", 2)]
        self.assertEqual(self.check(runs, max_h=14, fails=3)[0], "alert")

    def test_check_refresh_health_covers_every_monitored_workflow(self):
        seen = []

        def api(path):
            seen.append(path)
            return {"workflow_runs": [run("success", 1)]}
        self.assertEqual(H.check_refresh_health(NOW, api_get=api), [])
        self.assertEqual(len(seen), len(H.REFRESH_MONITORED))

    def test_monitored_workflow_files_exist_and_cover_the_refresh_set(self):
        names = {f for f, *_ in H.REFRESH_MONITORED}
        for f in names:
            self.assertTrue((ROOT / ".github" / "workflows" / f).exists(), f)
        for must in ("scheduled-refresh.yml", "daily-schedules-refresh.yml", "shl-schedule-refresh.yml", "liiga-schedule-refresh.yml",
                     "nla-schedule-refresh.yml", "extraliga-schedule-refresh.yml", "opta-soccer-stats-daily.yml",
                     "cfb-stats-weekly.yml", "nfl-stats-weekly.yml", "live-tracker.yml", "daily-player-stats-refresh.yml"):
            self.assertIn(must, names)

    def test_weekly_digest_includes_the_refresh_workflows(self):
        try:
            import weekly_health_digest as W
        except ImportError as exc:  # requests missing in this interpreter
            self.skipTest(str(exc))
        listed = {f for f, _l in W.MONITORED}
        self.assertIn("scheduled-refresh.yml", listed)
        self.assertIn("live-tracker.yml", listed)
        self.assertIn("auto-lock-settle.yml", listed)        # the original entries are still there


class MainPolicy(unittest.TestCase):
    """main(): STALE/failed -> e-mail; AGING-only -> logged, no e-mail; everything fine -> silent."""

    def run_main(self, data_findings, wf_findings, token="tok"):
        sent = []
        out = io.StringIO()
        with mock.patch.object(H, "GITHUB_TOKEN", token), mock.patch.object(H, "ALERT_TO", "owner@example.com"), \
                mock.patch.object(H, "check_lock_markers", return_value=[]), \
                mock.patch.object(H, "check_workflow", return_value=None), \
                mock.patch.object(H, "check_refresh_health", return_value=wf_findings), \
                mock.patch.object(H, "check_data_freshness", return_value=data_findings), \
                mock.patch.object(H, "send_email", side_effect=lambda s, to, body: (sent.append((s, to, body)) or (True, "ok"))), \
                contextlib.redirect_stdout(out):
            H.main()
        return sent, out.getvalue()

    def test_clean_day_is_silent(self):
        sent, out = self.run_main([], [])
        self.assertEqual(sent, [])
        self.assertIn("no alert sent", out)

    def test_warn_only_does_not_email(self):
        sent, out = self.run_main([("note", "Engine data: aging")], [])
        self.assertEqual(sent, [])
        self.assertIn("::warning::Engine data: aging", out)

    def test_stale_file_emails_and_lists_watch_items(self):
        sent, _ = self.run_main([("alert", "Engine data (data.json): STALE"), ("note", "NHL schedule: aging")], [])
        self.assertEqual(len(sent), 1)
        self.assertIn("STALE", sent[0][2])
        self.assertIn("NHL schedule: aging", sent[0][2])

    def test_failed_refresh_workflow_emails(self):
        sent, _ = self.run_main([], [("alert", "Main data refresh: last 1 run(s) FAILED")])
        self.assertEqual(len(sent), 1)

    def test_file_checks_still_run_without_a_github_token(self):
        sent, _ = self.run_main([("alert", "x: STALE")], [("alert", "never used")], token="")
        self.assertEqual(len(sent), 1)
        self.assertNotIn("never used", sent[0][2])      # the Actions API checks are skipped without a token

    def test_crashing_freshness_check_is_fail_open(self):
        out = io.StringIO()
        with mock.patch.object(H, "GITHUB_TOKEN", ""), mock.patch.object(H, "check_lock_markers", return_value=[]), \
                mock.patch.object(H, "check_data_freshness", side_effect=RuntimeError("boom")), contextlib.redirect_stdout(out):
            H.main()                                    # must not raise
        self.assertIn("crashed", out.getvalue())


class ScraperHealth(unittest.TestCase):
    def test_merge_health_unions_dedupes_and_trims(self):
        a = [{"league": "SHL", "ts": "2026-10-03T01:00:00Z", "fixtures": 1, "results": 1}]
        b = [{"league": "SHL", "ts": "2026-10-03T01:00:00Z", "fixtures": 1, "results": 1},
             {"league": "NLA", "ts": "2026-10-03T02:00:00Z", "fixtures": 2, "results": 2}]
        m = SH.merge_health(a, b)
        self.assertEqual([r["league"] for r in m], ["SHL", "NLA"])
        many = [{"league": "L", "ts": f"2026-10-03T{i // 60:02d}:{i % 60:02d}:00Z", "fixtures": i, "results": i} for i in range(100)]
        self.assertEqual(len(SH.merge_health(many, [])), SH.ROLLING_WINDOW)

    def test_log_scrape_is_atomic_and_appends(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "docs" / "scraper_health.json"
            with mock.patch.object(SH, "LOG_PATH", p):
                SH.log_scrape("SHL", 3, 4)
                SH.log_scrape("NLA", 5, 6)
            data = json.loads(p.read_text())
            self.assertEqual([(r["league"], r["fixtures"], r["results"]) for r in data], [("SHL", 3, 4), ("NLA", 5, 6)])
            self.assertEqual([x.name for x in p.parent.iterdir()], ["scraper_health.json"])      # no temp / lock file in docs/

    def test_log_scrape_survives_a_corrupt_file(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "scraper_health.json"
            p.write_text("{oops")
            with mock.patch.object(SH, "LOG_PATH", p):
                SH.log_scrape("SHL", 1, 1)
            self.assertEqual(len(json.loads(p.read_text())), 1)

    def test_fetch_scripts_commit_the_health_file(self):
        for league in ("shl", "liiga", "nla", "extraliga"):
            src = (HERE / f"fetch_{league}.py").read_text()
            self.assertIn("commit_and_push_health(", src, league)
            self.assertIn("from _scraper_health import commit_and_push as commit_and_push_health", src, league)


def git(cwd, *args, check=True):
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t",
               GIT_CONFIG_GLOBAL="/dev/null", GIT_TERMINAL_PROMPT="0")
    r = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, env=env)
    if check and r.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {r.stderr}")
    return r


class CommitAndPush(unittest.TestCase):
    """Real git against local repos only (no network)."""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        self.base = Path(self.td.name)
        self._env = mock.patch.dict(os.environ, {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                                                  "GIT_COMMITTER_EMAIL": "t@t", "GIT_CONFIG_GLOBAL": "/dev/null"})
        self._env.start()
        self.addCleanup(self._env.stop)
        self.origin = self.base / "origin.git"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(self.origin)], check=True)
        self.a = self.clone("a")
        (self.a / "docs").mkdir()
        (self.a / "docs" / "shl_schedule.json").write_text("{}")
        git(self.a, "add", "-A")
        git(self.a, "commit", "-q", "-m", "init")
        git(self.a, "push", "-q", "origin", "HEAD:main")

    def clone(self, name):
        p = self.base / name
        subprocess.run(["git", "clone", "-q", str(self.origin), str(p)], check=True, capture_output=True)
        git(p, "checkout", "-q", "-B", "main")
        return p

    def write(self, root, recs):
        (root / "docs").mkdir(exist_ok=True)
        (root / "docs" / "scraper_health.json").write_text(json.dumps(recs, indent=2))

    def rec(self, league, minute):
        return {"league": league, "ts": f"2026-10-03T10:{minute:02d}:00Z", "fixtures": 1, "results": 1}

    def remote_list(self):
        r = subprocess.run(["git", "--git-dir", str(self.origin), "show", "main:docs/scraper_health.json"], capture_output=True, text=True)
        return json.loads(r.stdout)

    def test_first_commit_creates_the_file_on_the_remote(self):
        self.write(self.a, [self.rec("SHL", 1)])
        self.assertTrue(SH.commit_and_push("health", root=self.a, sleep=lambda s: None))
        self.assertEqual(self.remote_list(), [self.rec("SHL", 1)])

    def test_nothing_new_is_a_no_op(self):
        self.write(self.a, [self.rec("SHL", 1)])
        SH.commit_and_push("health", root=self.a, sleep=lambda s: None)
        head = git(self.a, "rev-parse", "HEAD").stdout
        self.assertTrue(SH.commit_and_push("health", root=self.a, sleep=lambda s: None))
        self.assertEqual(git(self.a, "rev-parse", "HEAD").stdout, head)

    def test_missing_file_returns_false(self):
        self.assertFalse(SH.commit_and_push("health", root=self.a, sleep=lambda s: None))

    def test_only_the_health_file_is_committed(self):
        self.write(self.a, [self.rec("SHL", 1)])
        (self.a / "docs" / "shl_schedule.json").write_text('{"dirty": true}')       # an unrelated working-tree change
        SH.commit_and_push("health", root=self.a, sleep=lambda s: None)
        show = git(self.a, "show", "--stat", "--format=", "HEAD").stdout
        self.assertIn("scraper_health.json", show)
        self.assertNotIn("shl_schedule.json", show)

    def test_push_race_between_two_leagues_merges_instead_of_losing(self):
        self.write(self.a, [self.rec("SHL", 1)])
        self.assertTrue(SH.commit_and_push("health a1", root=self.a, sleep=lambda s: None))
        b = self.clone("b")                                      # B starts from the state that has a1
        # A records again and pushes first ...
        self.write(self.a, [self.rec("SHL", 1), self.rec("SHL", 2)])
        self.assertTrue(SH.commit_and_push("health a2", root=self.a, sleep=lambda s: None))
        # ... then B (still based on a1) records its own entry: the rebase conflicts on the shared file.
        self.write(b, [self.rec("SHL", 1), self.rec("NLA", 3)])
        self.assertTrue(SH.commit_and_push("health b1", root=b, sleep=lambda s: None))
        got = self.remote_list()
        self.assertEqual(sorted((r["league"], r["ts"]) for r in got),
                         sorted([("SHL", self.rec("SHL", 1)["ts"]), ("SHL", self.rec("SHL", 2)["ts"]), ("NLA", self.rec("NLA", 3)["ts"])]))
        self.assertEqual(git(b, "status", "--porcelain").stdout.strip(), "")     # no half-finished rebase / dirty tree left behind


class PlayerStatsWorkflow(unittest.TestCase):
    def setUp(self):
        self.text = (ROOT / ".github" / "workflows" / "daily-player-stats-refresh.yml").read_text()

    def test_tolerant_steps_still_tolerant_but_the_job_fails_loudly(self):
        self.assertIn("continue-on-error: true", self.text)
        self.assertIn("Fail loudly if any step failed", self.text)
        last = self.text[self.text.index("Fail loudly if any step failed"):]
        self.assertIn("if: always()", last)
        self.assertIn("exit 1", last)
        self.assertIn("GITHUB_STEP_SUMMARY", last)

    def test_every_tolerant_step_has_an_id_that_the_final_step_checks(self):
        steps = re.split(r"\n      - name: ", self.text)[1:]
        tolerant = [s for s in steps if "continue-on-error: true" in s and not s.startswith("Fail loudly")]
        self.assertGreaterEqual(len(tolerant), 6)
        results = self.text[self.text.index("RESULTS: |"):]
        for s in tolerant:
            m = re.search(r"\n        id: (\w+)", s)
            self.assertIsNotNone(m, s.splitlines()[0])
            self.assertIn(f"steps.{m.group(1)}.outcome", results, s.splitlines()[0])

    def test_yaml_parses(self):
        try:
            import yaml
        except ImportError:
            self.skipTest("PyYAML not installed")
        doc = yaml.safe_load(self.text)
        self.assertEqual(doc["jobs"]["refresh-player-stats"]["steps"][-1]["name"], "Fail loudly if any step failed")


class RunLinkTests(unittest.TestCase):
    def test_every_stamped_refresh_file_maps_to_a_workflow_link(self):
        for fname, _label, _key, _w, _s in H.FRESHNESS_FILES:
            if fname in H.WORKFLOW_FOR_FILE:
                link = H.run_link(H.WORKFLOW_FOR_FILE[fname])
                self.assertIn("/actions/workflows/", link)
                self.assertIn(H.REPO, link)

    def test_stale_message_carries_the_run_now_link(self):
        from datetime import datetime, timezone, timedelta
        now = datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc)
        import json, tempfile, pathlib
        with tempfile.TemporaryDirectory() as d:
            pathlib.Path(d, "data.json").write_text(json.dumps({"generated": (now - timedelta(hours=30)).isoformat()}))
            out = H.check_data_freshness(pathlib.Path(d), now=now, files=[("data.json", "Engine data", "generated", 14, 26)])
        self.assertEqual(out[0][0], "alert")
        self.assertIn("scheduled-refresh.yml", out[0][1])

    def test_unknown_workflow_gives_no_link(self):
        self.assertEqual(H.run_link(None), "")


if __name__ == "__main__":
    unittest.main(verbosity=1)
