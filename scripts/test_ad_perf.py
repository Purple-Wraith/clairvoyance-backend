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

    def test_leagues_add_up_to_the_overall_tiles(self):
        for key in ("month", "all"):
            tot = ap._sum([v[key] for v in self.d["leagues"].values()])
            o = self.d["overall"][key]
            self.assertEqual((tot["w"], tot["l"], tot["n"]), (o["w"], o["l"], o["n"]), key)

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
                self.assertEqual([m["month"]["pct"] for m in members], sorted((m["month"]["pct"] for m in members), reverse=True))
                self.assertEqual(r["month"]["w"], sum(m["month"]["w"] for m in members))
        tops = [r["month"]["pct"] for r in rows if r["kind"] in ("single", "bundle")]
        self.assertEqual(tops, sorted(tops, reverse=True))

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
