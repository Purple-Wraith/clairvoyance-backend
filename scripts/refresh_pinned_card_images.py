"""
refresh_pinned_card_images.py -- bring docs/pinned_card_{carbon,purplecarbon,whitecarbon,plaid}.png up to date.

These four 1080x1080 "pinned card" PNGs (published at purple-wraith.github.io/clairvoyance-backend/pinned_card_*.png)
were produced by a one-off script that is not in the repo, and their "SPORTS COVERED" block still advertised
"MLB, NBA, NHL, TENNIS, F1, More Coming".  Rather than redesign them (the look is locked), this script repaints ONLY that
block in place:

  * every pinned-card background is an exactly periodic tile (carbon variants: 32px; plaid: 129px), so the block's
    background is rebuilt losslessly from a clean margin strip of the same image -- no inpainting guesswork;
  * the block is redrawn as LEAGUES COVERED + the current league list (ad_coverage.LEAGUE_ROWS_WIDE) followed by the
    existing EVERY PICK GRADED / ADAPTIVE ENGINE sections, in the same colours, with proper drawn dot separators
    (the old image showed font-fallback boxes where the separators should be);
  * everything outside the block (logo, title, divider rules, FOLLOW FOR DAILY SIGNALS, footer) is left byte-identical.

Idempotent: running it again on its own output gives identical pixels.
Usage:  python3 scripts/refresh_pinned_card_images.py            (rewrites docs/pinned_card_*.png)
        python3 scripts/refresh_pinned_card_images.py --out-dir /tmp/x   (write elsewhere, leave docs untouched)
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
from PIL.PngImagePlugin import PngInfo

import ad_coverage as ac

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
CARDS = ("pinned_card_carbon", "pinned_card_purplecarbon", "pinned_card_whitecarbon", "pinned_card_plaid")

W = 1080
REGION = (0, 606, W, 852)            # rows y=606..851: sits between the two divider rules (y~594 and y~860)
TILE_ORIGIN_Y = 200                  # clean margin strip (x<tile) used as the background tile source

# Per-variant look, sampled from the original pixels.
STYLE = {
    "carbon":       dict(tile=32,  hdr=(0, 240, 255), row=(105, 118, 155), font="orbitron"),
    "purplecarbon": dict(tile=32,  hdr=(0, 240, 255), row=(105, 118, 155), font="orbitron"),
    "whitecarbon":  dict(tile=32,  hdr=(0, 240, 255), row=(60, 55, 80),    font="orbitron"),
    "plaid":        dict(tile=129, hdr=(0, 210, 245), row=(30, 24, 44),    font="helvetica"),
}

# Vertical layout (text baselines), tighter than the old 78px pitch so the extra league row fits above the lower divider.
HDR1, ROW_STEP, HDR_TO_ROW, ROW_TO_HDR = 637, 27, 31, 40


def _orbitron(sz: int, weight: int = 400) -> ImageFont.FreeTypeFont:
    f = ImageFont.truetype(os.path.expanduser("~/Library/Fonts/Orbitron-Bold.ttf"), sz)   # variable font, wght 400-900
    f.set_variation_by_axes([weight])
    return f


def _helv(sz: int, bold: bool) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", sz, index=1 if bold else 0)


def _fonts(kind: str):
    if kind == "orbitron":
        return _orbitron(22), _orbitron(22), 0.5
    return _helv(20, True), _helv(21, False), 2.0


def _text_width(draw, text, font, tracking):
    return sum(font.getlength(ch) for ch in text) + tracking * (len(text) - 1)


def _draw_tracked(draw, cx, baseline, text, font, fill, tracking):
    x = cx - _text_width(draw, text, font, tracking) / 2
    for ch in text:
        draw.text((x, baseline), ch, font=font, fill=fill, anchor="ls")
        x += font.getlength(ch) + tracking


def _draw_row(draw, baseline, parts, font, fill, gap=30):
    total = sum(font.getlength(p) for p in parts) + gap * (len(parts) - 1)
    x = W / 2 - total / 2
    for i, p in enumerate(parts):
        draw.text((x, baseline), p, font=font, fill=fill, anchor="ls")
        x += font.getlength(p)
        if i < len(parts) - 1:
            dx, dy = x + gap / 2, baseline - 8
            draw.ellipse([dx - 2, dy - 2, dx + 2, dy + 2], fill=fill)
            x += gap


def refresh(im: Image.Image, variant: str) -> Image.Image:
    st = STYLE[variant]
    P = st["tile"]
    im = im.convert("RGB")
    out = im.copy()
    tile = im.crop((0, TILE_ORIGIN_Y, P, TILE_ORIGIN_Y + P))
    x0, y0, x1, y1 = REGION
    for yy in range(y0, y1):
        ty = (yy - TILE_ORIGIN_Y) % P
        for xx in range(x0, x1):
            out.putpixel((xx, yy), tile.getpixel((xx % P, ty)))
    hdr_font, row_font, tracking = _fonts(st["font"])
    draw = ImageDraw.Draw(out)
    sections = (
        ("LEAGUES COVERED", [list(r) for r in ac.LEAGUE_ROWS_WIDE]),
        ("EVERY PICK GRADED", [["Advanced Analytics", "Confidence Scores", "Market Edge"]]),
        ("ADAPTIVE ENGINE", [["Recalibration", "Simulations", "Cutting Edge Statistical Analysis"]]),
    )
    hy = HDR1
    for header, rows in sections:
        _draw_tracked(draw, W / 2, hy, header, hdr_font, st["hdr"], tracking)
        by = hy + HDR_TO_ROW
        for parts in rows:
            _draw_row(draw, by, parts, row_font, st["row"])
            by += ROW_STEP
        hy = by - ROW_STEP + ROW_TO_HDR
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-dir", help="write here instead of rewriting docs/")
    args = ap.parse_args()
    for name in CARDS:
        variant = name.replace("pinned_card_", "")
        src = DOCS / f"{name}.png"
        out = (Path(args.out_dir).expanduser() / f"{name}.png") if args.out_dir else src
        out.parent.mkdir(parents=True, exist_ok=True)
        meta = PngInfo()
        meta.add_text(ac.FP_KEY, ac.pinned_docs_fingerprint())     # lets test_ad_coverage.py detect a stale committed PNG
        refresh(Image.open(src), variant).save(out, optimize=True, pnginfo=meta)
        print(f"Saved -> {out}")


if __name__ == "__main__":
    main()
