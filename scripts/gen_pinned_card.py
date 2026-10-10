"""
gen_pinned_card.py -- Clairvoyance pinned / "what we cover" card (1080x1080).

Repo-tracked rebuild of the Tron-Disc-only gen_pinned_card_v2.py (design spec: memory note
project_pinned_card_design_spec).  The layout, fonts, glow presets, dot-separator technique and
footer are the locked design; ONLY the copy is data-driven -- every league / pricing / props
string comes from ad_coverage.py, the single source of truth that scripts/test_ad_coverage.py
pins to the real engine (auto_lock_settle.PRODUCT_SPORTS, _subscribers.PRICING_BY_COUNT).

Outputs (CARDS below; each renders the SAME content on one of the two design_system backgrounds):
  scripts/assets/covers_card.png      void-black bg  -- the "What We Cover" rotation card that
                                      generate_social_cards.py attaches every 5th day.
  <out-dir>/CorrectPinnedCard2.0.png  void-black bg  (profile-pin copy)
  <out-dir>/CorrectPinnedCard6.0.png  covers_card bg (profile-pin copy)

Usage:
  python3 scripts/gen_pinned_card.py                      # covers_card.png in the repo only
  python3 scripts/gen_pinned_card.py --out-dir ~/Desktop  # + CorrectPinnedCard2.0 / 6.0
"""
from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image, ImageDraw
from PIL.PngImagePlugin import PngInfo

import ad_coverage as ac
from design_system import (
    W, H, CX, MAG, CYN, WHT, orb, mono, glow, make_background, make_background_covers, draw_footer, hline,
    TITLE_GLOW, SUB_GLOW, HDR_GLOW,
)

ROOT = Path(__file__).resolve().parent.parent
COVERS_CARD = ROOT / "scripts" / "assets" / "covers_card.png"
# The pinned card carries its own GOLD (255,210,0); design_system's is (255,213,0).  Kept on purpose --
# it is the already-approved rendered colour (see design-system memo), not something to "fix" silently.
GOLD = (255, 210, 0)

DOT_W = 20          # gap between row items; a drawn circle (r=1), never the "·" glyph (Orbitron/SF render it as a box)
HDR_Y0 = 371        # centre of the first section header
ROW_DY = 40         # header centre -> first row baseline
ROW_STEP = 28       # row -> row
SEC_GAP = 55        # last row baseline -> next header centre

SECTIONS_AFTER_LEAGUES = (
    ("EVERY PICK GRADED", MAG, [("Advanced Analytics", "Confidence Scores", "Market Edge")]),
    ("ADAPTIVE ENGINE", MAG, [("Recalibration", "Simulations", "Cutting Edge Statistical Analysis")]),
    ("HOW IT WORKS", MAG, [("Deep Analysis", "Real-Time Signals", "Precision Grading")]),
    ("FOLLOW FOR DAILY SIGNALS", MAG, [("Performance Recaps", "Free Pick of The Day")]),
    ("SUBSCRIPTIONS AVAILABLE", GOLD, [ac.PRICING_LINE_PARTS]),
)


def dotted_row(draw: ImageDraw.ImageDraw, parts, baseline: int, font, color=WHT, dot_color=MAG, dot_w: int = DOT_W) -> None:
    total = sum(font.getlength(p) for p in parts) + dot_w * (len(parts) - 1)
    x = CX - total / 2
    for i, p in enumerate(parts):
        draw.text((x, baseline), p, font=font, fill=(*color, 255), anchor="ls")
        x += font.getlength(p)
        if i < len(parts) - 1:
            dx = x + dot_w // 2
            dy = baseline - 7
            draw.ellipse([dx - 1, dy - 1, dx + 1, dy + 1], fill=(*dot_color, 230))
            x += dot_w


def sections(league_rows=ac.LEAGUE_ROWS):
    return (("LEAGUES COVERED", MAG, [tuple(r) for r in league_rows]),) + SECTIONS_AFTER_LEAGUES


def build_card(background_fn, body_lines=ac.BODY_LINES, league_rows=ac.LEAGUE_ROWS, title_size=88, sub_size=26) -> Image.Image:
    base = background_fn(W, H)
    base = glow(base, "CLAIRVOYANCE", (CX, 100), orb(title_size), MAG, TITLE_GLOW)
    base = glow(base, ac.TAGLINE, (CX, 166), orb(sub_size), CYN, SUB_GLOW)

    draw = ImageDraw.Draw(base)
    bf = mono(19)
    by = 249
    for ln in body_lines:
        draw.text((CX, by), ln, font=bf, fill=(*WHT, 255), anchor="ms")
        by += ROW_STEP

    hy = HDR_Y0
    for header, color, rows in sections(league_rows):
        base = glow(base, header, (CX, hy - 1), orb(22), color, HDR_GLOW)
        draw = ImageDraw.Draw(base)
        baseline = hy + ROW_DY
        for parts in rows:
            dotted_row(draw, parts, baseline, bf)
            baseline += ROW_STEP
        hy = baseline - ROW_STEP + SEC_GAP

    return draw_footer(base)


CARDS = (
    (make_background, COVERS_CARD, "covers_card.png"),
    (make_background, None, "CorrectPinnedCard2.0.png"),
    (make_background_covers, None, "CorrectPinnedCard6.0.png"),
)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-dir", help="also write CorrectPinnedCard2.0.png / 6.0.png here")
    args = ap.parse_args()
    for background_fn, fixed, name in CARDS:
        if fixed is None:
            if not args.out_dir:
                continue
            out = Path(args.out_dir).expanduser() / name
        else:
            out = fixed
        out.parent.mkdir(parents=True, exist_ok=True)
        meta = PngInfo()
        meta.add_text(ac.FP_KEY, ac.pinned_card_fingerprint())     # lets test_ad_coverage.py detect a stale committed PNG
        build_card(background_fn).convert("RGB").save(out, optimize=True, pnginfo=meta)
        print(f"Saved -> {out}")


if __name__ == "__main__":
    main()
