#!/usr/bin/env python3
"""design_system.make_background_carbon(): the ad cards' copy of the app's carbon twill weave (docs/app.html `body`).  Pure PIL, no fonts/network.

    python3 scripts/test_design_system_carbon.py
"""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import design_system as D  # noqa: E402


class Carbon(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.img = D.make_background_carbon(200, 200).convert("RGB")

    def test_size_and_opaque(self):
        self.assertEqual(self.img.size, (200, 200))
        self.assertEqual(D.make_background_carbon(120, 80).size, (120, 80))

    def test_weave_repeats_every_20px(self):
        for x, y in ((2, 2), (12, 2), (7, 7), (2, 12)):
            a, b = self.img.getpixel((60 + x, 60 + y)), self.img.getpixel((100 + x, 100 + y))
            self.assertLessEqual(max(abs(i - j) for i, j in zip(a, b)), 3, (x, y))     # same tile cell two repeats apart; only the soft glow gradient differs

    def test_light_and_dark_cells_and_gap(self):
        base = D.make_background_carbon(200, 200).convert("RGB")
        light = base.getpixel((122, 102))      # tile cell (0,0) of a repeat is LIGHT
        dark = base.getpixel((112, 102))       # tile cell (10,0) is DARK
        self.assertGreater(sum(light), sum(dark))

    def test_matches_the_apps_css_values(self):
        html = (HERE.parent / "docs" / "app.html").read_text(encoding="utf-8")
        body = next(l for l in html.splitlines() if l.startswith("body{font-family"))
        fills = re.findall(r"fill='%23([0-9A-Fa-f]{6})'|fill='#([0-9A-Fa-f]{6})'", body)
        got = [(a or b).lower() for a, b in fills]
        hexes = lambda c: "%02x%02x%02x" % c
        self.assertEqual(got, [hexes(D.CARBON_LIGHT), hexes(D.CARBON_DARK)])
        self.assertIn("#121212", body)
        self.assertEqual(hexes(D.CARBON_BASE), "121212")

    def test_page_glows_lift_the_top_left_corner_most(self):
        im = D.make_background_carbon(1080, 1080).convert("L")
        mean = lambda box: sum(im.crop(box).getdata()) / ((box[2] - box[0]) * (box[3] - box[1]))
        self.assertGreater(mean((100, 20, 260, 80)), mean((500, 500, 660, 560)))             # white glow top-left vs the middle
        self.assertGreater(mean((900, 980, 1060, 1040)), mean((500, 500, 660, 560)))         # grey glow bottom-right vs the middle


class Footer(unittest.TestCase):
    def test_footer_is_the_film_archives_darker_lit_carbon(self):
        f = D.make_footer_carbon().convert("RGB")
        self.assertEqual(f.size, (D.W, D.FBAR_H))
        lit, shade = f.getpixel((5, 5)), f.getpixel((900, 40))
        self.assertGreater(sum(lit), sum(shade) + 20)                    # the 160deg white sheen lights the top-left corner only
        region = f.crop((700, 0, 1080, D.FBAR_H)).convert("L")
        self.assertLess(sum(region.get_flattened_data() if hasattr(region, "get_flattened_data") else region.getdata()) / (380 * D.FBAR_H), 18)    # darker than the page's weave (mean ~24)

    def test_draw_footer_carbon_changes_the_bar_not_the_flat_one(self):
        flat = D.draw_footer(D.make_background_carbon(), carbon=False).convert("RGB")
        dark = D.draw_footer(D.make_background_carbon(), carbon=True).convert("RGB")
        self.assertEqual(flat.getpixel((900, D.H - 30)), (8, 8, 10))
        self.assertNotEqual(flat.getpixel((900, D.H - 30)), dark.getpixel((900, D.H - 30)))


class EverywhereCarbon(unittest.TestCase):
    """2026-11-17: the daily social cards (app.html _cfBg) and every daily social video template draw the carbon weave, not the old #222233 crosshatch."""
    def test_app_card_canvas_is_carbon(self):
        html = (HERE.parent / "docs" / "app.html").read_text(encoding="utf-8")
        body = html[html.index("function _cfBg(ctx,W,H){"):html.index("function _hudCorners")]
        self.assertIn("#121212", body)
        self.assertIn("#1E1E1E", body)
        self.assertNotIn("#222233", body)
        self.assertEqual((HERE.parent / "docs" / "app.html").read_bytes(), (HERE.parent / "docs" / "index.html").read_bytes())

    def test_video_templates_are_carbon(self):
        files = [f for f in sorted((HERE / "video_templates").glob("*.html")) if f.name != "image_glitch_reveal.html"]
        self.assertGreaterEqual(len(files), 11)
        for f in files:
            src = f.read_text(encoding="utf-8")
            self.assertIn("#1E1E1E", src, f.name)
            self.assertNotIn("ctx.fillStyle='#222233'", src, f.name)


class CarbonOnlyOutputs(unittest.TestCase):
    """Owner 2026-11-17: send ONLY the carbon-background versions -- every ad/card generator renders carbon and nothing else, and the committed rotation card is carbon."""
    def test_every_card_generator_outputs_only_carbon(self):
        import gen_league_scorecard as sc, gen_pinned_card as pc, gen_reddit_ad_v1 as ad, gen_subscription_card as sub
        for mod in (sc, pc, ad, sub):
            fns = {c[0] for c in mod.CARDS}
            self.assertEqual(fns, {D.make_background_carbon}, mod.__name__)

    def test_ad_videos_are_carbon_only(self):
        import ad_perf
        import gen_reddit_ad_videos as v
        pages = v.build_pages(ad_perf.load())
        self.assertEqual(sorted(pages), ["ClairvoyanceLeagueScorecard_carbon", "ClairvoyanceRedditAd_carbon"])

    def test_committed_rotation_card_is_carbon(self):
        from PIL import Image
        im = Image.open(HERE / "assets" / "covers_card.png").convert("RGB")
        r, g, b = im.getpixel((10, 540))
        self.assertLess(max(r, g, b), 45)
        self.assertLessEqual(abs(r - b), 6)                              # the old covers background was blue-grey (34,34,51) with a teal mesh


if __name__ == "__main__":
    unittest.main(verbosity=2)
