"""
gen_adjusted_scorecard.py -- Clairvoyance ADJUSTED-LINE scorecard (1080x1080, carbon background): the app's Analytics -> ADJ. LINES table as a social card.

Per league: picks locked at an adjusted line, their record at the line they were LOCKED at, the SAME picks regraded at the POSTED line they were moved from, and how many
the shift SAVED (lost at posted, won at locked).  Numbers are adj_perf.py -- a port of the app's own table (same grading, same win %), all time.
Look = the other scorecards (design_system.py; gen_league_scorecard.py locked 2026-10-27): league names magenta, numbers white, column headers cyan, group headers magenta.

Usage:  python3 scripts/gen_adjusted_scorecard.py --out-dir ~/Desktop
"""
from __future__ import annotations

import argparse
import datetime as dt
from pathlib import Path
from zoneinfo import ZoneInfo

from PIL import ImageDraw

import adj_perf
from ad_glow import MAG_TITLE_GLOW, MAG_LABEL_GLOW
from design_system import W, H, CX, MAG, CYN, WHT, orb, mono, glow, make_background_carbon, draw_footer, hline, SUB_GLOW

X_NAME = 64
X_PICKS = 372
X_A_REC, X_A_PCT = 488, 594          # LOCKED ADJ. LINE: W-L, WIN %
X_P_REC, X_P_PCT = 736, 842          # SAME PICKS AT POSTED: W-L, WIN %
X_SAVED = 966


def build_card(background_fn=make_background_carbon, data: dict | None = None, generated_at: str | None = None):
    data = data or adj_perf.load()
    generated_at = generated_at or dt.datetime.now(ZoneInfo("America/Denver")).strftime("%Y-%m-%d %H:%M MT")
    base = background_fn()
    base = glow(base, "CLAIRVOYANCE", (CX, 62), orb(54), MAG, MAG_TITLE_GLOW)
    base = glow(base, "ADJUSTED LINES", (CX, 112), orb(20), CYN, SUB_GLOW)
    base = hline(base, 142, color=MAG, alpha=90)

    for cx, l1, l2 in (((X_A_REC + X_A_PCT) // 2, "LOCKED", "ADJ. LINE"), ((X_P_REC + X_P_PCT) // 2, "SAME PICKS", "AT POSTED")):
        base = glow(base, l1, (cx, 166), orb(17), MAG, MAG_LABEL_GLOW)
        base = glow(base, l2, (cx, 190), orb(17), MAG, MAG_LABEL_GLOW)
    draw = ImageDraw.Draw(base)
    hf = mono(14)
    draw.text((X_NAME, 222), "LEAGUE", font=hf, fill=(*CYN, 255), anchor="lm")
    for x, t in ((X_PICKS, "PICKS"), (X_A_REC, "W-L"), (X_A_PCT, "WIN %"), (X_P_REC, "W-L"), (X_P_PCT, "WIN %"), (X_SAVED, "SAVED")):
        draw.text((x, 222), t, font=hf, fill=(*CYN, 255), anchor="mm")
    draw.line([(X_NAME, 240), (W - X_NAME, 240)], fill=(255, 255, 255, 40), width=1)

    def put_row(y, name, s, name_font, num_font, name_color):
        draw.text((X_NAME, y), name, font=name_font, fill=(*name_color, 255), anchor="lm")
        draw.text((X_PICKS, y), str(s["n"]), font=num_font, fill=(*WHT, 255), anchor="mm")
        draw.text((X_A_REC, y), adj_perf.rec(s["aw"], s["al"], s["ap"]), font=num_font, fill=(*WHT, 255), anchor="mm")
        draw.text((X_A_PCT, y), adj_perf.pct(s["aw"], s["al"]), font=num_font, fill=(*WHT, 255), anchor="mm")
        draw.text((X_P_REC, y), adj_perf.rec(s["bpw"], s["bpl"]) if s["both"] else "n/a", font=num_font, fill=(*WHT, 255), anchor="mm")
        draw.text((X_P_PCT, y), adj_perf.pct(s["bpw"], s["bpl"]) if s["both"] else "--", font=num_font, fill=(*WHT, 255), anchor="mm")
        draw.text((X_SAVED, y), str(s["saved"]) + (f" / -{s['cost']}" if s["cost"] else ""), font=num_font, fill=(*WHT, 255), anchor="mm")

    y, row_h = 270, 47
    for name, s in data["rows"]:
        put_row(y, name, s, orb(19), orb(18), MAG)
        y += row_h

    y += 2
    draw.line([(X_NAME, y - row_h // 2 + 2), (W - X_NAME, y - row_h // 2 + 2)], fill=(*MAG, 150), width=2)
    put_row(y + 6, "ALL LEAGUES", data["overall"], orb(21), orb(20), WHT)
    y += row_h + 14

    ny = min(y, H - 134)
    pend = data["overall"]["pending"]
    for i, line in enumerate((
        "Every adjusted pick, graded at the line it was locked at and at the original posted line.",
        f"Saved = lost at the posted line, won at the locked line. {pend} pending picks not counted.",
        f"Adjusted lines pay less per win. Updated {generated_at}.",
    )):
        draw.text((CX, ny + i * 20), line, font=mono(14), fill=(*WHT, 255), anchor="mm")
    return draw_footer(base, carbon=background_fn is make_background_carbon)


CARDS = [(make_background_carbon, "ClairvoyanceAdjustedLineScorecard_carbon.png")]       # carbon only (owner 2026-11-17)

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-dir", default=str(Path.home() / "Desktop"))
    args = ap.parse_args()
    out_dir = Path(args.out_dir).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    data = adj_perf.load()
    for bg, name in CARDS:
        build_card(bg, data).convert("RGB").save(out_dir / name, quality=97)
        print(f"Saved -> {out_dir / name}")
