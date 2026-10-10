"""
gen_subscription_card.py -- Clairvoyance subscription / pricing card (1080x1080), repo-tracked.

Rebuild of the Tron-Disc-only gen_subscription_card_v2.py (its league list still said Bundesliga + MLS and
it could not be edited from this repo).  Same brand system as the pinned card (design_system.py backgrounds,
glow presets, footer); ALL copy -- leagues, product bundles, prices, what a subscription includes -- comes from
ad_coverage.py, which scripts/test_ad_coverage.py pins to the real engine / pricing.

Structure: LEAGUES COVERED (3 rows) -> PRICING header + "mix any combination" caption -> five tier rows
(1-5 sports, $20/$30/$45/$60/$75 per 30 days; the 5-sport row is the highlighted ALL ACCESS row) -> what's included.

Outputs (same content on both design_system backgrounds):
  <out-dir>/Clairvoyance_Subscription_Card_Black.png   void bg
  <out-dir>/Clairvoyance_Subscription_Card_Grey.png    covers bg
Usage:  python3 scripts/gen_subscription_card.py --out-dir ~/Desktop
"""
from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

import ad_coverage as ac
from design_system import (
    W, H, CX, MAG, CYN, WHT, T2, CARD, orb, mono, glow, make_background, make_background_covers, draw_footer,
    TITLE_GLOW, SUB_GLOW, HDR_GLOW, SOCIAL_GLOW,
)
from gen_pinned_card import GOLD, dotted_row

PRODUCT_NAMES = {"nfl": "NFL", "cfb": "CFB", "nba": "NBA", "soccer": "Soccer", "hockey": "Hockey"}
TIER_COLORS = [CYN, CYN, MAG, MAG, GOLD]


def rounded_box(base, box, color, fill_alpha=150, glow_a=70):
    glow_layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow_layer)
    gd.rounded_rectangle(box, radius=12, outline=(*color, glow_a), width=4)
    glow_layer = glow_layer.filter(ImageFilter.GaussianBlur(5))
    base = Image.alpha_composite(base, glow_layer)
    ImageDraw.Draw(base).rounded_rectangle(box, radius=12, fill=(*CARD, fill_alpha), outline=(*color, 255), width=2)
    return base


def build_card(background_fn) -> Image.Image:
    base = background_fn(W, H)
    base = glow(base, "CLAIRVOYANCE", (CX, 78), orb(72), MAG, TITLE_GLOW)
    base = glow(base, "SUBSCRIPTIONS", (CX, 138), orb(26), CYN, SUB_GLOW)

    # LEAGUES COVERED
    base = glow(base, "LEAGUES COVERED", (CX, 196), orb(20), MAG, HDR_GLOW)
    draw = ImageDraw.Draw(base)
    cf = mono(19)
    y = 232
    for row in ac.LEAGUE_ROWS:
        dotted_row(draw, list(row), y, cf, dot_w=30)
        y += 28

    # PRICING
    base = glow(base, "PRICING — PER SPORT PRODUCT", (CX, 346), orb(20), MAG, HDR_GLOW)
    draw = ImageDraw.Draw(base)
    cap = " · ".join(PRODUCT_NAMES[p] for p in ("nfl", "cfb", "nba", "soccer", "hockey")) + " — mix any combination"
    draw.text((CX, 376), cap, font=mono(17), fill=(*WHT, 255), anchor="ms")

    ty, th, gap = 392, 80, 12
    bx0, bx1 = 120, W - 120
    for n, price in ac.PRICING_BY_COUNT.items():
        color = TIER_COLORS[n - 1]
        box = [bx0, ty, bx1, ty + th]
        base = rounded_box(base, box, color, fill_alpha=215 if n == ac.PRODUCT_COUNT else 150, glow_a=110 if n == ac.PRODUCT_COUNT else 60)
        label = f"{n} SPORT" if n == 1 else (f"{n} SPORTS — ALL ACCESS" if n == ac.PRODUCT_COUNT else f"{n} SPORTS")
        base = glow(base, label, (bx0 + 28, ty + th // 2), orb(22), color, SOCIAL_GLOW, anchor="lm")
        base = glow(base, f"${price}", (bx1 - 28 - 160, ty + th // 2), orb(32), color, SOCIAL_GLOW, anchor="rm")
        draw = ImageDraw.Draw(base)
        draw.text((bx1 - 28, ty + th // 2 + 2), f"/ {ac.ACCESS_DAYS} days", font=mono(17), fill=(*WHT, 255), anchor="rm")
        ty += th + gap

    # WHAT'S INCLUDED
    draw = ImageDraw.Draw(base)
    draw.text((CX, ty + 24), ac.INCLUDES_LINE, font=mono(16), fill=(*WHT, 255), anchor="ms")
    draw.text((CX, ty + 50),"Hockey = " + " + ".join(ac.LEAGUES_BY_PRODUCT["hockey"]) + "  ·  Soccer = "
              + ", ".join(ac.LEAGUES_BY_PRODUCT["soccer"]), font=mono(13), fill=(*T2, 255), anchor="ms")
    return draw_footer(base)


CARDS = (
    (make_background, "Clairvoyance_Subscription_Card_Black.png"),
    (make_background_covers, "Clairvoyance_Subscription_Card_Grey.png"),
)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-dir", required=True)
    args = ap.parse_args()
    out_dir = Path(args.out_dir).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    for bg, name in CARDS:
        out = out_dir / name
        build_card(bg).convert("RGB").save(out, optimize=True)
        print(f"Saved -> {out}")


if __name__ == "__main__":
    main()
