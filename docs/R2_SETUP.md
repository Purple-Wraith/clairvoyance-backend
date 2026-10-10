# Cloudflare R2 for the high-churn files -- owner setup

Why: `docs/picks_backup.json` (2 MB, rewritten by most lock/settle passes), `docs/picks_backup_meta.json` and `docs/live_data.json` (rewritten every live-score tick) are what fill the repo with bot commits and
huge diffs. R2 is an S3-compatible object store; this repo can publish those files there instead.

**Where you are now = phase 1 (dual-write).** Every workflow that writes those files still commits them to the repo exactly as before, AND (once the four secrets below exist) copies them to R2.
The app reads from R2 only if you set a base URL (step 6), and falls back to the repo copy on any failure. Nothing changes until you do these steps, and nothing breaks if you do only some of them.

## 0. Cost and limits (check Cloudflare's current pricing page before you start)

* R2's free tier (at the time of writing): **10 GB-month storage, 1 million Class A operations (writes/lists), 10 million Class B operations (reads) per month, and no egress fees.**
* This setup stores ~5 MB and does roughly 36 live-score writes/day in season plus a handful of ledger writes per day (an upload is skipped whenever the content hash is unchanged), so it should stay far
  inside the free tier. Reads are the visitors' browsers: each app open reads `live_data.json` every ~45 s while open, so keep an eye on the 10 M Class B reads if traffic grows.
* **Cloudflare may require a payment method on the account before it lets you enable R2, even though the free tier should cover this.** Add a card (or confirm what your account needs) at
  dash.cloudflare.com -> R2 Object Storage -> "Purchase R2 Plan"/"Enable R2". Set a billing alert if offered.
* An `r2.dev` public URL is for development: Cloudflare rate-limits it and does no edge caching. For production use a custom domain on a Cloudflare-managed zone (step 3, option B).

## 1. Create the bucket

Cloudflare dashboard -> **R2 Object Storage** -> **Create bucket**. Name it e.g. `clairvoyance-data` (this is `R2_BUCKET`). Location: Automatic. Default storage class (Standard).
Your **Account ID** (this is `R2_ACCOUNT_ID`) is shown on the R2 overview page (right-hand side) and in the dashboard URL.

## 2. CORS rule (the browser app on GitHub Pages reads R2 cross-origin)

Bucket -> **Settings** -> **CORS Policy** -> Add/Edit -> paste:

```json
[
  {
    "AllowedOrigins": [
      "https://purple-wraith.github.io",
      "https://clairvoyanceengine.info"
    ],
    "AllowedMethods": ["GET", "HEAD"],
    "AllowedHeaders": ["*"],
    "ExposeHeaders": ["ETag"],
    "MaxAgeSeconds": 3600
  }
]
```

(Add `"https://www.clairvoyanceengine.info"` if that host serves the app, and `"http://localhost:8000"` if you test locally. The app only issues plain GET requests, so no preflight is needed.)
Without this rule the browser blocks the read and the app silently keeps using the repo copy -- safe, but R2 is then never used.

## 3. Make the bucket publicly readable (pick ONE)

* **A. Quick test -- r2.dev URL:** Bucket -> Settings -> **Public Development URL** -> Enable (type `allow`). You get `https://pub-<hash>.r2.dev`. Fine to verify the setup; not for production traffic.
* **B. Production -- custom domain:** Bucket -> Settings -> **Custom Domains** -> Add, e.g. `data.clairvoyanceengine.info`. The domain's DNS zone must be on Cloudflare (if `clairvoyanceengine.info`
  is not, either move that zone's DNS to Cloudflare or use a Cloudflare-hosted domain you own). Cloudflare creates the DNS record and certificate for you.

The objects are served with `Cache-Control: no-cache` (set by `scripts/r2_publish.py`), so browsers revalidate on every read. The base URL is what you will put in step 6 (no trailing slash).
Public read exposes everything in this bucket -- only these three files (all already public in this repo) are ever put there. Do not reuse the bucket for anything private.

## 4. Create an API token (for GitHub Actions only)

R2 Object Storage -> **Manage R2 API Tokens** -> **Create API token**:
* Permissions: **Object Read & Write**
* Specify bucket: **only** the bucket from step 1 (not "All buckets")
* TTL: your choice (no expiry is fine; rotate yearly)

After "Create", copy the **Access Key ID** and **Secret Access Key** immediately (the secret is shown once). Ignore the "Token value" / Bearer token -- this code uses the S3 key pair.

## 5. Add the four GitHub secrets

GitHub repo -> Settings -> Secrets and variables -> Actions -> **New repository secret**, four times:

| Secret name | Value |
|---|---|
| `R2_ACCOUNT_ID` | the Account ID from step 1 |
| `R2_ACCESS_KEY_ID` | Access Key ID from step 4 |
| `R2_SECRET_ACCESS_KEY` | Secret Access Key from step 4 |
| `R2_BUCKET` | the bucket name, e.g. `clairvoyance-data` |

If any of the four is missing/empty, `scripts/r2_publish.py` logs `R2 not configured (missing: ...)` and does nothing -- the workflows stay green.

## 6. Seed R2 and verify

1. Trigger a write: Actions -> **Live Score Tracker** -> Run workflow (copies `live_data.json`), and Actions -> **Auto Lock + Settle** -> Run workflow (copies the ledger backup; leave `live` unchecked for a dry run
   -- the backup is still mirrored). In each run, open the **Publish** step log: you should see `[r2] live_data.json: uploaded ...` and later `... unchanged -- skipped`.
   Or from your own machine: `R2_ACCOUNT_ID=... R2_ACCESS_KEY_ID=... R2_SECRET_ACCESS_KEY=... R2_BUCKET=... python3 scripts/r2_publish.py --force docs/live_data.json docs/picks_backup.json docs/picks_backup_meta.json`
2. Check the public URL (replace BASE):
   ```
   curl -sI https://BASE/live_data.json | grep -i -E "^HTTP|content-type|cache-control"
   curl -sI -H "Origin: https://purple-wraith.github.io" https://BASE/picks_backup.json | grep -i access-control-allow-origin
   curl -s https://BASE/picks_backup.json | python3 -c "import json,sys; print(len(json.load(sys.stdin)), 'picks')"
   ```
   Expect `200`, `application/json; charset=utf-8`, `no-cache`, an `access-control-allow-origin` line, and the same pick count as `docs/picks_backup.json` in the repo.
3. **Point the app at it:** in `docs/config.js` change the last line to
   `window.CV_R2_BASE = window.CV_R2_BASE || 'https://BASE';` (no trailing slash), commit and push (a human push to `docs/**` deploys Pages on its own; the mobile mirror copies `config.js` too).
4. Open the app with DevTools -> Network: `live_data.json?...` should now go to the R2 host (status 200). Break it on purpose (e.g. temporarily change the base to a wrong host): the app must keep working
   from the repo copy -- that is the fallback, and it is covered by `scripts/test_r2_app.py`.
5. **Roll back at any time:** set the base back to `''` (one commit). Phase 1 never stops committing the files, so there is nothing else to undo.

Leave it in this state for a few days (a full lock/settle cycle and a live game day) and compare: R2's `live_data.json` `ts` should track the repo's, and `picks_backup_meta.json` `generated_at` should match.

## 7. Phase 2 checklist -- stop committing the files to the repo

Do this only after step 6 has been clean for a few days. It is NOT done in this change; it touches ledger logic, so each item below needs its own change + test run.

**What currently depends on the repo copy**

| Dependency | Where | What phase 2 must do |
|---|---|---|
| The CI ledger merge reads origin's previous `picks_backup.json` / `_meta.json` via `git show origin/main:...` | `scripts/auto_lock_settle.py` `_read_origin_state()` (+ degraded-mode loader, `persist_ledger_backup`) | Read the previous copy from R2 (`BASE/picks_backup.json`) instead -- or keep committing the backup once a day / when `needs_reconcile` flips, as the durable git-history safety net. **This is the one that must not be rushed.** |
| Lock/settle gate reads the backup from the checkout | `scripts/settle_gate.py` | Fetch from R2 (or the daily snapshot) |
| Social cards / weekly digest fallback when Supabase is down | `scripts/generate_social_cards.py`, `scripts/weekly_health_digest.py`, `scripts/build_ledger_archive.py` | Read from R2 or the daily snapshot |
| Health checks | `scripts/daily_health_check.py` (live feed freshness reads `live_data.json`'s `ts`; ledger-archive staleness reads `picks_backup.json`) | Point at R2 |
| Writers that `git add` the files | `auto-lock-settle.yml` ("Commit ledger backup" step `paths:`), `live-tracker.yml`, `scheduled-refresh.yml`, `manual-sync.yml` (live_data), `scripts/clairvoyance_update.py` `git_push()` (local cron, `docs/live_data.json`) | Remove the files from `paths:`; set `r2-source: workdir` (so R2 gets this runner's file, not origin's) and `dispatch: never` for live data (the app reads R2 directly) |
| Lock-only passes commit the backup themselves | `persist_ledger_backup(..., commit=True)` -> `_commit_and_push` | Skip the commit, upload instead |
| Tests that read the repo copies | `test_degraded_ledger.py`, `test_ledger_archive.py`, `test_settle_gate.py`, `test_settle_pass_sim.py`, `test_landing_scope.py`, `test_health_freshness.py`, `test_lock_timing.py`, `smoke_all_tabs.py`, `demo_share.py` | Keep a small fixture/seed copy in the repo and point tests at it |
| The in-app fallback | `docs/app.html` `loadPicksFromBackupJSON()` falls back to `docs/picks_backup.json` | Leave a last-known-good copy committed (frozen or daily) so the offline fallback still has something |
| `docs/manual_locks.json` (51 commits/day in the sample) | written by the OWNER'S BROWSER through the GitHub contents API with your token (`app.html` manual-lock relay) | Not movable by this change: it needs a write endpoint (e.g. a tiny Worker in front of R2). Leave it in the repo for now. |
| `docs/automation_status.json`, `docs/scraper_health.json` | written by the lock/settle scripts and the fetch scripts | Next best candidates after the three files above (26 commits/day each in the sample); same recipe |

**Order of operations**: (1) switch ONE writer (live-tracker -- lowest risk, tolerant of a missed tick) and watch a game day; (2) change the ledger readers to R2 + keep a daily committed snapshot; (3) switch the ledger writers;
(4) update the tests. Each step is a separate commit so it can be reverted alone.

**Rollback of phase 2**: restore the `paths:` lines and `r2-source: origin` (git revert of the step's commit), set `CV_R2_BASE` back to `''`, then run Live Score Tracker / Auto Lock + Settle once so the repo copies are current again.
The repo's existing history (the thousands of old bot commits) stays; shrinking it needs a history rewrite (`git filter-repo`) which is a separate, deliberate decision.
