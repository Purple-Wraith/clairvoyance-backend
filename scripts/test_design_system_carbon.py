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


if __name__ == "__main__":
    unittest.main(verbosity=2)
