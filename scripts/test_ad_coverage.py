#!/usr/bin/env python3
"""Every advertisement / promo asset must show CURRENT league coverage, pricing and props wording.

Pins scripts/ad_coverage.py (the single source of truth for ad copy) to the real engine
(auto_lock_settle.PRODUCT_SPORTS, _subscribers.PRICING_BY_COUNT), then checks that every ad generator, template and caption
builder uses it / contains no retired league or tier.  Image-level checks (rendered PNGs equal what the generators produce)
run only where Pillow + the brand fonts are installed (the Mac); in CI they SKIP and the text checks still run.

    python3 scripts/test_ad_coverage.py
"""
from __future__ import annotations

import ast
import json
import re
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))

import ad_coverage as ac  # noqa: E402
import auto_lock_settle as als  # noqa: E402
import _subscribers as subs  # noqa: E402

try:
    from PIL import Image, ImageChops
    import os
    HAVE_IMG = (Path(os.path.expanduser("~/Library/Fonts/Orbitron-wght900.ttf")).exists()
                and Path(os.path.expanduser("~/Library/Fonts/Orbitron-Bold.ttf")).exists()
                and Path("/System/Library/Fonts/SFNSMono.ttf").exists())
except Exception:  # pragma: no cover - Pillow missing in CI
    HAVE_IMG = False

# Generators / templates that carry advertising copy.
AD_PY = ["gen_pinned_card.py", "gen_reddit_ad_v1.py", "gen_subscription_card.py", "refresh_pinned_card_images.py"]
AD_TEMPLATES = ["video_templates/subscription_tiers_reveal.html"]
LANDING = Path.home() / "clairvoyance-landing" / "docs" / "index.html"


def _retired_regex() -> re.Pattern:
    parts = []
    for t in ac.RETIRED_TERMS:
        parts.append(r"(?<![A-Za-z0-9])" + re.escape(t) + r"(?![A-Za-z0-9])")
    return re.compile("|".join(parts), re.I)


RETIRED = _retired_regex()


def _code_strings(path: Path) -> list[str]:
    """String literals of a .py file excluding docstrings (docstrings may legitimately explain what was retired)."""
    tree = ast.parse(path.read_text())
    doc_nodes = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.ClassDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant):
                doc_nodes.add(id(body[0].value))
    return [n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in doc_nodes]


def _html_text(path: Path) -> str:
    s = path.read_text()
    s = re.sub(r"<!--.*?-->", "", s, flags=re.S)
    s = re.sub(r"/\*.*?\*/", "", s, flags=re.S)
    s = re.sub(r"(?m)^\s*//.*$", "", s)
    return s.replace("&middot;", "·").replace("&mdash;", "—").replace("&nbsp;", " ")


class SourceOfTruthMatchesEngine(unittest.TestCase):
    def test_leagues_equal_what_is_actually_sold(self):
        self.assertEqual(set(ac.LEAGUES_BY_PRODUCT), set(subs.PRODUCTS))
        for product, names in ac.LEAGUES_BY_PRODUCT.items():
            tags = {ac.LEAGUE_TAGS[n] for n in names}
            self.assertEqual(tags, set(als.PRODUCT_SPORTS[product]), f"ad_coverage {product} drifted from PRODUCT_SPORTS")
            self.assertEqual(len(tags), len(names), "duplicate league in a product")

    def test_twelve_leagues_four_sports(self):
        self.assertEqual(ac.LEAGUE_COUNT, 12)
        self.assertEqual(ac.LEAGUE_COUNT, sum(len(v) for v in als.PRODUCT_SPORTS.values()))
        self.assertEqual(ac.coverage_blurb(), "12 leagues across football, basketball, hockey and soccer")

    def test_pricing_equals_backend_ladder(self):
        self.assertEqual(ac.PRICING_BY_COUNT, subs.PRICING_BY_COUNT)
        self.assertEqual(ac.PRODUCT_COUNT, len(subs.PRODUCTS))
        self.assertEqual(subs.EXPIRY_DAYS, ac.ACCESS_DAYS)

    def test_league_rows_cover_every_league_exactly_once(self):
        for rows in (ac.LEAGUE_ROWS, ac.LEAGUE_ROWS_WIDE):
            flat = [n for r in rows for n in r]
            self.assertEqual(sorted(flat), sorted(ac.ALL_LEAGUES))

    def test_no_props_wording_while_props_are_paused(self):
        """Owner 2026-10-27: NBA player props are out of every ad until ~20 NBA games are settled."""
        body = " ".join(ac.BODY_LINES)
        for text in (body, ac.CORE_COPY, ac.INCLUDES_LINE):
            self.assertNotRegex(text, r"(?i)props?\b")
        self.assertNotRegex(body, r"(?i)across (every|select) sports?")
        self.assertIn("graded picks", body)
        self.assertIn("game lines", body)

    def test_ad_coverage_strings_have_no_retired_terms(self):
        for name in dir(ac):
            if name.startswith("_") or name == "RETIRED_TERMS":
                continue
            val = getattr(ac, name)
            flat = []

            def walk(v):
                if isinstance(v, str):
                    flat.append(v)
                elif isinstance(v, dict):
                    for k, x in v.items():
                        walk(k), walk(x)
                elif isinstance(v, (list, tuple, set, frozenset)):
                    for x in v:
                        walk(x)
            walk(val)
            for s in flat:
                self.assertIsNone(RETIRED.search(s), f"retired term in ad_coverage.{name}: {s!r}")


class GeneratorsCarryNoRetiredCopy(unittest.TestCase):
    def test_python_generators(self):
        for f in AD_PY:
            for s in _code_strings(HERE / f):
                self.assertIsNone(RETIRED.search(s), f"{f}: retired term in string {s!r}")

    def test_generators_take_leagues_from_the_source_of_truth(self):
        for f in ("gen_pinned_card.py", "gen_reddit_ad_v1.py", "gen_subscription_card.py", "refresh_pinned_card_images.py"):
            src = (HERE / f).read_text()
            self.assertIn("import ad_coverage as ac", src, f)
            lits = [s for s in _code_strings(HERE / f) if s in ac.ALL_LEAGUES and s not in ("NFL", "CFB", "NBA")]
            self.assertEqual(lits, [], f"{f} hard-codes league names {lits} -- use ad_coverage")

    def test_subscription_video_template(self):
        html = _html_text(HERE / "video_templates" / "subscription_tiers_reveal.html")
        self.assertIsNone(RETIRED.search(html), RETIRED.search(html))
        for name in ac.ALL_LEAGUES:
            self.assertIn(name, html, f"{name} missing from subscription video")
        for n, price in ac.PRICING_BY_COUNT.items():
            self.assertIn(f"${price}<", html)
            self.assertIn(f"{n} SPORT", html)
        self.assertEqual(html.count("/ 30 days"), len(ac.PRICING_BY_COUNT))
        self.assertNotIn("props", html.lower())
        for stale in ("BASE", "PLUS", "DAY PASS", "WEEKEND PASS", "/MO"):
            self.assertNotIn(stale, html)
        # no leftover references to the removed pass boxes
        self.assertNotIn("class=\"pass", html)

    def test_caption_builders(self):
        import generate_social_cards as g
        for fn in (g.build_subscription_caption, g.build_covers_caption):
            for k, text in fn().items():
                self.assertIsNone(RETIRED.search(text), f"{fn.__name__}[{k}]: {RETIRED.search(text)}")
        cov = g.build_covers_caption()
        self.assertIn("12 leagues across football, basketball, hockey and soccer", cov["instagram"])
        self.assertIn("12 leagues", cov["x"])
        sub = g.build_subscription_caption()
        self.assertIn("$20", sub["instagram"])
        self.assertIn("$75", sub["x"])
        self.assertNotIn("props", sub["instagram"].lower())

    def test_breakdown_demo_rows_are_not_baseball(self):
        src = (HERE / "generate_video_reveal.py").read_text()
        self.assertNotIn("BASEBALL", src)


@unittest.skipUnless(LANDING.exists(), "landing repo not checked out (CI)")
class LandingAgrees(unittest.TestCase):
    """The landing page is the canonical public ad copy; the cards must say the same thing."""

    def test_landing_coverage_and_prices(self):
        html = LANDING.read_text()
        m = re.search(r"(\d+) leagues across (\d+) sports", html)
        self.assertIsNotNone(m)
        self.assertEqual((int(m.group(1)), int(m.group(2))), (ac.LEAGUE_COUNT, len(ac.SPORT_GROUPS)))
        for name in ac.ALL_LEAGUES:
            token = {"Premier League": "PREMIER", "Champions League": "CHAMPIONS"}.get(name, name)
            self.assertRegex(html, rf'(?i)sport-abbr[^>]*>{re.escape(token)}<', f"{name} not on landing")
        for price in ac.PRICING_BY_COUNT.values():
            self.assertIn(f'price-amount">${price}<', html)
        self.assertNotIn("NBA player props", html)       # owner 2026-10-25: player props are off the landing page until ~20 NBA games are settled


class CommittedImagesAreCurrent(unittest.TestCase):
    """Committed ad PNGs carry a fingerprint of the copy they were rendered from (PNG tEXt, stdlib-readable), so a league /
    price / props change in ad_coverage.py that is not followed by regenerating the images fails here -- in CI too."""

    def test_covers_card(self):
        p = ROOT / "scripts" / "assets" / "covers_card.png"
        self.assertEqual(ac.read_png_text(p).get(ac.FP_KEY), ac.pinned_card_fingerprint(),
                         "covers_card.png is stale vs ad_coverage.py -- run: python3 scripts/gen_pinned_card.py")

    def test_docs_pinned_cards(self):
        for name in ("carbon", "purplecarbon", "whitecarbon", "plaid"):
            p = ROOT / "docs" / f"pinned_card_{name}.png"
            self.assertEqual(ac.read_png_text(p).get(ac.FP_KEY), ac.pinned_docs_fingerprint(),
                             f"{p.name} is stale vs ad_coverage.py -- run: python3 scripts/refresh_pinned_card_images.py")

    def test_fingerprint_changes_when_copy_changes(self):
        base = ac.pinned_card_fingerprint()
        saved = ac.LEAGUE_ROWS
        try:
            ac.LEAGUE_ROWS = (("NFL", "CFB", "NBA", "NHL", "MLS"),)
            self.assertNotEqual(base, ac.pinned_card_fingerprint())
        finally:
            ac.LEAGUE_ROWS = saved
        self.assertEqual(base, ac.pinned_card_fingerprint())


class HeroStat(unittest.TestCase):
    def test_reddit_hero_stat_is_the_live_all_time_rate(self):
        if not HAVE_IMG:
            self.skipTest("Pillow + brand fonts not available here (CI)")
        import gen_reddit_ad_v1 as ad
        d = json.loads((ROOT / "docs" / "engine_performance_subscriber.json").read_text())
        allt = next(p for p in d["periods"] if p["key"] == "ALL_TIME")
        self.assertEqual(ad.hero_win_rate_pct(), f"{allt['pct'] * 100:.1f}%")
        # same definition the landing page's Live Track Record uses (wins / all settled picks, pushes included)
        self.assertEqual(ad.hero_win_rate_pct(), f"{allt['w'] / allt['n'] * 100:.1f}%")


@unittest.skipUnless(HAVE_IMG, "Pillow + brand fonts not available here (CI)")
class RenderedAssets(unittest.TestCase):
    def test_pinned_docs_refresh_only_touches_the_coverage_block(self):
        import refresh_pinned_card_images as r
        for name in r.CARDS:
            cur = Image.open(r.DOCS / f"{name}.png").convert("RGB")
            again = r.refresh(cur, name.replace("pinned_card_", ""))
            x0, y0, x1, y1 = r.REGION
            for box in ((0, 0, 1080, y0), (0, y1, 1080, 1080)):
                self.assertIsNone(ImageChops.difference(cur.crop(box), again.crop(box)).getbbox(), f"{name} {box}")

    def test_cards_render(self):
        import gen_pinned_card as gp
        import gen_reddit_ad_v1 as ad
        import gen_subscription_card as gs
        from design_system import make_background, make_background_covers
        for bg in (make_background, make_background_covers):
            for mod in (gp, ad, gs):
                self.assertEqual(mod.build_card(bg).size, (1080, 1080))


if __name__ == "__main__":
    unittest.main(verbosity=1)
