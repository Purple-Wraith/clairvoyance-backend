"""
gen_reddit_ad_v1.py — Clairvoyance Reddit advertising card.

New card, distinct from CorrectPinnedCard2.0.png (that one is an "about /
reference" card meant to sit pinned on a profile; this one is a single-
focus ad meant to stop a scroll). Built on the shared design_system.py
library for full brand consistency (glow presets, palette, footer).
Leads with a real, current, verifiable stat as the hero element rather
than hype copy, since Reddit's audience is unusually ad-skeptical and
rewards concrete numbers over claims — the all-time record was chosen
specifically for its sample size (n=733) over a single day/week number,
which would read as cherry-picked.

Renders the same content on both design_system backgrounds -- covers_card
style (matches ClairvoyanceRedditAd1.mp4 / CorrectPinnedCard6.0.png) and
void-black/teal-diamond style (matches ClairvoyanceRedditAd.mp4 /
CorrectPinnedCard2.0.png) -- so the two variants can never drift out of
sync with each other the way two separate scripts did.

Output:
  ~/Desktop/ClairvoyanceRedditAd.png                                (covers_card bg)
  /Volumes/Tron Disc/Clairvoyance/Advertising/ClairvoyanceRedditAd.png (void bg)
"""
import argparse
import json
from pathlib import Path

import ad_coverage as ac
import ad_perf
from design_system import (
    W, H, CX, BG, CARD, MAG, CYN, GOLD, RED, WHT, DIM, T2, T3,
    orb, mono, glow, make_background, make_background_covers, draw_footer, hline,
    TITLE_GLOW, SUB_GLOW, HDR_GLOW, SOCIAL_GLOW,
)
from PIL import Image, ImageDraw, ImageFilter


ROOT = Path(__file__).resolve().parent.parent
PERF_JSON = ROOT / 'docs' / 'engine_performance_subscriber.json'


def hero_win_rate_pct(path: Path = PERF_JSON) -> str:
    """All-time win rate for the hero stat, read from the same subscriber-scope file the landing page's
    Live Track Record uses -- it used to be a hard-coded '71.7%' (n=733) that drifted as the ledger grew."""
    d = json.loads(Path(path).read_text())
    allt = next(p for p in d['periods'] if p['key'] == 'ALL_TIME')
    return f"{allt['pct'] * 100:.1f}%"


def cx_text(draw, text, font):
    bb = draw.textbbox((0, 0), text, font=font)
    return (W - (bb[2] - bb[0])) // 2


def dotted_row(draw, parts, ym, font, color=WHT, dot_color=MAG, dot_w=20):
    DOT_W, dot_r = dot_w, 1
    total = sum(font.getlength(p) for p in parts) + DOT_W * (len(parts) - 1)
    x = CX - total / 2
    for i, p in enumerate(parts):
        pw = font.getlength(p)
        draw.text((x, ym), p, font=font, fill=(*color, 255), anchor='lm')
        x += pw
        if i < len(parts) - 1:
            dot_x = x + DOT_W // 2
            draw.ellipse([dot_x - dot_r, ym - dot_r, dot_x + dot_r, ym + dot_r], fill=(*dot_color, 230))
            x += DOT_W


def glow_tracked(base, text, cy, font, color, layers, tracking=3):
    """Same as glow(), but with manual letter-spacing (PIL text has no
    tracking param). Without it, a short all-caps header like 'WHAT WE
    COVER' at a small size has its per-letter glow halos overlap into a
    smeared blob -- the video templates avoid this via CSS letter-spacing,
    so this reproduces that here by glowing each character separately."""
    widths = [font.getlength(ch) for ch in text]
    total = sum(widths) + tracking * (len(text) - 1)
    x = CX - total / 2
    for ch, w in zip(text, widths):
        if ch != ' ':
            base = glow(base, ch, (x + w / 2, cy), font, color, layers, anchor='mm')
        x += w + tracking
    return base


def build_card(background_fn):
    base = background_fn()
    draw = ImageDraw.Draw(base)

    # ── Brand header ─────────────────────────────────────────────
    base = glow(base, 'CLAIRVOYANCE', (CX, 92), orb(70), MAG, TITLE_GLOW)
    base = glow(base, 'ADVANCED SPORTS INTELLIGENCE ENGINE', (CX, 148), orb(19), CYN, SUB_GLOW)

    draw = ImageDraw.Draw(base)
    base = hline(base, 188, color=MAG, alpha=90)

    # ── Hero stats (the actual hook) ──────────────────────────────
    # 2026-10-26 (owner): this month's AND the all-time record + win %, read live from the same files the landing page uses
    # (ad_perf).  Earlier (2026-09) the card was win-rate only; records are back on explicit request.
    perf = ad_perf.load()
    for xc, key, lbl in ((CX - 255, 'month', perf['month_name']), (CX + 255, 'all', 'ALL TIME')):
        rec = perf['overall'][key]
        base = glow(base, lbl, (xc, 232), orb(20), GOLD, SOCIAL_GLOW)
        base = glow(base, ad_perf.fmt_pct(rec), (xc, 300), orb(70), MAG, TITLE_GLOW)
        base = glow(base, f"{rec['w']:,}W - {rec['l']:,}L", (xc, 358), orb(24), CYN, SOCIAL_GLOW)
    draw = ImageDraw.Draw(base)
    draw.line([(CX, 214), (CX, 380)], fill=(*MAG, 120), width=2)
    base = hline(base, 410, color=MAG, alpha=90)

    # ── Value proposition ─────────────────────────────────────────
    draw = ImageDraw.Draw(base)
    vp_lines = [
        'Every pick uses mathematical precision, multi-layered ensemble models,',
        'and adaptive intelligence.',
        'Published live and graded on outcome — no cherry-picking, no deleted losses.',
    ]
    bf = mono(19)
    vy = 470
    for ln in vp_lines:
        x = cx_text(draw, ln, bf)
        draw.text((x, vy), ln, font=bf, fill=(*WHT, 255))
        vy += 28
    vy += 24

    # ── Coverage ──────────────────────────────────────────────────
    base = glow_tracked(base, 'WHAT WE COVER', vy + 10, orb(22), MAG, SOCIAL_GLOW)
    vy += 46
    draw = ImageDraw.Draw(base)
    cf = mono(19)
    # League rows come from ad_coverage.LEAGUE_ROWS (the single source of truth, pinned to the real
    # engine by scripts/test_ad_coverage.py) -- never hard-code a league list here.
    for i, row in enumerate(ac.LEAGUE_ROWS):
        dotted_row(draw, list(row), vy + 8, cf, dot_w=30)
        vy += 28 if i < len(ac.LEAGUE_ROWS) - 1 else 56

    # ── CTA box ─────────────────────────────────────────────────────
    box_w, box_h = 700, 118
    bx0, by0 = CX - box_w // 2, vy
    bx1, by1 = CX + box_w // 2, vy + box_h
    glow_layer = Image.new('RGBA', (W, H), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow_layer)
    for pad, alpha in [(10, 40), (5, 80), (2, 140)]:
        gd.rounded_rectangle([bx0 - pad, by0 - pad, bx1 + pad, by1 + pad], radius=14, outline=(*MAG, alpha), width=3)
    glow_layer = glow_layer.filter(ImageFilter.GaussianBlur(6))
    base = Image.alpha_composite(base, glow_layer)
    draw = ImageDraw.Draw(base)
    draw.rounded_rectangle([bx0, by0, bx1, by1], radius=14, fill=(*CARD, 235), outline=(*MAG, 255), width=2)
    base = glow(base, 'VIEW THE FULL TRACK RECORD AND MORE INFO', (CX, by0 + 42), orb(21), GOLD, HDR_GLOW)
    draw = ImageDraw.Draw(base)
    base = glow(base, 'clairvoyanceengine.info', (CX, by0 + 84), orb(22), CYN, HDR_GLOW)

    # ── Basis footnote ──────────────────────────────────────────────
    draw = ImageDraw.Draw(base)
    foot = f"All graded picks, incl. some locked after kickoff. Updated {perf['generated_at']}."
    draw.text((CX, by1 + 34), foot, font=mono(13), fill=(172, 168, 200, 255), anchor='mm')

    # ── Footer ────────────────────────────────────────────────────
    draw = ImageDraw.Draw(base)
    base = draw_footer(base)

    return base


TRON_ADS = Path('/Volumes/Tron Disc/Clairvoyance/Advertising')
CARDS = [
    (make_background_covers, Path.home() / 'Desktop' / 'ClairvoyanceRedditAd.png'),
    (make_background, TRON_ADS / 'ClairvoyanceRedditAd.png'),
]

if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--out-dir', help='write ClairvoyanceRedditAd.png (covers bg) + ClairvoyanceRedditAd_void.png (void bg) here instead of the defaults')
    args = ap.parse_args()
    for background_fn, out in CARDS:
        if args.out_dir:
            name = 'ClairvoyanceRedditAd.png' if background_fn is make_background_covers else 'ClairvoyanceRedditAd_void.png'
            out = Path(args.out_dir).expanduser() / name
        elif not out.parent.exists():
            print(f'SKIP {out} (folder not available -- Tron Disc not mounted?)')
            continue
        card = build_card(background_fn)
        card.convert('RGB').save(out, quality=97)
        print(f'Saved -> {out}')
