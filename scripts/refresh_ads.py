"""
refresh_ads.py -- one command to rebuild every social post / content file from the current published numbers, in ONE folder:
league scorecard + adjusted-line scorecard + Reddit ad + subscription card + pinned card (PNG, carbon background only), the league-scorecard and Reddit-ad
glitch-reveal MP4s, and Reddit_Marketing_Pack.md.
Run `git pull` first so docs/sport_performance.json, docs/engine_performance_subscriber.json and docs/picks_backup.json (adjusted-line card) are today's.

The folder is ~/Desktop/Clairvoyance Ads 2026-11-17 (owner 2026-11-20: all social content lives in one Desktop folder, and an update rebuilds the files there in place --
same file names, overwritten).  Pass --out-dir to write somewhere else instead.
Side effect: gen_pinned_card.py also rewrites scripts/assets/covers_card.png (the daily "What We Cover" card) in the repo, as it always does.
Usage:  python3 scripts/refresh_ads.py
        python3 scripts/refresh_ads.py --out-dir <other folder>
"""
import argparse
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_OUT = Path.home() / "Desktop" / "Clairvoyance Ads 2026-11-17"
SCRIPTS = ("gen_league_scorecard.py", "gen_adjusted_scorecard.py", "gen_reddit_ad_v1.py", "gen_subscription_card.py", "gen_pinned_card.py",
           "gen_reddit_ad_videos.py", "gen_reddit_pack.py")

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-dir", default=str(DEFAULT_OUT))
    out = str(Path(ap.parse_args().out_dir).expanduser())
    Path(out).mkdir(parents=True, exist_ok=True)
    for script in SCRIPTS:
        subprocess.run([sys.executable, str(HERE / script), "--out-dir", out], check=True)
