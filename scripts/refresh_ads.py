"""
refresh_ads.py -- one command to rebuild the whole Reddit asset set from the current published numbers:
league scorecard + Reddit ad (PNG, both backgrounds), their four glitch-reveal MP4s, and Reddit_Marketing_Pack.md.
Run `git pull` first so docs/sport_performance.json and docs/engine_performance_subscriber.json are today's.
Usage:  python3 scripts/refresh_ads.py --out-dir ~/Desktop/<folder>
"""
import argparse
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-dir", required=True)
    out = ap.parse_args().out_dir
    for script in ("gen_league_scorecard.py", "gen_reddit_ad_v1.py", "gen_reddit_ad_videos.py", "gen_reddit_pack.py"):
        subprocess.run([sys.executable, str(HERE / script), "--out-dir", out], check=True)
