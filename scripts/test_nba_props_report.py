#!/usr/bin/env python3
"""Tests for scripts/nba_props_report.py: games-played status and the 20-game projection, prop grading (incl. the posted-line trust rule), the summary splits and data-quality flags, the
Monday-digest section, and the ONE-TIME 20-game email.  Nothing is sent: the mail function is injected.

    python3 scripts/test_nba_props_report.py
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import nba_props_report as R  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def game(day, home, away, state="post", **kw):
    g = {"id": f"{day}{home}{away}", "day": day, "home": home, "away": away, "state": state, "seasonType": 2, "preseason": False}
    g.update(kw)
    return g


def prop(pid, bet, outcome, date="2026-10-21", stat=None, alt=None, win=0.66, dec=1.8, **kw):
    p = {"id": pid, "sport": "NBA", "betType": "PROP", "betOn": bet, "outcome": outcome, "date": date, "winProb": win, "decOdds": dec, "player": bet.split(" OVER ")[0].split(" UNDER ")[0],
         "hA": "DEN", "awA": "OKC"}
    if stat is not None:
        p["playerResult"] = stat
    if alt:
        p["altLine"] = alt
    p.update(kw)
    return p


class Games(unittest.TestCase):
    def sched(self, n_games_each, teams=("A", "B", "C", "D")):
        games = []
        for i in range(n_games_each):
            d = (R.date(2026, 10, 20) + R.timedelta(days=i)).isoformat()
            games += [game(d, teams[0], teams[1]), game(d, teams[2], teams[3])]
        return {"games": games}

    def test_counts_final_regular_season_games_per_team(self):
        s = {"games": [game("2026-10-20", "A", "B"), game("2026-10-21", "A", "C"), game("2026-10-22", "A", "B", state="in"), game("2026-10-10", "A", "B", preseason=True),
                       game("2026-10-23", "A", "B", seasonType=3)]}
        self.assertEqual(dict(R.team_games(s)), {"A": 2, "B": 1, "C": 1})

    def test_projection_uses_the_pace_so_far(self):
        g = R.games_status(self.sched(5))                      # 5 games each over 5 days = 1 game/team/day
        self.assertEqual((g["median"], g["min"], g["max"], g["ready"]), (5, 5, 5, False))
        self.assertEqual(g["projected"], "2026-11-08")         # last final 10-24 + 15 more games at 1 game/team/day
        self.assertAlmostEqual(g["per_day"], 1.0)

    def test_ready_when_the_median_team_has_the_target(self):
        g = R.games_status(self.sched(20))
        self.assertTrue(g["ready"])
        self.assertIsNone(g["projected"])

    def test_a_few_teams_ahead_do_not_make_it_ready(self):
        games = [game("2026-10-20", "A", "B")] * 25 + [game("2026-10-21", "C", "D")] * 3 + [game("2026-10-22", "E", "F")] * 3
        g = R.games_status({"games": games})
        self.assertEqual((g["max"], g["ready"]), (25, False))   # 2 of 6 teams are at 25, the median team has 3

    def test_empty_schedule_is_not_ready_and_does_not_crash(self):
        g = R.games_status({"games": []})
        self.assertFalse(g["ready"])
        self.assertEqual(g["teams"], 0)


class Grading(unittest.TestCase):
    def test_grade_over_under_and_push(self):
        p = prop("a", "X OVER PTS 20.5", "win", stat=21)
        self.assertEqual(R.grade_at(p, 20.5), "win")
        self.assertEqual(R.grade_at(p, 21), "push")
        self.assertEqual(R.grade_at(prop("b", "X UNDER AST 6.5", "win", stat=5), 6.5), "win")
        self.assertIsNone(R.grade_at(prop("c", "X OVER PTS 20.5", "win"), 20.5))               # no stat

    def test_posted_line_result_of_an_adjusted_pick(self):
        alt = {"line": 9.5, "posted": 11.5}
        self.assertEqual(R.posted_result(prop("a", "X OVER REB 9.5", "win", stat=10, alt=alt)), "loss")      # cleared the cushion, missed the posted line
        self.assertEqual(R.posted_result(prop("b", "X OVER REB 9.5", "win", stat=12, alt=alt)), "win")

    def test_posted_line_is_not_trusted_when_the_stat_contradicts_the_stored_result(self):
        alt = {"line": 9.5, "posted": 11.5}
        self.assertIsNone(R.posted_result(prop("a", "X OVER REB 9.5", "loss", stat=12, alt=alt)))            # stored LOSS but 12 > 9.5
        self.assertIsNone(R.posted_result(prop("b", "X OVER REB 9.5", "win", alt=alt)))                       # no stat
        self.assertIsNone(R.posted_result(prop("c", "X OVER REB 9.5", "win", stat=12)))                       # not an adjusted pick

    def test_wilson_interval_widens_with_a_small_sample(self):
        lo, hi = R.wilson(8, 10)
        lo2, hi2 = R.wilson(80, 100)
        self.assertLess(lo, lo2)
        self.assertGreater(hi, hi2)
        self.assertEqual(R.wilson(0, 0), (0.0, 1.0))


class Summary(unittest.TestCase):
    LEDGER = [
        prop("p1", "A OVER PTS 20.5", "win", date="2026-10-20", stat=25),
        prop("p2", "B UNDER AST 6.5", "loss", date="2026-10-21", stat=9),
        prop("p3", "C OVER REB 9.5", "win", date="2026-10-26", stat=10, alt={"line": 9.5, "posted": 11.5}),
        prop("p4", "A OVER REB 8.5", "win", date="2026-10-22"),                                                    # settled, no stat
        prop("p5", "D OVER PTS 10.5", "win", date="2026-10-22", stat=5),                                          # stored win, stat says loss
        prop("p6", "E OVER AST 5.5", "push", date="2026-10-26", voidReason="DNP"),
        prop("p7", "F OVER PTS 10.5", "pending", date="2026-10-27"),
        prop("old", "G OVER PTS 10.5", "win", date="2026-06-03", stat=30),                                         # last season: excluded
        {"id": "nbaml", "sport": "NBA", "betType": "ML", "betOn": "DEN ML", "outcome": "win", "date": "2026-10-21"},  # not a prop
        prop("gone", "H OVER PTS 10.5", "_removed", date="2026-10-21"),
    ]

    def setUp(self):
        self.s = R.summarize(self.LEDGER)

    def test_only_this_seasons_nba_props_count(self):
        self.assertEqual(self.s["total"], 7)

    def test_record_and_pending(self):
        o = self.s["overall"]
        self.assertEqual((o["w"], o["l"], o["push"], o["pending"]), (4, 1, 1, 1))

    def test_splits(self):
        self.assertEqual(set(self.s["by_stat"]), {"PTS", "AST", "REB"})
        self.assertEqual(set(self.s["by_dir"]), {"OVER", "UNDER"})
        self.assertEqual(set(self.s["by_line"]), {"ADJUSTED line", "plain line"})
        self.assertEqual(self.s["by_gate"]["before the gate"]["n"], 4)       # p1, p2, p4, p5
        self.assertEqual(self.s["by_gate"]["since the featured-player gate (10-26)"]["n"], 1)

    def test_adjusted_vs_posted(self):
        a = self.s["adjusted"]
        self.assertEqual((a["n"], a["graded"], a["locked_w"], a["posted_w"], a["posted_l"], a["saved"], a["cost"]), (1, 1, 1, 0, 1, 1, 0))

    def test_data_quality_flags(self):
        q = self.s["quality"]
        self.assertEqual(q["missing_stat"], ["p4"])                  # p6 is a void: not counted as missing
        self.assertEqual(q["mismatch"], ["p5"])
        self.assertEqual(q["void"], 1)

    def test_verdict_warns_about_a_small_sample_and_names_the_data_problems(self):
        text = " ".join(R.verdict(self.s))
        self.assertIn("below the 50", text)
        self.assertIn("no stored box-score stat", text)
        self.assertIn("disagree", text)

    def test_low_minute_voids_are_listed_and_never_flagged_as_mismatches(self):
        led = [prop("v1", "A UNDER PTS 10.5", "push", date="2026-10-21", stat=0, playerMin=5, voidReason="LOW_MIN", altLine={"line": 10.5, "posted": 12.5}),
               prop("v2", "B OVER PTS 10.5", "win", date="2026-10-21", stat=20, playerMin=30)]
        q = R.summarize(led)
        self.assertEqual(q["quality"]["low_min"], [("v1", 5)])
        self.assertEqual(q["quality"]["mismatch"], [])                       # push with a stat of 0 is a void, not a disagreement
        self.assertEqual(q["quality"]["minutes_unchecked"], [])
        self.assertEqual(q["adjusted"]["graded"], 0)                         # a void has no posted-line result
        self.assertIn("under 10 minutes", " ".join(R.verdict(q)))
        self.assertEqual((q["overall"]["w"], q["overall"]["l"], q["overall"]["push"]), (1, 0, 1))

    def test_concentration(self):
        led = [prop(f"c{i}", f"Star OVER PTS {20 + i}.5", "win", date="2026-10-21", stat=40) for i in range(3)]
        c = R.summarize(led)["concentration"]
        self.assertEqual(c["players_3plus"], [("Star", 3)])
        self.assertEqual(c["same_player_same_day"], 1)
        self.assertEqual(c["most_props_one_game"], 3)

    def test_week_label_crosses_months(self):
        self.assertEqual(R._week_label("2026-10-28", R.date(2026, 10, 20)), "Oct 27-Nov 2")

    def test_empty_ledger(self):
        s = R.summarize([])
        self.assertEqual(s["total"], 0)
        self.assertIsNone(s["overall"]["pct"])
        R.render_text(s, R.games_status({"games": []}))              # must not raise


class Realistic(unittest.TestCase):
    """The real committed files render without error."""

    def test_real_files(self):
        ledger = json.loads((ROOT / "docs" / "picks_backup.json").read_text())
        sched = json.loads((ROOT / "docs" / "nba_schedule.json").read_text())
        text = R.render_text(R.summarize(ledger), R.games_status(sched))
        self.assertIn("NBA PLAYER PROPS REVIEW", text)
        self.assertIn("<h3>6. NBA props review</h3>", R.weekly_section_html(ROOT))


class Section(unittest.TestCase):
    def test_section_never_raises_when_the_files_are_missing(self):
        with tempfile.TemporaryDirectory() as d:
            html = R.weekly_section_html(Path(d))
        self.assertIn("Unavailable", html)

    def test_digest_includes_the_section(self):
        import weekly_health_digest as W
        html = W.build_email_html(None, [], [], [])
        self.assertIn("6. NBA props review", html)


class Milestone(unittest.TestCase):
    def root(self, games_each):
        d = tempfile.TemporaryDirectory()
        r = Path(d.name)
        (r / "docs").mkdir()
        games = []
        for i in range(games_each):
            day = (R.date(2026, 10, 20) + R.timedelta(days=i)).isoformat()
            games += [game(day, "A", "B"), game(day, "C", "D")]
        (r / "docs" / "nba_schedule.json").write_text(json.dumps({"games": games}))
        (r / "docs" / "picks_backup.json").write_text(json.dumps([prop("p1", "A OVER PTS 20.5", "win", stat=25)]))
        self.addCleanup(d.cleanup)
        return r

    def test_not_yet_sends_nothing(self):
        sent = []
        out = R.check_milestone(self.root(10), send=lambda *a, **k: sent.append(a) or (True, "ok"), to="me@x")
        self.assertEqual((out, sent), ("not-yet", []))

    def test_sends_once_at_the_target_and_never_again(self):
        r, sent = self.root(20), []
        send = lambda subj, to, html: sent.append((subj, to, html)) or (True, "ok")  # noqa: E731
        self.assertEqual(R.check_milestone(r, send=send, to="me@x", today="2026-12-02"), "sent")
        self.assertEqual(R.check_milestone(r, send=send, to="me@x", today="2026-12-03"), "already-sent")
        self.assertEqual(len(sent), 1)
        subj, to, html = sent[0]
        self.assertIn("20 games reached", subj)
        self.assertIn("EMAIL_NBA_PROPS", html)                                        # says exactly what to flip
        self.assertEqual(json.loads((r / R.STATE_REL).read_text())["sent"], "2026-12-02")

    def test_a_failed_send_is_retried_next_time(self):
        r = self.root(20)
        self.assertEqual(R.check_milestone(r, send=lambda *a, **k: (False, "smtp down"), to="me@x"), "failed")
        self.assertFalse((r / R.STATE_REL).exists())                                  # not recorded -> the next pass tries again
        self.assertEqual(R.check_milestone(r, send=lambda *a, **k: (True, "ok"), to="me@x"), "sent")

    def test_no_recipient_does_not_record_anything(self):
        r = self.root(20)
        self.assertEqual(R.check_milestone(r, send=lambda *a, **k: (True, "ok"), to=""), "no-recipient")
        self.assertFalse((r / R.STATE_REL).exists())

    def test_wired_into_the_daily_health_check_and_its_publish_step(self):
        src = (ROOT / "scripts" / "daily_health_check.py").read_text()
        self.assertIn("nba_props_report.check_milestone", src)
        wf = (ROOT / ".github" / "workflows" / "daily-health-check.yml").read_text()
        self.assertIn("data/nba_props_milestone.json", wf)


if __name__ == "__main__":
    unittest.main(verbosity=2)
