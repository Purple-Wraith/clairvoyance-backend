"""
gen_reel_videos.py -- the Reddit ad + league scorecard as VERTICAL glitch-reveal videos (1080x1920 MP4, 30 fps) for Instagram Reels and TikTok.

Same carbon background, same neon colors, same glitch-reveal timing and the same live numbers as the square videos (gen_reddit_ad_videos.py,
which is left untouched -- its design is locked); only the layout is re-composed for a 9:16 phone screen.

Safe zone: Reels / TikTok draw their own UI over the top ~220 px, the bottom ~340 px and the right edge, so every element sits between
y=SAFE_TOP and y=SAFE_BOTTOM and inside x=SAFE_LEFT..SAFE_RIGHT.  The footer (handles + disclaimer) is a floating bar that ends at SAFE_BOTTOM, not a bar glued
to the bottom edge where the platform's caption would cover it.  test_reel_videos.py asserts every element's box stays inside this zone.

Outputs (in --out-dir):
  ClairvoyanceRedditAd_reel.mp4     ClairvoyanceLeagueScorecard_reel.mp4
Frame-stepped render (every CSS animation paused and seeked per frame), so timing is exact and nothing drops.  Uses Playwright's Chromium, or the
installed Google Chrome when the bundled one is missing.
Usage:  python3 scripts/gen_reel_videos.py --out-dir ~/Desktop/<folder>   [--html-only to just write the .html files]
"""
from __future__ import annotations

import argparse
import html
import subprocess
import tempfile
from pathlib import Path

import ad_coverage as ac
import ad_perf
import gen_reddit_ad_videos as sq

W, H = 1080, 1920
FPS = 30
SAFE_TOP, SAFE_BOTTOM, SAFE_LEFT, SAFE_RIGHT = 220, 1590, 60, 970   # content stays inside this box (the right edge is the busiest on both apps)
HOLD_S = 3.0                                       # fully-loaded hero ad stays on screen this long
SCORECARD_HOLD_S = 5.0
FADE, STEP = sq.FADE, sq.STEP

# the square page's CSS, re-sized: stage + canvas are 1080x1920, the footer floats inside the safe zone, small text is scaled up for a phone
CSS_V = f"""
  html,body {{ width:{W}px; height:{H}px; overflow:hidden; }}
  .stage {{ width:{W}px; height:{H}px; }}
  .hr {{ width:840px; }}
  .rule {{ left:70px; width:870px; }}
  .cta-box {{ top:1330px; width:820px; height:140px; border-radius:16px; }}
  .fbar {{ top:1500px; bottom:auto; height:90px; }}
  .fline1 {{ top:18px; font-size:20px; }}
  .fline2 {{ top:54px; font-size:16px; }}
"""


def _bg_js() -> str:
    return sq.BG_CARBON      # uses W / H from the page below, so the weave + soft lights span the tall canvas


def _page(body: str) -> str:
    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<link href="https://fonts.googleapis.com/css2?family=Share+Tech+Mono&family=Orbitron:wght@400;700;900&display=swap" rel="stylesheet">
<style>{sq.CSS}{sq.FBAR_CARBON}{CSS_V}</style>
</head>
<body>
  <canvas id="bgCanvas" width="{W}" height="{H}" style="position:absolute;top:0;left:0;z-index:-1"></canvas>
  <script>
    (function(){{
      var ctx=document.getElementById('bgCanvas').getContext('2d');
      var W={W},H={H},cx=W/2,cy=H/2;
{_bg_js()}
    }})();
  </script>
  <div class="stage">
{body}
    <div class="fbar">
      <div class="fline1">X @ClairvoyanceEng &nbsp;·&nbsp; IG @clairvoyanceengine</div>
      <div class="fline2">Model outputs are probabilistic projections, not financial advice</div>
    </div>
  </div>
</body>
</html>
"""


def _e(s: str) -> str:
    return html.escape(s, quote=False)


def ad_body(perf: dict) -> str:
    out = [sq._logo(254, 84),
           '<div class="sub c mm" style="top:322px;font-size:24px">ADVANCED SPORTS INTELLIGENCE ENGINE</div>',
           '<div class="hr" style="top:362px"></div>']
    # the two records stack: November on top, ALL TIME below (side by side would leave each number too small for a phone)
    for top, key, lbl, t in ((410, "month", perf["month_name"], 2.8), (685, "all", "ALL TIME", 3.4)):
        rec = perf["overall"][key]
        out.append(f'<div class="a orb M neon-m ctr c" style="top:{top}px;font-size:28px;letter-spacing:3px;--t:{t}s">{_e(lbl)}</div>')
        out.append(f'<div class="a orb M neon-m ctr c glitch" style="top:{top + 90}px;font-size:120px;--t:{t + .1}s">{ad_perf.fmt_pct(rec)}</div>')
        out.append(f'<div class="a orb C neon-c ctr c" style="top:{top + 170}px;font-size:34px;--t:{t + .4}s">{rec["w"]:,}W - {rec["l"]:,}L</div>')
        out.append(f'<div class="hr" style="top:{top + 220}px;--t:{t + .2}s"></div>')
    vp = ["Every pick uses mathematical precision,", "multi-layered ensemble models,", "and adaptive intelligence.",
          "Published live and graded on outcome —", "no cherry-picking, no deleted losses."]
    for i, ln in enumerate(vp):
        out.append(f'<div class="a mono W ctr c" style="top:{955 + 34 * i}px;font-size:26px;--t:{4.2 + .1 * i}s">{_e(ln)}</div>')
    out.append('<div class="a orb M neon-m ctr c glitch" style="top:1155px;font-size:28px;letter-spacing:3px;--t:4.9s">WHAT WE COVER</div>')
    for i, row in enumerate(ac.LEAGUE_ROWS):
        txt = " &nbsp;·&nbsp; ".join(_e(n) for n in row)
        out.append(f'<div class="a mono W ctr c" style="top:{1205 + 38 * i}px;font-size:25px;--t:{5.2 + .3 * i}s">{txt}</div>')
    out.append('<div class="cta-box c" style="--t:6.2s"></div>')
    out.append('<div class="a orb M neon-m ctr c glitch" style="top:1375px;font-size:25px;letter-spacing:1px;--t:6.5s">VIEW THE FULL TRACK RECORD AND MORE INFO</div>')
    out.append('<div class="a orb C neon-c ctr c" style="top:1425px;font-size:30px;letter-spacing:1px;--t:6.8s">clairvoyanceengine.info</div>')
    return "\n    ".join(out)


def ad_duration() -> float:
    return 7.6 + HOLD_S         # footer bar reveals at 6.6 s, the URL at 6.8 s


def scorecard_body(data: dict) -> str:
    X_NAME, X_M_REC, X_M_PCT, X_A_REC, X_A_PCT = 70, 440, 575, 722, 890
    out = [sq._logo(250, 60), '<div class="sub c mm" style="top:306px;font-size:24px;letter-spacing:3px">LEAGUE SCORECARD</div>',
           '<div class="hr" style="top:342px"></div>']

    def cell(x, y, txt, size, t, anchor="ctr", cls="W"):
        return f'<div class="a orb {cls} {anchor}" style="left:{x}px;top:{y}px;font-size:{size}px;--t:{t:.2f}s;--d:{FADE}s">{_e(txt)}</div>'

    out.append(cell((X_M_REC + X_M_PCT) // 2, 382, data["month_name"], 22, 2.6, cls="M neon-m"))
    out.append(cell((X_A_REC + X_A_PCT) // 2, 382, "ALL TIME", 22, 2.8, cls="M neon-m"))
    out.append(f'<div class="a mono C lft" style="left:{X_NAME}px;top:424px;font-size:17px;--t:2.9s;--d:.8s">SPORT / LEAGUE</div>')
    for x, t in ((X_M_REC, "W-L"), (X_M_PCT, "WIN %"), (X_A_REC, "W-L"), (X_A_PCT, "WIN %")):
        out.append(f'<div class="a mono C ctr" style="left:{x}px;top:424px;font-size:17px;--t:2.9s;--d:.8s">{t}</div>')
    out.append('<div class="rule" style="top:448px;--t:2.9s;--d:.8s"></div>')

    y, row_h, t = 482, 52, 3.0
    for r in ad_perf.sport_rows(data):
        league, bundle = r["kind"] == "league", r["kind"] == "bundle"
        if bundle:
            out.append(f'<div class="rule m" style="top:{y - row_h // 2 + 2}px;--t:{t:.2f}s;--d:{FADE}s"></div>')
        out.append(cell(X_NAME + (26 if league else 0), y, r["name"], 21 if league else 24, t, anchor="lft", cls="M" if league else "W"))
        for key, xr, xp in (("month", X_M_REC, X_M_PCT), ("all", X_A_REC, X_A_PCT)):
            sz = 20 if league else 22
            out.append(cell(xr, y, ad_perf.fmt_rec(r[key]), sz, t))
            out.append(cell(xp, y, ad_perf.fmt_pct(r[key]), sz, t))
        y += row_h
        t += STEP
    y += 6
    out.append(f'<div class="rule m" style="top:{y - row_h // 2 + 2}px;height:2px;--t:{t:.2f}s;--d:{FADE}s"></div>')
    o = data["overall"]
    out.append(cell(X_NAME, y + 4, "ALL SPORTS", 25, t + STEP, anchor="lft", cls="W"))
    for key, xr, xp in (("month", X_M_REC, X_M_PCT), ("all", X_A_REC, X_A_PCT)):
        out.append(cell(xr, y + 4, ad_perf.fmt_rec(o[key]), 24, t + STEP))
        out.append(cell(xp, y + 4, ad_perf.fmt_pct(o[key]), 24, t + STEP))
    notes = ["Every graded pick, published live, losses included.",
             f"Sorted by {data['month_name'].title()} win %. Updated {data['generated_at']}.",
             "Win % = wins / all graded picks (a third number is pushes)."]
    for i, n in enumerate(notes):
        out.append(f'<div class="a mono W ctr c" style="top:{y + 58 + 26 * i}px;font-size:17px;--t:{t + 1.0:.2f}s;--d:{FADE}s">{_e(n)}</div>')
    return "\n    ".join(out)


def scorecard_duration(data: dict) -> float:
    """Last element (the notes) finishes fading in at 3.0 + STEP*rows + 1.0 + FADE; then hold."""
    return 3.0 + STEP * len(ad_perf.sport_rows(data)) + 1.0 + FADE + SCORECARD_HOLD_S


def build_pages(data: dict) -> dict[str, str]:
    return {
        "ClairvoyanceRedditAd_reel": _page(ad_body(data)),
        "ClairvoyanceLeagueScorecard_reel": _page(scorecard_body(data)),
    }


def render(html_path: Path, out_path: Path, duration_s: float, fps: int = FPS) -> Path:
    """Frame-stepped render: pause every CSS animation, seek it to each frame's time, screenshot, encode.  Every animation on these pages is CSS."""
    from playwright.sync_api import sync_playwright
    import imageio_ffmpeg

    n = int(duration_s * fps)
    with tempfile.TemporaryDirectory() as tmp:
        with sync_playwright() as pw:
            try:
                browser = pw.chromium.launch()
            except Exception:
                browser = pw.chromium.launch(channel="chrome")      # machines without Playwright's bundled Chromium
            page = browser.new_context(viewport={"width": W, "height": H}).new_page()
            page.goto(f"file://{html_path}")
            page.evaluate("document.fonts.ready")
            page.wait_for_timeout(1200)                              # web fonts + the canvas background
            page.evaluate("document.getAnimations().forEach(a => a.pause())")
            for i in range(n):
                page.evaluate("t => document.getAnimations().forEach(a => { a.currentTime = t; })", i * 1000.0 / fps)
                page.screenshot(path=f"{tmp}/f{i:05d}.png")
            browser.close()
        subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-framerate", str(fps), "-i", f"{tmp}/f%05d.png", "-c:v", "libx264", "-preset", "medium", "-crf", "18",
                        "-pix_fmt", "yuv420p", "-r", str(fps), "-movflags", "+faststart", str(out_path)], check=True, capture_output=True)
    return out_path


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--html-only", action="store_true")
    args = ap.parse_args()
    out_dir = Path(args.out_dir).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    data = ad_perf.load()
    with tempfile.TemporaryDirectory() as tmp:
        for name, page_html in build_pages(data).items():
            hp = (out_dir if args.html_only else Path(tmp)) / f"{name}.html"
            hp.write_text(page_html)
            if args.html_only:
                print(f"Wrote {hp}")
                continue
            dur = scorecard_duration(data) if "Scorecard" in name else ad_duration()
            print(f"Saved -> {render(hp, out_dir / (name + '.mp4'), dur)}")
