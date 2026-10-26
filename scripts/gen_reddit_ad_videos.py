"""
gen_reddit_ad_videos.py -- the Reddit ad + league scorecard as glitch-reveal videos (1080x1080 MP4, 9 s), numbers read live from
ad_perf.py (the same files the PNG cards and the landing page use) and league rows from ad_coverage.py, so a video can never
quote a different record from its card.  Replaces the two hand-edited HTML templates that used to live on the Tron Disc
(they still said "71.7% WIN RATE", Bundesliga and MLS).

Both videos render on both design_system backgrounds (covers = crosshatch, void = black/teal diamond), matching the PNGs.

Outputs (in --out-dir):
  ClairvoyanceRedditAd.mp4            (covers bg)     ClairvoyanceRedditAd_void.mp4
  ClairvoyanceLeagueScorecard.mp4     (covers bg)     ClairvoyanceLeagueScorecard_void.mp4
Needs: pip install playwright imageio_ffmpeg && playwright install chromium (and network for the Google Fonts link).
Usage:  python3 scripts/gen_reddit_ad_videos.py --out-dir ~/Desktop/<folder>   [--html-only to just write the .html files]
"""
from __future__ import annotations

import argparse
import html
import subprocess
import tempfile
from pathlib import Path

import ad_coverage as ac
import ad_perf

W = H = 1080
DURATION_S = 9.0

BG_COVERS = """
      ctx.fillStyle='#222233';ctx.fillRect(0,0,W,H);
      var DS=Math.max(8,Math.round(W/64));
      ctx.save();ctx.strokeStyle='rgba(78,166,176,.5)';ctx.lineWidth=1;
      for(var i=-H;i<=W+H;i+=DS){ctx.beginPath();ctx.moveTo(i,0);ctx.lineTo(i+H,H);ctx.stroke();}
      for(var i=-H;i<=W+H;i+=DS){ctx.beginPath();ctx.moveTo(i,H);ctx.lineTo(i+H,0);ctx.stroke();}
      ctx.restore();
      var vg=ctx.createRadialGradient(cx,cy,W*.30,cx,cy,W*.72);
      vg.addColorStop(0,'rgba(0,0,0,0)');vg.addColorStop(1,'rgba(0,0,0,.22)');
      ctx.fillStyle=vg;ctx.fillRect(0,0,W,H);
"""
BG_VOID = """
      var minWH=Math.min(W,H);
      ctx.fillStyle='#080a0b';ctx.fillRect(0,0,W,H);
      var DS=30;
      ctx.save();ctx.strokeStyle='rgba(20,90,90,.6)';ctx.lineWidth=1;
      for(var i=-H;i<=W+H;i+=DS){ctx.beginPath();ctx.moveTo(i,0);ctx.lineTo(i+H,H);ctx.stroke();}
      for(var i=-H;i<=W+H;i+=DS){ctx.beginPath();ctx.moveTo(i,H);ctx.lineTo(i+H,0);ctx.stroke();}
      ctx.restore();
      var glowR=minWH*0.52;
      var gl=ctx.createRadialGradient(cx,H*0.4,0,cx,H*0.4,glowR);
      gl.addColorStop(0,'rgba(0,180,180,.10)');gl.addColorStop(1,'rgba(0,180,180,0)');
      ctx.fillStyle=gl;ctx.fillRect(0,0,W,H);
      var vg=ctx.createRadialGradient(cx,cy,minWH*.28,cx,cy,minWH*.75);
      vg.addColorStop(0,'rgba(0,0,0,0)');vg.addColorStop(1,'rgba(0,0,0,.6)');
      ctx.fillStyle=vg;ctx.fillRect(0,0,W,H);
"""

CSS = """
  * { margin:0; padding:0; box-sizing:border-box; }
  html,body { width:1080px; height:1080px; overflow:hidden; }
  .stage { width:1080px; height:1080px; position:relative; background:transparent; }
  .c { position:absolute; left:50%; }
  .mm { transform:translate(-50%,-50%); }
  .a { position:absolute; white-space:nowrap; opacity:0; animation: reveal .01s linear var(--t,0s) forwards; }
  .orb { font-family:'Orbitron',sans-serif; font-weight:700; }
  .mono { font-family:'Share Tech Mono',monospace; }
  .W { color:#fff; } .M { color:#f000ff; } .C { color:#00f0ff; } .G { color:#ffd500; }
  .ctr { transform:translate(-50%,-50%); } .lft { transform:translate(0,-50%); }

  .logo, .logo-r, .logo-c { font-family:'Orbitron',sans-serif; font-weight:900; letter-spacing:6px; white-space:nowrap; }
  .logo { color:#f000ff; text-shadow:0 0 13px #f000ff,0 0 28px rgba(240,0,255,.55); opacity:0; animation: settle .01s linear 1.5s forwards; }
  .logo-r { color:#ff2090; opacity:.8; animation: glitchR 1.2s steps(2) forwards, hide .01s linear 1.5s forwards; }
  .logo-c { color:#00f0ff; opacity:.8; animation: glitchC 1.2s steps(2) forwards, hide .01s linear 1.5s forwards; }
  .sub { white-space:nowrap; font-family:'Orbitron',sans-serif; font-weight:700; letter-spacing:4px; color:#00f0ff; text-shadow:0 0 12px #00f0ff; opacity:0; animation: reveal .01s linear 2.4s forwards; }
  .hr { position:absolute; left:50%; width:936px; height:1px; background:linear-gradient(90deg, transparent, rgba(240,0,255,.7), transparent); opacity:0; animation: reveal .01s linear var(--t,2.5s) forwards; transform:translateX(-50%); }
  .glitch { animation: reveal .01s linear var(--t) forwards, statGlitch .45s steps(4) var(--t) forwards; }
  .neon-m { text-shadow:0 0 11px #f000ff,0 0 26px rgba(240,0,255,.5); }
  .neon-c { text-shadow:0 0 10px rgba(0,240,255,.7); }
  .neon-g { text-shadow:0 0 10px rgba(255,213,0,.6); }
  .vline { position:absolute; left:540px; top:214px; width:2px; height:166px; background:rgba(240,0,255,.55); opacity:0; animation: reveal .01s linear 2.8s forwards; }
  .cta-box { top:736px; width:700px; height:118px; transform:translateX(-50%); background:rgba(15,5,28,.9); border:2px solid rgba(240,0,255,.9); border-radius:14px;
    box-shadow:0 0 22px rgba(240,0,255,.35), 0 0 4px rgba(240,0,255,.6); opacity:0; animation: reveal .01s linear 5.7s forwards; }
  .fbar { position:absolute; left:0; right:0; bottom:0; height:70px; background:#08080a; opacity:0; animation: reveal .01s linear 6.6s forwards; }
  .fbar::before { content:''; position:absolute; top:0; left:0; right:0; height:2px; background:rgba(240,0,255,.9); box-shadow:0 0 8px rgba(240,0,255,.7); }
  .fline1 { position:absolute; left:50%; top:24px; transform:translateX(-50%); white-space:nowrap; font-family:'Orbitron',sans-serif; font-weight:700; font-size:12px; letter-spacing:1px; color:#f000ff; text-shadow:0 0 8px rgba(240,0,255,.5); }
  .fline2 { position:absolute; left:50%; top:46px; transform:translateX(-50%); white-space:nowrap; font-family:'Share Tech Mono',monospace; font-size:11px; color:#787396; }
  .rule { position:absolute; left:64px; width:952px; height:1px; background:rgba(255,255,255,.16); opacity:0; animation: reveal .01s linear var(--t) forwards; }
  .rule.m { background:rgba(240,0,255,.45); }

  @keyframes glitchR { 0%{transform:translate(calc(-50% - 6px),calc(-50% - 4px))} 25%{transform:translate(calc(-50% + 5px),calc(-50% + 3px))} 50%{transform:translate(calc(-50% - 3px),calc(-50% - 2px))} 75%{transform:translate(calc(-50% + 6px),calc(-50% + 4px))} 100%{transform:translate(calc(-50% - 6px),calc(-50% - 4px))} }
  @keyframes glitchC { 0%{transform:translate(calc(-50% + 6px),calc(-50% + 3px))} 25%{transform:translate(calc(-50% - 5px),calc(-50% - 4px))} 50%{transform:translate(calc(-50% + 3px),calc(-50% + 2px))} 75%{transform:translate(calc(-50% - 6px),calc(-50% - 3px))} 100%{transform:translate(calc(-50% + 6px),calc(-50% + 3px))} }
  @keyframes hide { to { opacity:0; } }
  @keyframes settle { to { opacity:1; } }
  @keyframes reveal { to { opacity:1; } }
  @keyframes statGlitch {
    0%   { filter:hue-rotate(0deg) saturate(1); transform:translate(-50%,-50%); }
    20%  { filter:hue-rotate(110deg) saturate(4) brightness(1.3); transform:translate(calc(-50% + 4px),calc(-50% - 3px)); }
    40%  { filter:hue-rotate(-110deg) saturate(4) brightness(1.3); transform:translate(calc(-50% - 4px),calc(-50% + 3px)); }
    60%  { filter:hue-rotate(70deg) saturate(3); transform:translate(calc(-50% + 3px),calc(-50% + 2px)); }
    80%  { filter:hue-rotate(-70deg) saturate(3); transform:translate(calc(-50% - 3px),calc(-50% - 2px)); }
    100% { filter:hue-rotate(0deg) saturate(1); transform:translate(-50%,-50%); }
  }
"""


def _page(bg_js: str, body: str) -> str:
    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<link href="https://fonts.googleapis.com/css2?family=Share+Tech+Mono&family=Orbitron:wght@400;700;900&display=swap" rel="stylesheet">
<style>{CSS}</style>
</head>
<body>
  <canvas id="bgCanvas" width="1080" height="1080" style="position:absolute;top:0;left:0;z-index:-1"></canvas>
  <script>
    (function(){{
      var ctx=document.getElementById('bgCanvas').getContext('2d');
      var W=1080,H=1080,cx=W/2,cy=H/2;
{bg_js}
    }})();
  </script>
  <div class="stage">
{body}
    <div class="fbar">
      <div class="fline1">X @ClairvoyanceEng &nbsp;·&nbsp; clairvoyanceengine.info &nbsp;·&nbsp; IG @clairvoyanceengine</div>
      <div class="fline2">Model outputs are probabilistic projections, not financial advice</div>
    </div>
  </div>
</body>
</html>
"""


def _e(s: str) -> str:
    return html.escape(s, quote=False)


def _logo(top: int, size: int) -> str:
    st = f"top:{top}px;font-size:{size}px"
    return "".join(f'<div class="{c} c mm" style="{st}">CLAIRVOYANCE</div>' for c in ("logo-r", "logo-c", "logo"))


def ad_body(perf: dict) -> str:
    out = [_logo(92, 70),
           '<div class="sub c mm" style="top:148px;font-size:19px">ADVANCED SPORTS INTELLIGENCE ENGINE</div>',
           '<div class="hr" style="top:188px"></div>', '<div class="vline"></div>']
    for xc, key, lbl, t in ((-255, "month", perf["month_name"], 2.8), (255, "all", "ALL TIME", 3.1)):
        rec = perf["overall"][key]
        x = f"left:calc(50% + {xc}px)"
        out.append(f'<div class="a orb M neon-m ctr" style="{x};top:232px;font-size:20px;letter-spacing:2px;--t:{t}s">{_e(lbl)}</div>')
        out.append(f'<div class="a orb M neon-m ctr glitch" style="{x};top:300px;font-size:70px;--t:{t + .1}s">{ad_perf.fmt_pct(rec)}</div>')
        out.append(f'<div class="a orb C neon-c ctr" style="{x};top:358px;font-size:24px;--t:{t + .4}s">{rec["w"]:,}W - {rec["l"]:,}L</div>')
    out.append('<div class="hr" style="top:410px;--t:3.6s"></div>')
    vp = ["Every pick uses mathematical precision, multi-layered ensemble models,", "and adaptive intelligence.",
          "Published live and graded on outcome — no cherry-picking, no deleted losses."]
    for i, ln in enumerate(vp):
        out.append(f'<div class="a mono W ctr c" style="top:{480 + 28 * i}px;font-size:19px;--t:{3.8 + .1 * i}s">{_e(ln)}</div>')
    out.append('<div class="a orb M neon-m ctr c glitch" style="top:588px;font-size:22px;letter-spacing:3px;--t:4.3s">WHAT WE COVER</div>')
    for i, row in enumerate(ac.LEAGUE_ROWS):
        txt = " &nbsp;·&nbsp; ".join(_e(n) for n in row)
        out.append(f'<div class="a mono W ctr c" style="top:{632 + 28 * i}px;font-size:19px;--t:{4.6 + .3 * i}s">{txt}</div>')
    out.append('<div class="cta-box c"></div>')
    out.append('<div class="a orb G neon-g ctr c glitch" style="top:778px;font-size:21px;letter-spacing:1px;--t:6.0s">VIEW THE FULL TRACK RECORD AND MORE INFO</div>')
    out.append('<div class="a orb C neon-c ctr c" style="top:820px;font-size:22px;letter-spacing:1px;--t:6.3s">clairvoyanceengine.info</div>')
    return "\n    ".join(out)


def scorecard_body(data: dict) -> str:
    X_NAME, X_M_REC, X_M_PCT, X_A_REC, X_A_PCT = 64, 452, 590, 790, 940
    out = [_logo(62, 54), '<div class="sub c mm" style="top:112px;font-size:20px;letter-spacing:3px">LEAGUE SCORECARD</div>',
           '<div class="hr" style="top:142px"></div>']

    def cell(x, y, txt, size, t, anchor="ctr", cls="C"):
        return f'<div class="a orb {cls} {anchor}" style="left:{x}px;top:{y}px;font-size:{size}px;--t:{t:.2f}s">{_e(txt)}</div>'

    out.append(cell((X_M_REC + X_M_PCT) // 2, 172, data["month_name"], 19, 2.6, cls="M neon-m"))
    out.append(cell((X_A_REC + X_A_PCT) // 2, 172, "ALL TIME", 19, 2.8, cls="M neon-m"))
    out.append(f'<div class="a mono C lft" style="left:{X_NAME}px;top:204px;font-size:14px;--t:3s">SPORT / LEAGUE</div>')
    for x, t in ((X_M_REC, "W-L"), (X_M_PCT, "WIN %"), (X_A_REC, "W-L"), (X_A_PCT, "WIN %")):
        out.append(f'<div class="a mono C ctr" style="left:{x}px;top:204px;font-size:14px;--t:3s">{t}</div>')
    out.append('<div class="rule" style="top:222px;--t:3s"></div>')

    y, row_h, t = 246, 46, 3.3
    for r in ad_perf.block_rows(data):
        league, bundle = r["kind"] == "league", r["kind"] == "bundle"
        if bundle:
            out.append(f'<div class="rule m" style="top:{y - row_h // 2 + 2}px;--t:{t:.2f}s"></div>')
        out.append(cell(X_NAME + (26 if league else 0), y, r["name"], 18 if league else 20, t, anchor="lft"))
        for key, xr, xp in (("month", X_M_REC, X_M_PCT), ("all", X_A_REC, X_A_PCT)):
            sz = 17 if league else 19
            out.append(cell(xr, y, ad_perf.fmt_rec(r[key]), sz, t))
            out.append(cell(xp, y, ad_perf.fmt_pct(r[key]), sz, t))
        y += row_h
        t += 0.2
    y += 6
    out.append(f'<div class="rule m" style="top:{y - row_h // 2 + 2}px;height:2px;--t:{t:.2f}s"></div>')
    o = data["overall"]
    out.append(cell(X_NAME, y + 4, "ALL SPORTS", 22, t + .2, anchor="lft"))
    for key, xr, xp in (("month", X_M_REC, X_M_PCT), ("all", X_A_REC, X_A_PCT)):
        out.append(cell(xr, y + 4, ad_perf.fmt_rec(o[key]), 21, t + .2))
        out.append(cell(xp, y + 4, ad_perf.fmt_pct(o[key]), 21, t + .2))
    y += row_h + 8
    note = f"Every graded pick, published live, losses included. Sorted by {data['month_name'].title()} win %. Updated {data['generated_at']}."
    note2 = "Win % = wins / all graded picks (a third number is pushes)."
    for i, n in enumerate((note, note2)):
        out.append(f'<div class="a mono W ctr c" style="top:{min(y, H - 118) + 20 * i}px;font-size:14px;--t:{t + .6:.2f}s">{_e(n)}</div>')
    return "\n    ".join(out)


def build_pages(data: dict) -> dict[str, str]:
    return {
        "ClairvoyanceRedditAd": _page(BG_COVERS, ad_body(data)),
        "ClairvoyanceRedditAd_void": _page(BG_VOID, ad_body(data)),
        "ClairvoyanceLeagueScorecard": _page(BG_COVERS, scorecard_body(data)),
        "ClairvoyanceLeagueScorecard_void": _page(BG_VOID, scorecard_body(data)),
    }


def record(html_path: Path, out_path: Path, duration_s: float = DURATION_S) -> Path:
    from playwright.sync_api import sync_playwright
    import imageio_ffmpeg

    with tempfile.TemporaryDirectory() as tmp:
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            ctx = browser.new_context(viewport={"width": W, "height": H}, record_video_dir=tmp, record_video_size={"width": W, "height": H})
            page = ctx.new_page()
            page.goto(f"file://{html_path}")
            page.evaluate("document.fonts.ready")
            page.wait_for_timeout(int(duration_s * 1000))
            vid = page.video
            ctx.close()
            browser.close()
            webm = Path(vid.path())
        subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-i", str(webm), "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(out_path)],
                       check=True, capture_output=True)
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
            print(f"Saved -> {record(hp, out_dir / (name + '.mp4'))}")
