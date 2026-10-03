# Live-scores relay (European hockey)

A ~100-line Cloudflare Worker that gives the app **live scores for Liiga, SHL, National League and Extraliga**, which ESPN does not cover.
Flashscore's live feed works from a server but refuses browsers on other domains, so the worker fetches it, keeps only these four leagues and serves a small JSON with CORS.
Free tier is plenty: the response is edge-cached for 180 s (the app's live cycle), so one upstream request per 3 minutes however many people have the app open (Workers free plan = 100,000 requests/day).

## Deploy (one time, ~3 minutes)

1. Free account at https://dash.cloudflare.com (no card needed).
2. In a terminal:

   ```bash
   cd relay
   npx wrangler login      # opens the browser once
   npx wrangler deploy     # prints  https://clairvoyance-live-relay.<your-subdomain>.workers.dev
   ```

3. Check it: open `https://clairvoyance-live-relay.<your-subdomain>.workers.dev/hockey?tz=-6` -- you should see JSON with a `games` array.
4. Give the app the URL: in the app go to **Overall -> SYNC -> LIVE RELAY** and paste the URL (it is stored on that device), or tell Claude to bake it in as the default.

## What the app does with it

- Polls it only while a European hockey game is inside its live window (start - 2 min ... start + 3.5 h): every 180 s. No polling otherwise.
- Ticker + Top Picks show live scores with the period and elapsed minutes.
- The moment a game goes final, any pending pick on it is settled in the app (no waiting for the next results refresh).
- If the relay is down or not set, the app falls back to the schedule/results files (finals only, hours late).

## Files

- `worker.js` -- the worker and `parseFeed` (exported for tests)
- `wrangler.toml` -- name + allowed browser origins (`ALLOW_ORIGINS`; add your own domain if you host the app elsewhere)
- `test_parse.mjs` -- `node relay/test_parse.mjs` checks the parser against a saved real sample (`fixtures/feed_sample.txt`)

## Risk

The Flashscore feed is unofficial. If it changes, `/hockey` returns 502 and the app silently falls back. The `X-Fsign` header it needs is the public constant Flashscore's own website sends, not an account credential.
