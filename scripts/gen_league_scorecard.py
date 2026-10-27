"""
DESIGN LOCKED 2026-10-27 by the owner (colors, layout, glow) -- change only on explicit request; spec in memory project_reddit_cards_locked_design.
gen_league_scorecard.py -- Clairvoyance league scorecard (1080x1080): this month's and the all-time record + win % for every
sport and league the paid products cover.  Built for Reddit (concrete numbers beat claims with an ad-skeptical audience).

All numbers come from ad_perf.py (the same files the landing page's Live Track Record reads); league names and coverage from
ad_coverage.py.  Records + win % only -- no units.  Same brand system as the other cards (design_system.py).

Outputs (same content on both design_system backgrounds):
  <out-dir>/ClairvoyanceLeagueScorecard.png         covers bg
  <out-dir>/ClairvoyanceLeagueScorecard_void.png    void bg
Usage:  python3 scripts/gen_league_scorecard.py --out-dir ~/Desktop
"""
from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image, ImageDraw

import ad_perf
from ad_glow import MAG_TITLE_GLOW, MAG_LABEL_GLOW
from design_system import (
    W, H, CX, MAG, CYN, GOLD, WHT, DIM, orb, mono, glow, make_background, make_background_covers, draw_footer, hline,
    TITLE_GLOW, SUB_GLOW, SOCIAL_GLOW,
)

LIME = (187, 255, 0)
PINK = (255, 32, 144)
# Column centres / anchors
X_NAME = 64
X_M_REC, X_M_PCT = 452, 590
X_A_REC, X_A_PCT = 790, 940


def pct_color(p):
    """Same win % colour rule as the app's track-record cards (cyan >= 62%, lime >= 50%, pink below)."""
    return DIM if p is None else CYN if p >= .62 else LIME if p >= .50 else PINK


def build_card(background_fn, data: dict | None = None):
    data = data or ad_perf.load()
    rows = ad_perf.sport_rows(data)
    base = background_fn()
    base = glow(base, "CLAIRVOYANCE", (CX, 62), orb(54), MAG, MAG_TITLE_GLOW)
    base = glow(base, "LEAGUE SCORECARD", (CX, 112), orb(20), CYN, SUB_GLOW)
    base = hline(base, 142, color=MAG, alpha=90)

    # column headers
    base = glow(base, data["month_name"], ((X_M_REC + X_M_PCT) // 2, 172), orb(19), MAG, MAG_LABEL_GLOW)
    base = glow(base, "ALL TIME", ((X_A_REC + X_A_PCT) // 2, 172), orb(19), MAG, MAG_LABEL_GLOW)
    draw = ImageDraw.Draw(base)
    hf = mono(14)
    draw.text((X_NAME, 204), "SPORT / LEAGUE", font=hf, fill=(*CYN, 255), anchor="lm")
    for x, t in ((X_M_REC, "W-L"), (X_M_PCT, "WIN %"), (X_A_REC, "W-L"), (X_A_PCT, "WIN %")):
        draw.text((x, 204), t, font=hf, fill=(*CYN, 255), anchor="mm")
    draw.line([(X_NAME, 222), (W - X_NAME, 222)], fill=(255, 255, 255, 40), width=1)

    y, row_h = 240, 42
    nf_b, nf_l = orb(20), orb(18)
    for r in rows:
        bundle, league = r["kind"] == "bundle", r["kind"] == "league"
        if bundle:
            draw.line([(X_NAME, y - row_h // 2 + 2), (W - X_NAME, y - row_h // 2 + 2)], fill=(*MAG, 70), width=1)
        name_font = nf_l if league else nf_b
        draw.text((X_NAME + (26 if league else 0), y), r["name"], font=name_font, fill=(*(MAG if league else WHT), 255), anchor="lm")
        rec_font = orb(17) if league else orb(19)
        for key, xr, xp in (("month", X_M_REC, X_M_PCT), ("all", X_A_REC, X_A_PCT)):
            rec = r[key]
            draw.text((xr, y), ad_perf.fmt_rec(rec), font=rec_font, fill=(*WHT, 255), anchor="mm")
            draw.text((xp, y), ad_perf.fmt_pct(rec), font=rec_font, fill=(*WHT, 255), anchor="mm")
        y += row_h

    # overall row
    y += 6
    draw.line([(X_NAME, y - row_h // 2 + 2), (W - X_NAME, y - row_h // 2 + 2)], fill=(*MAG, 150), width=2)
    o = data["overall"]
    draw.text((X_NAME, y + 4), "ALL SPORTS", font=orb(22), fill=(*WHT, 255), anchor="lm")
    for key, xr, xp in (("month", X_M_REC, X_M_PCT), ("all", X_A_REC, X_A_PCT)):
        draw.text((xr, y + 4), ad_perf.fmt_rec(o[key]), font=orb(21), fill=(*WHT, 255), anchor="mm")
        draw.text((xp, y + 4), ad_perf.fmt_pct(o[key]), font=orb(21), fill=(*WHT, 255), anchor="mm")
    y += row_h + 8

    note = f"Every graded pick, published live, losses included. Sorted by {data['month_name'].title()} win %. Updated {data['generated_at']}."
    note2 = "Win % = wins / all graded picks (a third number is pushes)."
    draw.text((CX, min(y, H - 118)), note, font=mono(14), fill=(*WHT, 255), anchor="mm")
    draw.text((CX, min(y, H - 118) + 20), note2, font=mono(14), fill=(*WHT, 255), anchor="mm")
    base = draw_footer(base)
    return base


CARDS = [(make_background_covers, "ClairvoyanceLeagueScorecard.png"), (make_background, "ClairvoyanceLeagueScorecard_void.png")]

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-dir", default=str(Path.home() / "Desktop"))
    args = ap.parse_args()
    out_dir = Path(args.out_dir).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    data = ad_perf.load()
    for bg, name in CARDS:
        card = build_card(bg, data)
        card.convert("RGB").save(out_dir / name, quality=97)
        print(f"Saved -> {out_dir / name}")
