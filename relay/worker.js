/**
 * Clairvoyance live-scores relay (Cloudflare Worker) -- European hockey: Liiga, SHL, National League, Extraliga.
 *
 * WHY: ESPN does not cover these leagues, and Flashscore's public live feed (the same one its website reads) answers a plain server request but its CORS preflight only allows
 * flashscore.com, so a browser on another domain cannot read it. This worker makes the request server-side, keeps only the four leagues, and re-serves a tiny JSON with CORS for the app.
 *
 *   GET /hockey?tz=-6   ->  { ts, tz, source, games: [ { lg, id, home, away, homeScore, awayScore, state, so, ot, regHome, regAway, period, periodStart, startMs } ] }
 *        state  : "pre" | "in" | "post"
 *        period : 1 | 2 | 3 | "OT" | "SO" | null   (live games only; from Flashscore's status code)
 *        periodStart : epoch seconds when the current period began (the app shows elapsed minutes from it)
 *        so / ot     : final decided in a shootout (AC 11) / overtime (AC 10); for a shootout regHome/regAway hold the score BEFORE it (hockey totals are settled on that, not on the final)
 *   GET /health         ->  { ok: true }
 *
 * One upstream request per tz per CACHE_SECONDS (180 s, the app's live cycle) no matter how many people have the app open (edge cache), so the load on Flashscore stays tiny.
 * The X-Fsign header value is the public constant Flashscore's own web client sends; it is not a secret and not an account credential.
 *
 * Risk: this is an unofficial feed. If Flashscore changes it, the worker returns 502 and the app falls back to the schedule files (finals only, hours late).
 */

const FEED = tz => `https://local-global.flashscore.ninja/2/x/feed/f_4_0_${tz}_en_1`;
const FSIGN = "SW9D1eZo";
const CACHE_SECONDS = 180;   // matches the app's 180 s live cycle: one upstream request per 3 minutes however many devices are open

// Flashscore league header -> app league tag
const LEAGUES = {
  "FINLAND: Liiga": "LIIGA",
  "SWEDEN: SHL": "SHL",
  "SWITZERLAND: National League": "NLA",
  "CZECH REPUBLIC: Extraliga": "EXTRALIGA",
};
// live status code (AC) while AB = 2: 14 / 15 / 16 = 1st / 2nd / 3rd period, 17 = overtime, 18 = shootout (codes verified live on 2026-10-03 for 14-16; 17/18 follow the sequence)
const PERIOD = { 14: 1, 15: 2, 16: 3, 17: "OT", 18: "SO" };

export function parseFeed(text) {
  const out = [];
  let lg = null;
  for (const rec of text.split("~")) {
    const kv = {};
    for (const part of rec.split("¬")) {
      const i = part.indexOf("÷");
      if (i > 0) kv[part.slice(0, i)] = part.slice(i + 1);
    }
    if (kv.ZA !== undefined) lg = LEAGUES[kv.ZA] || null;
    if (lg && kv.AA && kv.AE && kv.AF) {
      const num = v => (v === undefined || v === "" ? null : Number(v));
      const homeScore = num(kv.AG), awayScore = num(kv.AH);
      let state = kv.AB === "2" ? "in" : kv.AB === "3" ? "post" : "pre";
      // a "finished" record without a score is a postponed / cancelled / awarded game: never show it as a FINAL
      if (state === "post" && (homeScore === null || awayScore === null)) state = "pre";
      const so = state === "post" && kv.AC === "11";     // decided in a shootout: the final score INCLUDES the shootout-winning goal (sportsbooks settle totals without it)
      const ot = state === "post" && kv.AC === "10";     // decided in overtime (the OT goal counts)
      out.push({
        lg,
        id: kv.AA,
        home: kv.AE,
        away: kv.AF,
        homeScore,
        awayScore,
        state,
        so,
        ot,
        // AT / AU = the score before the shootout (regulation + overtime); only meaningful for a shootout final
        regHome: so ? num(kv.AT) : null,
        regAway: so ? num(kv.AU) : null,
        period: state === "in" ? (PERIOD[kv.AC] ?? null) : null,
        periodStart: state === "in" && kv.AO ? Number(kv.AO) : null,
        startMs: (Number(kv.AD) || 0) * 1000,
      });
    }
  }
  return out;
}

function corsHeaders(request, env) {
  const allowed = (env.ALLOW_ORIGINS || "https://purple-wraith.github.io,http://localhost:8765").split(",").map(s => s.trim());
  const origin = request.headers.get("Origin") || "";
  return {
    "Access-Control-Allow-Origin": allowed.includes(origin) ? origin : allowed[0],
    "Access-Control-Allow-Methods": "GET, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
    "Access-Control-Max-Age": "86400",
    Vary: "Origin",
  };
}

export default {
  async fetch(request, env, ctx) {
    const cors = corsHeaders(request, env);
    if (request.method === "OPTIONS") return new Response(null, { status: 204, headers: cors });
    const url = new URL(request.url);
    const json = (obj, status = 200, extra = {}) =>
      new Response(JSON.stringify(obj), { status, headers: { "Content-Type": "application/json; charset=utf-8", ...cors, ...extra } });

    if (url.pathname === "/health") return json({ ok: true });
    if (url.pathname !== "/hockey") return json({ error: "not found" }, 404);

    let tz = Number(url.searchParams.get("tz"));
    if (!Number.isFinite(tz) || tz < -12 || tz > 14) tz = -6;
    tz = Math.round(tz);

    const cache = caches.default;
    const cacheKey = new Request(`https://relay.invalid/hockey?tz=${tz}`);
    const hit = await cache.match(cacheKey);
    if (hit) {
      const body = await hit.text();
      return new Response(body, { status: hit.status, headers: { "Content-Type": "application/json; charset=utf-8", ...cors, "X-Cache": "HIT" } });
    }

    let upstream;
    try {
      upstream = await fetch(FEED(tz), { headers: { "X-Fsign": FSIGN, "User-Agent": "Mozilla/5.0 (compatible; ClairvoyanceRelay/1.0)" }, cf: { cacheTtl: 0 } });
    } catch (e) {
      return json({ error: "upstream unreachable" }, 502);
    }
    if (!upstream.ok) {
      // negative cache: while Flashscore is failing, answer from the edge for 30 s instead of hitting it again on every request
      const err = JSON.stringify({ error: "upstream " + upstream.status });
      ctx.waitUntil(cache.put(cacheKey, new Response(err, { status: 502, headers: { "Cache-Control": "public, max-age=30", "Content-Type": "application/json" } })));
      return json({ error: "upstream " + upstream.status }, 502);
    }
    const games = parseFeed(await upstream.text());
    const body = JSON.stringify({ ts: Date.now(), tz, source: "flashscore", games });
    ctx.waitUntil(cache.put(cacheKey, new Response(body, { headers: { "Cache-Control": `public, max-age=${CACHE_SECONDS}`, "Content-Type": "application/json" } })));
    return new Response(body, { status: 200, headers: { "Content-Type": "application/json; charset=utf-8", ...cors, "X-Cache": "MISS" } });
  },
};
