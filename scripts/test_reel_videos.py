#!/usr/bin/env python3
"""Vertical reels (gen_reel_videos.py): 1080x1920 pages built from the SAME live numbers as the square videos, laid out inside the Reels/TikTok safe zone.

    python3 scripts/test_reel_videos.py
Geometry tests need a browser (Playwright's Chromium, or installed Chrome) and skip when neither can launch.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import ad_perf  # noqa: E402
import gen_reddit_ad_videos as sq  # noqa: E402
import gen_reel_videos as g  # noqa: E402


class Pages(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = ad_perf.load()
        cls.pages = g.build_pages(cls.data)

    def test_two_reels_named_for_the_two_cards(self):
        self.assertEqual(sorted(self.pages), ["ClairvoyanceLeagueScorecard_reel", "ClairvoyanceRedditAd_reel"])

    def test_pages_are_phone_sized(self):
        for name, h in self.pages.items():
            self.assertIn(f'width="{g.W}" height="{g.H}"', h, name)
            self.assertIn(f"height:{g.H}px", h, name)
        self.assertEqual((g.W, g.H), (1080, 1920))

    def test_hero_reel_quotes_exactly_the_square_videos_numbers(self):
        square = sq.build_pages(self.data)["ClairvoyanceRedditAd_carbon"]
        reel = self.pages["ClairvoyanceRedditAd_reel"]
        for key in ("month", "all"):
            rec = self.data["overall"][key]
            for needle in (ad_perf.fmt_pct(rec), f'{rec["w"]:,}W - {rec["l"]:,}L'):
                self.assertIn(needle, square)
                self.assertIn(needle, reel)

    def test_scorecard_reel_has_every_row_of_the_square_scorecard_and_the_totals(self):
        reel = self.pages["ClairvoyanceLeagueScorecard_reel"]
        for r in ad_perf.sport_rows(self.data):
            self.assertIn(r["name"], reel)
            for key in ("month", "all"):
                self.assertIn(ad_perf.fmt_rec(r[key]), reel)
                self.assertIn(ad_perf.fmt_pct(r[key]), reel)
        for key in ("month", "all"):
            self.assertIn(ad_perf.fmt_rec(self.data["overall"][key]), reel)

    def test_no_nba_props_wording_and_the_disclaimer_is_kept(self):
        for name, h in self.pages.items():
            self.assertNotIn("props", h.lower(), name)
            self.assertIn("not financial advice", h, name)

    def test_durations_cover_the_last_reveal(self):
        self.assertGreater(g.scorecard_duration(self.data), 3.0 + g.STEP * len(ad_perf.sport_rows(self.data)) + g.FADE)
        self.assertGreaterEqual(g.ad_duration(), 6.8 + 1.0)                          # the URL is the last thing to appear (6.8 s)


class SafeZone(unittest.TestCase):
    """Everything visible at the end of the animation sits inside the zone the platforms leave clear."""
    def check(self, name):
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            try:
                b = pw.chromium.launch()
            except Exception:
                try:
                    b = pw.chromium.launch(channel="chrome")
                except Exception:
                    self.skipTest("no browser available")
            tmp = HERE / f".reel_test_{name}.html"
            try:
                tmp.write_text(g.build_pages(ad_perf.load())[name])
                page = b.new_context(viewport={"width": g.W, "height": g.H}).new_page()
                page.goto(f"file://{tmp}")
                page.wait_for_timeout(1200)
                page.evaluate("document.getAnimations().forEach(a => { a.pause(); a.currentTime = 60000; })")
                return page.evaluate("""() => [...document.querySelectorAll('.stage *')].filter(e => (e.textContent || '').trim() && e.children.length === 0 && getComputedStyle(e).opacity > 0.05).map(e => {
                    const r = e.getBoundingClientRect(); return {t: e.textContent.slice(0, 40), l: r.left, r: r.right, top: r.top, b: r.bottom}; })""")
            finally:
                b.close()
                tmp.unlink(missing_ok=True)

    def assert_inside(self, boxes):
        self.assertGreater(len(boxes), 10)
        for x in boxes:
            self.assertGreaterEqual(x["l"], g.SAFE_LEFT, x)
            self.assertLessEqual(x["r"], g.SAFE_RIGHT, x)
            self.assertGreaterEqual(x["top"], g.SAFE_TOP - 30, x)            # a line box is taller than its glyphs
            self.assertLessEqual(x["b"], g.SAFE_BOTTOM, x)

    def test_hero_reel(self):
        self.assert_inside(self.check("ClairvoyanceRedditAd_reel"))

    def test_scorecard_reel(self):
        self.assert_inside(self.check("ClairvoyanceLeagueScorecard_reel"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
