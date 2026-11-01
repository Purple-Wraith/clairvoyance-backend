#!/usr/bin/env python3
"""The record the ads quote (ad_perf.py) is the record the site publishes: leagues add up to the overall tiles, every covered league (and nothing
else) is present, win % is the landing page's wins / all-graded definition, and the Reddit ad + league scorecard render it.

    python3 scripts/test_ad_perf.py
"""
from __future__ import annotations

import json, os, sys, tempfile, unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = Path(os.environ.get("CV_ROOT") or HERE.parent)
sys.path.insert(0, str(HERE))
import ad_coverage as ac  # noqa: E402
import ad_perf as ap  # noqa: E402

try:
    import PIL  # noqa: F401
    from design_system import ORB_PATH, SYS_PATH
    HAVE_IMG = os.path.exists(ORB_PATH) and os.path.exists(SYS_PATH)
except Exception:
    HAVE_IMG = False


class Data(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.d = ap.load()

    def test_every_covered_league_and_only_those(self):
        self.assertEqual(sorted(self.d["leagues"]), sorted(ac.ALL_LEAGUES))
        for name, v in self.d["leagues"].items():
            self.assertIn("month", v, name); self.assertIn("all", v, name)

    def test_scorecard_groups_leagues_under_their_sport(self):
        rows = ap.sport_rows(self.d)
        groups, cur = {}, None
        for r in rows:
            if r["kind"] == "bundle":
                cur = groups.setdefault(r["name"], [])
            else:
                cur.append(r["name"])
        self.assertEqual({k: sorted(v) for k, v in groups.items()},
                         {"FOOTBALL": ["CFB", "NFL"], "BASKETBALL": ["NBA"], "HOCKEY": sorted(ac.LEAGUES_BY_PRODUCT["hockey"]), "SOCCER": sorted(ac.LEAGUES_BY_PRODUCT["soccer"])})
        heads = [r for r in rows if r["kind"] == "bundle"]
        self.assertLessEqual(abs(sum(h["month"]["w"] for h in heads) - self.d["overall"]["month"]["w"]), self.SETTLE_SKEW)
        self.assertLessEqual(abs(sum(h["all"]["n"] for h in heads) - self.d["overall"]["all"]["n"]), self.SETTLE_SKEW)   # see SETTLE_SKEW

    # docs/sport_performance.json and docs/engine_performance_subscriber.json are two files written a moment apart by the landing-perf refresh, and a pick that settles between the two
    # writes shows up in one and not the other (seen 2 times in 12 consecutive refreshes on 2026-11-01, always 1-2 picks, gone by the next refresh).  A real bug -- a league missing or counted
    # twice -- is hundreds of picks, so the totals must agree to within this many.
    SETTLE_SKEW = 5

    def test_leagues_add_up_to_the_overall_tiles(self):
        for key in ("month", "all"):
            tot = ap._sum([v[key] for v in self.d["leagues"].values()])
            o = self.d["overall"][key]
            for f in ("w", "l", "n"):
                self.assertLessEqual(abs(tot[f] - o[f]), self.SETTLE_SKEW, (key, f, tot[f], o[f]))

    def test_pct_is_the_landing_pages_definition(self):
        eng = json.loads((ROOT / "docs" / "engine_performance_subscriber.json").read_text())
        per = {p["key"]: p for p in eng["periods"]}
        self.assertAlmostEqual(self.d["overall"]["month"]["pct"], per["THIS_MONTH"]["pct"], places=9)
        self.assertAlmostEqual(self.d["overall"]["all"]["pct"], per["ALL_TIME"]["pct"], places=9)
        sp = json.loads((ROOT / "docs" / "sport_performance.json").read_text())
        for row in sp["allTime"]["leagues"]:
            name = ap._LABEL_TO_NAME[row["lbl"]]
            self.assertAlmostEqual(self.d["leagues"][name]["all"]["pct"], row["pct"], places=9, msg=name)

    def test_rows_group_bundles_and_sort_by_month_win_pct(self):
        rows = ap.block_rows(self.d)
        kinds = [r["kind"] for r in rows]
        self.assertEqual(kinds.count("bundle"), 2)                              # Hockey, Soccer
        self.assertEqual(sum(k == "league" for k in kinds), 9)                  # 5 + 4 leagues inside the bundles
        self.assertEqual(sum(k == "single" for k in kinds), 3)                  # NFL, CFB, NBA
        names = [r["name"] for r in rows]
        self.assertEqual(sorted(n for n in names if n not in ("HOCKEY", "SOCCER")), sorted(ac.ALL_LEAGUES))
        for i, r in enumerate(rows):                                            # members follow their bundle, best month win % first
            if r["kind"] == "bundle":
                members = []
                for m in rows[i + 1:]:
                    if m["kind"] != "league":
                        break
                    members.append(m)
                pcts = [m["month"]["pct"] or 0 for m in members]                # a league with no pick yet this month has pct None (0-0): sorts as 0
                self.assertEqual(pcts, sorted(pcts, reverse=True))
                self.assertEqual(r["month"]["w"], sum(m["month"]["w"] for m in members))
        tops = [r["month"]["pct"] or 0 for r in rows if r["kind"] in ("single", "bundle")]
        self.assertEqual(tops, sorted(tops, reverse=True))

    def test_first_of_the_month_every_league_is_a_zero_record_not_a_missing_key(self):
        """On the 1st nobody has a pick this month: docs/sport_performance.json's thisMonth list is empty (or holds only the leagues that have played). Every covered league must still carry
        a 'month' record (0-0, pct None) so the scorecards and the monthly ad refresh do not crash (2026-11-01: KeyError 'month')."""
        for month_rows in ([], [{"lbl": "EXTRALIGA", "w": 1, "l": 0, "n": 1, "pct": 1.0, "units": .2}]):
            with tempfile.TemporaryDirectory() as td:
                sp = json.loads((ROOT / "docs" / "sport_performance.json").read_text())
                sp["thisMonth"] = {"label": "NOV 2026", "leagues": month_rows, "excluded_late": 0}
                f = Path(td) / "sp.json"; f.write_text(json.dumps(sp))
                d = ap.load(f, ROOT / "docs" / "engine_performance_subscriber.json")
            self.assertEqual(sorted(d["leagues"]), sorted(ac.ALL_LEAGUES))
            for name, v in d["leagues"].items():
                self.assertIn("month", v, name)
                self.assertIn("all", v, name)
            quiet = [n for n in ac.ALL_LEAGUES if n != ("Extraliga" if month_rows else None)]
            for n in quiet:
                self.assertEqual(d["leagues"][n]["month"], {"w": 0, "l": 0, "p": 0, "n": 0, "pct": None}, n)
            if month_rows:
                self.assertEqual(d["leagues"]["Extraliga"]["month"]["w"], 1)
            rows_b, rows_s = ap.block_rows(d), ap.sport_rows(d)                      # neither display layout may crash on 0-0 months
            self.assertEqual(sorted(r["name"] for r in rows_s if r["kind"] == "league"), sorted(ac.ALL_LEAGUES))
            self.assertEqual(sum(r["kind"] in ("single", "league") for r in rows_b), len(ac.ALL_LEAGUES))

    def test_a_league_outside_the_products_is_refused(self):
        with tempfile.TemporaryDirectory() as td:
            sp = json.loads((ROOT / "docs" / "sport_performance.json").read_text())
            sp["thisMonth"]["leagues"].append({"lbl": "MLS", "w": 1, "l": 0, "n": 1, "pct": 1.0, "units": 1})
            f = Path(td) / "sp.json"; f.write_text(json.dumps(sp))
            with self.assertRaises(ValueError):
                ap.load(f)

    def test_record_format_shows_pushes_only_when_there_are_some(self):
        self.assertEqual(ap.fmt_rec(ap._rec(78, 25)), "78-25")
        self.assertEqual(ap.fmt_rec(ap._rec(200, 73, 274)), "200-73-1")
        self.assertEqual(ap.fmt_pct(ap._rec(200, 73, 274)), "73.0%")


@unittest.skipUnless(HAVE_IMG, "Pillow + brand fonts not available here (CI)")
class Render(unittest.TestCase):
    def test_both_cards_render_on_both_backgrounds(self):
        import gen_league_scorecard as sc
        import gen_reddit_ad_v1 as ad
        from design_system import make_background, make_background_covers
        for bg in (make_background, make_background_covers):
            for mod in (sc, ad):
                img = mod.build_card(bg)
                self.assertEqual(img.size, (1080, 1080))


if __name__ == "__main__":
    unittest.main()
