"""CFB team rankings come from the College Football Playoff committee poll (owner 2026-11-21); AP is only the fallback. Pins the poll choice, the stored label, the refresh day and the app wiring."""
import re, sys, unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import fetch_cfb as F

ROOT = HERE.parent


def poll(short, year, abbrs):
    return {"shortName": short, "season": {"year": year}, "ranks": [
        {"current": i + 1, "previous": i + 1, "trend": "-", "points": 0.0, "firstPlaceVotes": 0,
         "team": {"id": str(i), "abbreviation": a, "location": a.title(), "name": "Team"}} for i, a in enumerate(abbrs)]}


CFP = lambda y=2026: poll("CFP Rankings", y, ["UGA", "UTAH", "ALA"])
AP = lambda y=2026: poll("AP Poll", y, ["UGA", "UTAH", "ND"])


class PickPoll(unittest.TestCase):
    def test_cfp_wins_when_both_are_current(self):
        self.assertEqual(F.pick_poll({"rankings": [AP(), CFP()]})[0], "CFP")

    def test_ap_when_there_is_no_cfp_poll_yet(self):
        self.assertEqual(F.pick_poll({"rankings": [AP(), poll("AFCA Coaches Poll", 2026, ["UGA"])]})[0], "AP")

    def test_ap_when_the_cfp_poll_is_empty(self):
        self.assertEqual(F.pick_poll({"rankings": [AP(), {"shortName": "CFP Rankings", "season": {"year": 2026}, "ranks": []}]})[0], "AP")

    def test_last_seasons_final_cfp_poll_does_not_beat_this_seasons_ap_preseason_poll(self):
        self.assertEqual(F.pick_poll({"rankings": [AP(2027), CFP(2026)]})[0], "AP")

    def test_last_seasons_cfp_is_kept_when_there_is_nothing_newer(self):
        self.assertEqual(F.pick_poll({"rankings": [CFP(2026)]})[0], "CFP")
        self.assertEqual(F.pick_poll({"rankings": [AP(2026), CFP(2026)]})[0], "CFP")     # postseason: the final CFP poll stays

    def test_nothing_current(self):
        self.assertEqual(F.pick_poll({"rankings": []}), (None, None))


class Fetch(unittest.TestCase):
    def fetch(self, payload):
        resp = mock.Mock(); resp.json.return_value = payload; resp.raise_for_status.return_value = None
        with mock.patch.object(F.requests, "get", return_value=resp):
            return F.fetch_rankings_poll()

    def test_rows_come_from_the_cfp_poll(self):
        label, rows = self.fetch({"rankings": [AP(), CFP()], "latestWeek": {"displayValue": "Week 12"}})
        self.assertEqual(label, "CFP")
        self.assertEqual([r["abbr"] for r in rows], ["UGA", "UTAH", "ALA"])          # ALA is CFP #3 (AP has Notre Dame there)
        self.assertEqual(rows[2]["rank"], 3)

    def test_fetch_rankings_still_returns_just_the_rows(self):
        resp = mock.Mock(); resp.json.return_value = {"rankings": [AP(), CFP()]}; resp.raise_for_status.return_value = None
        with mock.patch.object(F.requests, "get", return_value=resp):
            self.assertEqual([r["abbr"] for r in F.fetch_rankings()], ["UGA", "UTAH", "ALA"])

    def test_write_power_stores_which_poll_it_used(self):
        import json, tempfile
        out = Path(tempfile.mkdtemp()) / "cfb_power.json"
        with mock.patch.object(F, "fetch_rankings_poll", return_value=("CFP", [{"rank": 1, "abbr": "UGA"}])), \
             mock.patch.object(F, "fetch_power_ratings", return_value={}), mock.patch.object(F, "POWER_OUT_PATH", out):
            F.write_power()
        d = json.loads(out.read_text())
        self.assertEqual((d["rankingsPoll"], d["rankings"][0]["abbr"]), ("CFP", "UGA"))


class Wiring(unittest.TestCase):
    def test_the_refresh_pulls_rankings_on_wednesday_utc_too(self):
        """The CFP committee releases Tuesday evening Eastern = Wednesday UTC; Mon/Tue-only would pick each release up a week late."""
        y = (ROOT / ".github" / "workflows" / "cfb-refresh.yml").read_text()
        self.assertIn('"$DOW" = "3"', y)

    def test_app_reads_the_poll_label_and_defaults_to_ap_for_old_files(self):
        s = (ROOT / "docs" / "app.html").read_text()
        self.assertIn("rankingsPoll:power?.rankingsPoll||'AP'", s)
        self.assertEqual(s, (ROOT / "docs" / "index.html").read_text())                 # index.html mirrors app.html byte for byte


if __name__ == "__main__":
    unittest.main()
