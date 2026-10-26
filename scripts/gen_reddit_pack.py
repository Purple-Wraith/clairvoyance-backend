"""
gen_reddit_pack.py -- Reddit_Marketing_Pack.md: ad copy + organic posts with this month's / all-time records filled in from ad_perf.py
(the same files the cards, videos and landing page use).  Records + win % only, no units.  The two audit posts (soccer moneyline floor,
euro-hockey unders) quote fixed, dated findings and are marked in-sample.
Usage:  python3 scripts/gen_reddit_pack.py --out-dir ~/Desktop/<folder>
"""
from __future__ import annotations

import argparse
from pathlib import Path

import ad_perf


def _r(rec):
    return f"{rec['w']:,}-{rec['l']:,}" + (f"-{rec['p']}" if rec["p"] else "")


def render(d: dict) -> str:
    m, a = d["overall"]["month"], d["overall"]["all"]
    mn = d["month_name"].title()
    rows = ad_perf.block_rows(d)
    lg = sorted(d["leagues"].items(), key=lambda kv: -(kv[1]["month"]["pct"] or 0))
    top3 = ", ".join(f"{n} {ad_perf.fmt_pct(v['month'])}" for n, v in lg[:3])
    ranked = [(n, v) for n, v in lg if v["month"]["n"] >= 20]
    worst_n, worst = ranked[-1]
    best_n, best = ranked[0]
    table = "\n".join(
        f"| {'**' + r['name'] + '**' if r['kind'] != 'league' else r['name']} | {_r(r['month'])} · {ad_perf.fmt_pct(r['month'])} | {_r(r['all'])} · {ad_perf.fmt_pct(r['all'])} |"
        for r in rows)
    return f"""# Clairvoyance — Reddit marketing pack (data as of {d['generated_at']})

Every number is read from the live files the landing page uses (`docs/sport_performance.json`, `docs/engine_performance_subscriber.json`). Regenerate everything (cards, videos, this pack) with `python3 scripts/refresh_ads.py --out-dir <folder>` after `git pull`.

## The numbers (records + win %, no units)

| | {d['month_name']} | ALL TIME |
|---|---|---|
| **All sports** | {_r(m)} · **{ad_perf.fmt_pct(m)}** | {_r(a)} · **{ad_perf.fmt_pct(a)}** |
{table}

Win % = wins ÷ all graded picks (a third record number is pushes), the landing page's definition. Bundle rows are sums of their leagues.

## Before posting anything — rules to check (not verified from here)

- **Reddit Ads:** I could not read Reddit's gambling-ads policy. X and Meta restrict ads for tips/picks services and require pre-approval in many countries, so assume Reddit may too: read Reddit's Advertising Policies and apply for gambling approval before spending.
- **Subreddits:** many betting subs ban selling picks and self-promotion. Open each sub's sidebar/wiki first. Post the organic pieces as *content* (record, method, audit), not a sales pitch.

## A. Paid ad creative (3 variants)

Assets: `ClairvoyanceRedditAd` (.png/.mp4, + `_void`) = {mn} + all-time hero. `ClairvoyanceLeagueScorecard` (.png/.mp4, + `_void`) = full league table.

| # | Asset | Headline | Body | CTA |
|---|---|---|---|---|
| 1 | Reddit Ad | {ad_perf.fmt_pct(m)} in {mn}. {ad_perf.fmt_pct(a)} all-time. Every pick published and graded. | {a['w']:,} wins, {a['l']:,} losses, nothing deleted. 12 leagues across football, basketball, hockey and soccer. See the full record. | View track record |
| 2 | League Scorecard | Every league, every record — including the weak ones. | {top3} this month. {worst_n} {ad_perf.fmt_pct(worst['month'])} — we publish that too. | See the scorecard |
| 3 | Reddit Ad (void) | A public track record, not a promise. | Mathematical models, graded picks, game lines on 12 leagues. Starting at $20 for 30 days. | Learn more |

Destination: clairvoyanceengine.info. Pricing if asked: $20 / $30 / $45 / $60 / $75 for 1–5 sports, 30-day access.

## B. Organic posts (value-first)

**1. The scorecard (transparency)**
- Title: `We publish every pick we make. Here's {mn}, league by league — worst one included.`
- Body: `Engine record this month: {_r(m)} ({ad_perf.fmt_pct(m)}). All-time {_r(a)} ({ad_perf.fmt_pct(a)}). {best_n} is best at {ad_perf.fmt_pct(best['month'])}, {worst_n} worst at {ad_perf.fmt_pct(worst['month'])} ({_r(worst['month'])}). Win % is wins over all graded picks. Happy to answer questions on how anything is graded.`
- Image: League Scorecard. No price, no link in the post body.

**2. We audited our own bets and cut one (method)** — in-sample, fixed finding
- Title: `We checked our own soccer moneylines and found a leak: the 60–62% picks went 8-16.`
- Body: `Soccer 3-way moneylines (PL, La Liga, Serie A, UCL), Aug 17–Oct 25: picks the model rated 65%+ went 79-21 (79%); below 65% went 50-38 (57%). The worst band was 60–62% at 8-16 — the 3-way "win" bet loses every draw. We now skip Premier League / La Liga / Serie A moneylines under 65%. Caveat: n=188, and we picked the cut-off from this same sample, so treat it as in-sample until more picks settle.`

**3. Euro hockey totals: the market has been pricing overs too high** — fixed finding, Oct 2–25
- Title: `SHL / Liiga / NLA / Extraliga: unders at 5.5 and 6.5 have hit 63% and 72% vs 54% and 64% implied.`
- Body: `427 game-lines, Oct 2–25, one bookmaker's snapshot per game. Under 5.5 hit 63% (no-vig implied 54%), under 6.5 hit 72% (64%), overs at 5.5 only 37% (46%). SHL and Extraliga show it most, NLA a little, Liiga barely. About 50 games per line per league, so it may be a hot three weeks. We've added a small under lean to our euro hockey totals; will report back at ~100 games per league.`

**4. Per-league posts** (league-specific subs where betting talk is allowed): use the league's own row from the table above.

## C. Reply templates

- **"Cherry-picked?"** → `Every locked pick is graded and published, losses included. Full league table: [scorecard].`
- **"What are the units / ROI?"** → `We quote records and win % only. Some soccer moneylines and early euro-hockey spreads were priced at placeholder odds, so unit totals aren't comparable with real-market units.`
- **"Are you selling picks?"** → answer plainly if asked: 30-day access from $20; don't volunteer it in a sub that bans promotion.

## D. Posting order (MT)

1. Read Reddit's gambling-ad policy and each target sub's rules (15 min). Apply for ad approval if required.
2. Post organic #1 (scorecard), then #2 (audit) at least a few hours apart; don't paste the same text into many subs.
3. Once approved: launch ad 1 and ad 2 as separate ad sets to compare; keep budget small until one wins.
4. Answer comments for the first hour after each post.

## Not updated

- Pinned cards and the subscription card/video (2026-10-10 set) still say "NBA player props", which came off the landing page on 10-25.
"""


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-dir", required=True)
    args = ap.parse_args()
    out = Path(args.out_dir).expanduser(); out.mkdir(parents=True, exist_ok=True)
    (out / "Reddit_Marketing_Pack.md").write_text(render(ad_perf.load()))
    print(f"Saved -> {out / 'Reddit_Marketing_Pack.md'}")
