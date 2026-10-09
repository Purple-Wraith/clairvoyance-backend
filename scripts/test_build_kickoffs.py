#!/usr/bin/env python3
"""docs/kickoffs.json (scripts/build_kickoffs.py): every upcoming start the engine locks, any sport, merged by minute -- what the scheduler Worker sweeps before.  Only locked sports/leagues,
only games not yet started, regular season only, 72 h ahead.  The Pages deploy builds it into every artifact.

    python3 scripts/test_build_kickoffs.py
"""
import json, sys, tempfile, unittest
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import build_kickoffs as K  # noqa: E402

NOW = datetime(2026, 10, 10, 10, 0, tzinfo=timezone.utc)


def put(d: Path, name: str, obj) -> None:
    (d / name).write_text(json.dumps(obj))


class Collect(unittest.TestCase):
    def docs(self):
        d = Path(tempfile.mkdtemp())
        g = lambda t, state="pre", **kw: {"id": t, "date": t, "state": state, **kw}  # noqa: E731
        put(d, "nhl_schedule.json", {"games": [g("2026-10-10T23:00Z"), g("2026-10-10T23:00Z"), g("2026-10-10T09:00Z", "post"), g("2026-10-10T09:30Z", "in"), g("2026-10-14T23:00Z")]})
        put(d, "shl_schedule.json", {"games": [g("2026-10-10T13:00Z")]})
        put(d, "nba_schedule.json", {"games": [g("2026-10-10T23:00Z", preseason=True), g("2026-10-11T00:00Z", preseason=False), g("2026-10-11T00:30Z", postponed=True)]})
        put(d, "nfl_schedule.json", {"weeks": {"Preseason Week 3": [g("2026-10-10T18:00Z")], "Week 5": [g("2026-10-11T17:00Z", seasonType=2), g("2026-10-11T17:00Z", seasonType=2), g("2026-10-11T20:05Z", "post")]}})
        put(d, "cfb_schedule.json", {"weeks": {"Week 6": [g("2026-10-10T16:00Z"), g("2026-10-10T19:30Z")]}})
        mk = lambda t, status="pre": {"id": t, "date": t, "status": status}  # noqa: E731
        put(d, "soccer_schedule_tomorrow.json", {"leagues": {"pl": [mk("2026-10-10T11:30Z"), mk("2026-10-10T11:30Z"), mk("2026-10-10T14:00Z", "in")], "bl": [mk("2026-10-10T13:30Z")],
                                                              "mls": [mk("2026-10-10T20:00Z")], "ita": [mk("2026-10-10T16:00Z")]}})
        return d

    def test_collects_only_what_the_engine_locks(self):
        out = {x["t"]: x["sports"] for x in K.collect(self.docs(), NOW)}
        self.assertEqual(out["2026-10-10T23:00Z"], ["NHL"])                      # NBA preseason on the same minute is NOT added
        self.assertEqual(out["2026-10-11T00:00Z"], ["NBA"])                      # regular season counts
        self.assertEqual(out["2026-10-10T13:00Z"], ["SHL"])
        self.assertEqual(out["2026-10-11T17:00Z"], ["NFL"])                      # regular season only, duplicates merged
        self.assertEqual(out["2026-10-10T16:00Z"], ["CFB", "SERIEA"])            # same minute, two sports -> one entry
        self.assertEqual(out["2026-10-10T11:30Z"], ["PL"])
        for gone in ("2026-10-10T09:00Z", "2026-10-10T09:30Z",                   # final / in progress
                     "2026-10-10T18:00Z",                                        # NFL preseason
                     "2026-10-11T00:30Z",                                        # postponed NBA
                     "2026-10-11T20:05Z",                                        # final NFL
                     "2026-10-10T13:30Z", "2026-10-10T20:00Z",                   # Bundesliga, MLS (retired)
                     "2026-10-10T14:00Z",                                        # soccer already in play
                     "2026-10-14T23:00Z"):                                       # beyond 72 h
            self.assertNotIn(gone, out, gone)
        self.assertEqual(list(out), sorted(out))                                 # sorted by time

    def test_missing_or_broken_files_never_raise(self):
        d = Path(tempfile.mkdtemp())
        (d / "nhl_schedule.json").write_text("not json")
        self.assertEqual(K.collect(d, NOW), [])

    def test_build_payload_shape(self):
        p = K.build(self.docs(), NOW)
        self.assertEqual(p["generated_at"], "2026-10-10T10:00Z")
        self.assertEqual(p["lookahead_h"], 72)
        self.assertTrue(p["starts"] and all(set(x) == {"t", "sports"} for x in p["starts"]))

    def test_real_repo_files_build(self):
        p = K.build(HERE.parent / "docs", NOW)                                   # smoke: today's real schedule files parse
        self.assertIsInstance(p["starts"], list)


class Wiring(unittest.TestCase):
    def test_pages_deploy_builds_the_list_into_each_artifact(self):
        wf = (HERE.parent / ".github" / "workflows" / "pages-deploy.yml").read_text()
        i = wf.index("name: Build kickoff list")
        self.assertIn("python3 scripts/build_kickoffs.py", wf[i:i + 400])
        self.assertLess(i, wf.index("name: Upload artifact"))                    # built BEFORE the artifact is uploaded
        self.assertIn("continue-on-error: true", wf[i:i + 400])                  # a failure must never block a deploy
        self.assertIn("https://purple-wraith.github.io/clairvoyance-backend/kickoffs.json", wf)       # self-heal when the file is not live yet

    def test_worker_reads_the_same_url(self):
        w = (HERE.parent / "scheduler" / "worker.js").read_text()
        self.assertIn('KICKOFFS_URL = "https://purple-wraith.github.io/clairvoyance-backend/kickoffs.json"', w)


if __name__ == "__main__":
    unittest.main(verbosity=2)
