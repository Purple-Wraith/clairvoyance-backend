// Clairvoyance scheduler -- fires the DATA-REFRESH workflows on time.
//
// Why: GitHub's own `schedule:` triggers start 3-6 hours late (documented in this repo's lock-workflow headers), so a refresh that should land at
// 05:13 UTC lands mid-morning and the freshness checks have to be loose enough to tolerate it. A Cloudflare cron trigger is accurate to about a
// minute: this Worker wakes every TICK_MIN minutes, works out which refresh workflows are due in that slot, and starts them through the GitHub API
// (workflow_dispatch). The workflows' own `schedule:` entries stay in place as a fallback, so nothing is lost if this Worker is ever off.
//
// Deliberately NOT here as fixed-time slots: the lock / settle / email workflows. Their scheduled runs choose behaviour from `github.event.schedule`, and a dispatched run
// does not carry it; they are left on GitHub's cron plus the watchdog. The ONE exception is the kickoff-driven END-OF-GAME SETTLE sweep below, which dispatches
// auto-lock-settle.yml with explicit inputs (mode=settle, live=true -- what the header's SETTLE NOW button sends), so it needs no `github.event.schedule`. The WATCHDOG itself is here (evening slots only): it does not read `github.event.schedule`,
// and `gated=true` gives a dispatched run the scheduled behaviour.
//
// Needs one secret: GH_TOKEN, a fine-grained token limited to this repo with "Actions: Read and write". Without it the Worker only logs.

export const REPO = "Purple-Wraith/clairvoyance-backend";
export const TICK_MIN = 10;          // must match the cron expression in wrangler.toml ("*/10 * * * *")

// UTC schedule, mirrored from each workflow's own `schedule:` block. dow: 0=Sunday..6=Saturday; omitted = every day.
export const SCHEDULE = [
  { wf: "scheduled-refresh.yml", at: ["05:13", "15:13", "21:13"] },
  { wf: "hockey-euro-refresh.yml", at: ["08:10", "12:10", "21:00"] },
  { wf: "daily-player-stats-refresh.yml", at: ["15:45"] },
  { wf: "soccer-refresh.yml", at: ["03:50"], inputs: { which: "tomorrow" } },
  { wf: "soccer-refresh.yml", at: ["11:33"], inputs: { which: "opta" } },
  { wf: "cfb-refresh.yml", at: ["14:33"] },
  { wf: "cfb-refresh.yml", at: ["02:17"], dow: [1] },
  { wf: "nfl-weekly-refresh.yml", at: ["13:53"], dow: [2] },
  // Evening lock coverage (2026-10-07): the pre-kickoff watchdog auto-locks any qualifying leg 10-150 min before kickoff. gated=true makes the dispatched run behave exactly
  // like a scheduled slot (LIVE_MODE variable + kickoff gate); without it a dispatch is the manual dry-run path. 15:30 / 17:30 / 19:30 MT.
  { wf: "lock-watchdog.yml", at: ["21:30", "23:30", "01:30"], inputs: { gated: "true" } },
  // PRE-DROP SWEEPS (2026-10-09, owner: "so no games in the evening are missed"): on 2026-10-08 the 3-hourly slots left a gap right before the 7 PM ET puck drops -- seven legs qualified on the last
  // odds readings and were only seen 30-155 min AFTER the games started (the auto-lock refuses started games). One run ~40 min before each common evening start (NHL/NBA 23:00, 23:30, 00:00, 00:30,
  // 01:00, 02:00, 02:30 UTC) refreshes the odds, re-grades the slate and auto-locks whatever now qualifies, with the 10-minute start guard still in force. Worker-only (workerOnly): GitHub's own cron
  // would land hours late, so these have no fallback entry in the workflow; the 3-hourly slots above remain the fallback.
  { wf: "lock-watchdog.yml", at: ["22:20", "22:50", "23:20", "23:50", "00:20", "01:20", "01:50"], inputs: { gated: "true" }, workerOnly: true },
];

const slotOf = (hhmm) => { const [h, m] = hhmm.split(":").map(Number); return Math.floor((h * 60 + m) / TICK_MIN); };

/** Schedule entries ({wf, inputs?}) due in the tick that contains `when` (a Date or epoch ms). */
export function dueEntries(when) {
  const d = new Date(when);
  const slot = Math.floor((d.getUTCHours() * 60 + d.getUTCMinutes()) / TICK_MIN);
  const dow = d.getUTCDay();
  return SCHEDULE.filter((s) => (!s.dow || s.dow.includes(dow)) && s.at.some((t) => slotOf(t) === slot));
}

/** Workflow files due in that tick (names only, de-duplicated). */
export function dueWorkflows(when) {
  return [...new Set(dueEntries(when).map((s) => s.wf))];
}

// ── Kickoff-aware pre-drop sweeps ────────────────────────────────────────────────────────────────────────────────────────────────────
// docs/kickoffs.json (scripts/build_kickoffs.py, generated into every Pages deploy) lists every upcoming game start the engine locks, in any sport. About SWEEP_LEAD_MIN minutes before EACH start the
// Worker runs the pre-kickoff watchdog (gated=true: the same gate / LIVE_MODE / auto-lock behaviour as a scheduled slot), so a pick that only qualifies on the last odds readings is locked before the
// game -- evening NHL/NBA, early-morning European soccer and hockey, Saturday CFB, Sunday NFL -- with no hand-kept list of times. If the file cannot be read, the fixed sweep times in SCHEDULE still run.
// (the app + this file are served by GitHub Pages at purple-wraith.github.io/clairvoyance-backend; clairvoyanceengine.info does not point at this site)
export const KICKOFFS_URL = "https://purple-wraith.github.io/clairvoyance-backend/kickoffs.json";
export const SWEEP_LEAD_MIN = 40;          // the 10-minute tick that contains (start - 40 min) fires, so a sweep runs 40-50 min before the start; the lock guard still refuses anything inside 10 min
export const SWEEP_ENTRY = { wf: "lock-watchdog.yml", inputs: { gated: "true" } };

/** Starts ({t: ISO, sports}) whose sweep falls in the tick that contains `when`. */
export function sweepsDue(when, starts, leadMin = SWEEP_LEAD_MIN) {
  const tickMs = TICK_MIN * 60000, slot = Math.floor(new Date(when).getTime() / tickMs);
  return (starts || []).filter((x) => { const t = Date.parse(x && (x.t || x)); return Number.isFinite(t) && Math.floor((t - leadMin * 60000) / tickMs) === slot; });
}

/** kickoffs.json -> {starts, games}. `starts` feeds the pre-kickoff lock sweeps, `games` ([{t, sport}], any state, last 8 h + next 72 h) the end-of-game settle sweeps. An older file without `games` just has none. */
async function loadKickoffs(fetchImpl) {
  try {
    const r = await fetchImpl(KICKOFFS_URL, { cf: { cacheTtl: 240 } });
    if (!r.ok) return { starts: [], games: [] };
    const j = await r.json();
    return { starts: Array.isArray(j && j.starts) ? j.starts : [], games: Array.isArray(j && j.games) ? j.games : [] };
  } catch (e) {
    return { starts: [], games: [] };
  }
}
async function loadStarts(fetchImpl) { return (await loadKickoffs(fetchImpl)).starts; }

// ── End-of-game settle sweeps ────────────────────────────────────────────────────────────────────────────────────────────────────────
// GitHub's own settle crons land hours late, so a result can sit unsettled for most of a day. kickoffs.json (`games`) lists each game's start and sport; this adds a per-sport typical game length to get
// an EXPECTED END and dispatches auto-lock-settle.yml (mode=settle, live=true -- the same fixed inputs as the header's SETTLE NOW button) in the first tick at/after that moment, then again ~20 and ~45 minutes later
// for overtime / shootouts / delays. Stateless: everything is derived from `now` against kickoff + duration + offset, exactly like sweepsDue. The workflow's own settle crons stay as the fallback.
//
// Minutes from start to the final whistle. Deliberately CONSERVATIVE typical lengths (a normal game is a little shorter, so the first check usually finds the game just over; a long one is caught by the
// follow-ups). Keyed by the sport tags build_kickoffs.py writes. Unknown tag -> DEFAULT_GAME_MIN.
export const GAME_MINUTES = {
  NHL: 165,                                                   // 2h45
  NBA: 150,                                                   // 2h30
  NFL: 200,                                                   // 3h20
  CFB: 225,                                                   // 3h45
  PL: 115, LIGA: 115, SERIEA: 115, CL: 115,                   // soccer ~1h55 (90 + stoppage + half-time); Champions League extra time is what the follow-ups are for
  SHL: 155, LIIGA: 155, NLA: 155, EXTRALIGA: 155,             // European hockey ~2h35 (three periods + intermissions)
};
export const DEFAULT_GAME_MIN = 180;
export const SETTLE_OFFSETS_MIN = [0, 20, 45];                // minutes after the expected end: first check, then two follow-ups
export const SETTLE_ENTRY = { wf: "auto-lock-settle.yml", inputs: { mode: "settle", live: "true" } };   // identical to TRIGGERS.settle (declared below) -- a test pins that

/** Epoch ms of a game's expected end (kickoff + the sport's typical length), or NaN when the start is unreadable. */
export function expectedEndMs(game) {
  const t = Date.parse(game && game.t);
  return t + (GAME_MINUTES[String(game && game.sport).toUpperCase()] ?? DEFAULT_GAME_MIN) * 60000;
}

/**
 * Settle checks due in the tick that contains `when`: [{t, sport, endsAt, offsetMin}], one row per (game, offset). A check at expected-end + offset X runs in the first tick whose start is at or after X, so it lands
 * 0-10 minutes AFTER that moment, never before it. Many rows in one tick still mean ONE dispatch (the caller collapses them).
 */
export function settleSweepsDue(when, games, offsets = SETTLE_OFFSETS_MIN) {
  const tickMs = TICK_MIN * 60000, slot = Math.floor(new Date(when).getTime() / tickMs), out = [];
  for (const g of games || []) {
    const end = expectedEndMs(g);
    if (!Number.isFinite(end)) continue;
    for (const off of offsets) if (Math.ceil((end + off * 60000) / tickMs) === slot) out.push({ t: g.t, sport: g.sport, endsAt: new Date(end).toISOString(), offsetMin: off });
  }
  return out;
}

/** The next settle sweeps after `now`, tick by tick (status page + tests): [{at, forGames:[{t, sport, endsAt, offsetMin}], sports}]. */
export function upcomingSettleSweeps(now, games, { max = 10, horizonHours = 48 } = {}) {
  const tickMs = TICK_MIN * 60000, out = [];
  for (let slot = Math.ceil(now / tickMs); slot * tickMs <= now + horizonHours * 3600000 && out.length < max; slot++) {
    const due = settleSweepsDue(slot * tickMs, games);
    if (due.length) out.push({ at: new Date(slot * tickMs).toISOString(), forGames: due, sports: [...new Set(due.map((d) => d.sport))].sort() });
  }
  return out;
}

/** "2026-10-10 05:21 AM MT" for an epoch ms / ISO string (the owner reads Mountain time; the Worker answers in UTC AND this). */
export function mountain(when) {
  const p = Object.fromEntries(new Intl.DateTimeFormat("en-US", { timeZone: "America/Denver", year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: true }).formatToParts(new Date(when)).map((x) => [x.type, x.value]));
  return `${p.year}-${p.month}-${p.day} ${p.hour}:${p.minute} ${String(p.dayPeriod).toUpperCase()} MT`;
}

async function gh(env, path, init = {}, fetchImpl = fetch) {
  return fetchImpl(`https://api.github.com/repos/${REPO}${path}`, {
    ...init,
    headers: {
      Authorization: `Bearer ${env.GH_TOKEN}`, Accept: "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
      "User-Agent": "clairvoyance-scheduler", ...(init.headers || {}),
    },
  });
}

/** True when a run of this workflow is already queued / in progress (a manual or GitHub-cron run beat us to it). */
async function alreadyRunning(env, wf, fetchImpl) {
  for (const status of ["in_progress", "queued"]) {
    const r = await gh(env, `/actions/workflows/${wf}/runs?status=${status}&per_page=1`, {}, fetchImpl);
    if (!r.ok) throw new Error(`runs lookup ${wf} -> HTTP ${r.status}`);
    if ((await r.json()).total_count > 0) return true;
  }
  return false;
}

/** True when ANY auto-lock-settle run (cron, manual or ours) is queued / in progress. One cheap call. FAIL-OPEN: any API or network problem answers false -- a missed settle is worse than a duplicate one. */
async function settleRunActive(env, fetchImpl) {
  try {
    const r = await gh(env, `/actions/workflows/${SETTLE_ENTRY.wf}/runs?per_page=10`, {}, fetchImpl);
    if (!r.ok) return false;
    return ((await r.json()).workflow_runs || []).some((x) => x && x.status && x.status !== "completed");
  } catch (e) {
    return false;
  }
}

/** The end-of-game settle sweep for the tick containing `when`: at most ONE dispatch however many games ended. Returns a result row, or null when nothing is due. */
async function runSettleSweep(env, when, games, fetchImpl) {
  const due = settleSweepsDue(when, games);
  if (!due.length) return null;
  const row = { wf: SETTLE_ENTRY.wf, games: due.length, sports: [...new Set(due.map((d) => d.sport))].sort(), offsets: [...new Set(due.map((d) => d.offsetMin))].sort((a, b) => a - b) };
  try {
    if (await settleRunActive(env, fetchImpl)) return { ...row, result: "skipped: already queued or running" };
    const r = await gh(env, `/actions/workflows/${SETTLE_ENTRY.wf}/dispatches`, { method: "POST", body: JSON.stringify({ ref: "main", inputs: SETTLE_ENTRY.inputs }) }, fetchImpl);
    return { ...row, result: r.status === 204 ? "dispatched" : `error: HTTP ${r.status}` };
  } catch (e) {
    return { ...row, result: `error: ${e.message}` };
  }
}

/** Starts every workflow due at `when` (plus the end-of-game settle sweep). Returns [{wf, result}] where result is "dispatched" | "skipped: ..." | "error: ...". */
export async function runTick(env, when, fetchImpl = fetch) {
  const out = [];
  const entries = dueEntries(when).slice();
  let games = [];
  if (env.GH_TOKEN) {
    const kick = await loadKickoffs(fetchImpl);
    games = kick.games;
    const due = sweepsDue(when, kick.starts);
    const have = entries.some((e) => e.wf === SWEEP_ENTRY.wf && JSON.stringify(e.inputs || {}) === JSON.stringify(SWEEP_ENTRY.inputs));
    if (due.length && !have) entries.push({ ...SWEEP_ENTRY, kickoff: due.map((x) => x.t).join(",") });
  }
  for (const { wf, inputs } of entries) {
    if (!env.GH_TOKEN) { out.push({ wf, result: "skipped: GH_TOKEN secret not set" }); continue; }
    try {
      if (await alreadyRunning(env, wf, fetchImpl)) { out.push({ wf, result: "skipped: already queued or running" }); continue; }
      const r = await gh(env, `/actions/workflows/${wf}/dispatches`, { method: "POST", body: JSON.stringify(inputs ? { ref: "main", inputs } : { ref: "main" }) }, fetchImpl);
      out.push({ wf, result: r.status === 204 ? "dispatched" : `error: HTTP ${r.status}` });
    } catch (e) {
      out.push({ wf, result: `error: ${e.message}` });
    }
  }
  const settle = env.GH_TOKEN ? await runSettleSweep(env, when, games, fetchImpl) : null;     // no token -> no network at all
  if (settle) out.push(settle);
  return out;
}

// ── Owner trigger (POST /trigger) ────────────────────────────────────────────────────────────────────────────────────────────────────
// The app header's "LOCK NOW" / "SETTLE NOW" buttons call this. The GitHub token never leaves the Worker; the browser sends a shared secret (TRIGGER_KEY, a Worker secret you set
// once: `npx wrangler secret put TRIGGER_KEY`). Only two fixed actions exist and each maps to a fixed workflow + inputs -- nothing the caller sends is passed through to GitHub.
export const TRIGGERS = {
  lock: { wf: "auto-lock-settle.yml", inputs: { mode: "lock", live: "true" }, label: "LOCK" },        // locks every qualifying pick that has not started (no subscriber email -- that is the workflow's own 'lock' mode)
  settle: { wf: "auto-lock-settle.yml", inputs: { mode: "settle", live: "true" }, label: "SETTLE" },
};
export const ALLOWED_ORIGINS = ["https://clairvoyanceengine.info", "https://www.clairvoyanceengine.info", "https://purple-wraith.github.io", "http://localhost:8000", "http://127.0.0.1:8000", "http://localhost:8765", "http://127.0.0.1:8765"];
const corsFor = (origin) => (ALLOWED_ORIGINS.includes(origin) ? { "access-control-allow-origin": origin, "vary": "origin", "access-control-allow-methods": "GET, PUT, POST, OPTIONS", "access-control-allow-headers": "content-type, x-owner-key" } : { "vary": "origin" });
const json = (obj, status, extra = {}) => new Response(JSON.stringify(obj), { status, headers: { "content-type": "application/json", ...extra } });

/** Constant-time string comparison (equal length or not, the loop runs over the longer one). */
export function safeEqual(a, b) {
  a = String(a ?? ""); b = String(b ?? "");
  let diff = a.length ^ b.length;
  for (let i = 0; i < Math.max(a.length, b.length); i++) diff |= (a.charCodeAt(i) || 0) ^ (b.charCodeAt(i) || 0);
  return diff === 0;
}

/** True when a manually dispatched run of this workflow is already waiting or running (stops double taps / spam). */
async function manualRunActive(env, wf, fetchImpl) {
  const r = await gh(env, `/actions/workflows/${wf}/runs?event=workflow_dispatch&per_page=5`, {}, fetchImpl);
  if (!r.ok) throw new Error(`runs lookup ${wf} -> HTTP ${r.status}`);
  const runs = (await r.json()).workflow_runs || [];
  return runs.some((x) => ["queued", "in_progress", "waiting", "pending", "requested"].includes(x.status));
}

export async function handleTrigger(request, env, fetchImpl = fetch, sleep = (ms) => new Promise((r) => setTimeout(r, ms))) {
  const cors = corsFor(request.headers.get("origin") || "");
  if (request.method === "OPTIONS") return new Response(null, { status: 204, headers: cors });
  if (request.method !== "POST") return json({ error: "POST only" }, 405, cors);
  let body;
  try { body = await request.json(); } catch { return json({ error: "bad request" }, 400, cors); }
  if (!env.TRIGGER_KEY) return json({ error: "trigger key not configured on the Worker (wrangler secret put TRIGGER_KEY)" }, 503, cors);
  if (!safeEqual(body && body.key, env.TRIGGER_KEY)) { await sleep(500); return json({ error: "wrong key" }, 401, cors); }   // the pause slows key guessing
  const t = TRIGGERS[body.action];
  if (!t) return json({ error: "unknown action" }, 400, cors);
  if (!env.GH_TOKEN) return json({ error: "GH_TOKEN secret not set on the Worker" }, 503, cors);
  try {
    if (await manualRunActive(env, t.wf, fetchImpl)) return json({ error: `a manual ${t.label} run is already queued or running` }, 409, cors);
    const r = await gh(env, `/actions/workflows/${t.wf}/dispatches`, { method: "POST", body: JSON.stringify({ ref: "main", inputs: t.inputs }) }, fetchImpl);
    if (r.status !== 204) return json({ error: `GitHub refused the dispatch (HTTP ${r.status})` }, 502, cors);
    return json({ ok: true, action: body.action, message: `${t.label} started` }, 200, cors);
  } catch (e) {
    return json({ error: `dispatch failed: ${e.message}` }, 502, cors);
  }
}

// ── Owner profit-tracker sync (GET /profit, PUT /profit) ─────────────────────────────────────────────────────────────────────────────
// The app's PROFIT tab keeps the owner's private bankroll ledger in the browser; this optional route lets phone + desktop share it. ONE JSON blob lives in the Workers KV namespace bound as PROFIT
// (`npx wrangler kv namespace create PROFIT`, see wrangler.toml). Auth = the same TRIGGER_KEY the header LOCK/SETTLE buttons use, sent as the `x-owner-key` header and compared in constant time.
// The Worker does NOT merge: the client sends {entries, tombstones, baseRev}; the Worker stores them as rev+1 only when baseRev is the stored rev, otherwise it answers 409 with the stored blob so
// the client can re-pull, merge (union by id, newest ts wins, tombstones beat older entries) and retry. Nothing here ever touches Supabase or any public file.
export const PROFIT_KEY = "ledger";
export const PROFIT_MAX_BYTES = 2_000_000;       // a blob this size is ~10k entries; far beyond real use, small enough to reject junk
const profitEmpty = () => ({ entries: [], tombstones: [], rev: 0 });
const profitOk = (a, max) => Array.isArray(a) && a.length <= max && a.every((x) => x && typeof x === "object" && !Array.isArray(x) && typeof x.id === "string" && x.id.length > 0 && x.id.length <= 80 && Number.isFinite(x.ts));

export async function handleProfit(request, env, sleep = (ms) => new Promise((r) => setTimeout(r, ms))) {
  const cors = { ...corsFor(request.headers.get("origin") || ""), "cache-control": "no-store" };
  if (request.method === "OPTIONS") return new Response(null, { status: 204, headers: cors });
  if (request.method !== "GET" && request.method !== "PUT") return json({ error: "GET or PUT only" }, 405, cors);
  if (!env.PROFIT || typeof env.PROFIT.get !== "function") return json({ error: "sync not configured" }, 503, cors);
  if (!env.TRIGGER_KEY) return json({ error: "trigger key not configured on the Worker (wrangler secret put TRIGGER_KEY)" }, 503, cors);
  if (!safeEqual(request.headers.get("x-owner-key"), env.TRIGGER_KEY)) { await sleep(500); return json({ error: "wrong key" }, 401, cors); }   // the pause slows key guessing
  let cur;
  try { cur = (await env.PROFIT.get(PROFIT_KEY, "json")) || profitEmpty(); } catch (e) { return json({ error: "storage read failed" }, 502, cors); }
  if (request.method === "GET") return json(cur, 200, cors);
  let text;
  try { text = await request.text(); } catch { return json({ error: "bad request" }, 400, cors); }
  if (text.length > PROFIT_MAX_BYTES) return json({ error: "too large" }, 413, cors);
  let body;
  try { body = JSON.parse(text); } catch { return json({ error: "bad request" }, 400, cors); }
  if (!body || !profitOk(body.entries, 20000) || !profitOk(body.tombstones, 40000) || !Number.isInteger(body.baseRev) || body.baseRev < 0) return json({ error: "bad request" }, 400, cors);
  if (body.baseRev !== (cur.rev || 0)) return json({ ...cur, error: "rev mismatch" }, 409, cors);   // the caller re-pulls (this body IS the stored blob), merges, retries once
  const next = { entries: body.entries, tombstones: body.tombstones, rev: (cur.rev || 0) + 1, updated: Date.now() };
  try { await env.PROFIT.put(PROFIT_KEY, JSON.stringify(next)); } catch (e) { return json({ error: "storage write failed" }, 502, cors); }
  return json(next, 200, cors);
}

export default {
  async scheduled(event, env, ctx) {
    const results = await runTick(env, event.scheduledTime);
    for (const r of results) console.log(`${r.wf}: ${r.result}`);
    const failed = results.filter((r) => r.result.startsWith("error"));
    if (failed.length) throw new Error("scheduler: " + failed.map((f) => `${f.wf} ${f.result}`).join("; "));   // shows as a failed invocation in the dashboard
  },
  // GET / -> what the next ticks would start (no secrets, no side effects); handy for checking the table.
  async fetch(request, env) {
    const path = new URL(request.url).pathname;
    if (path === "/trigger") return handleTrigger(request, env || {});
    if (path === "/profit") return handleProfit(request, env || {});
    const now = Date.now();
    const { starts, games } = await loadKickoffs(fetch);
    const kickoffSweeps = [];
    for (let i = 0; i < 6 * 24 * 2 && kickoffSweeps.length < 10; i++) {          // next 48 h of kickoff-aware sweeps
      const t = now + i * TICK_MIN * 60000, due = sweepsDue(t, starts);
      if (due.length) kickoffSweeps.push({ at: new Date(t).toISOString(), atMT: mountain(t), forStarts: due.map((x) => x.t), sports: [...new Set(due.flatMap((x) => x.sports || []))] });
    }
    const settleSweeps = upcomingSettleSweeps(now, games).map((x) => ({ ...x, atMT: mountain(x.at), forGames: x.forGames.map((g) => ({ ...g, endsAtMT: mountain(g.endsAt) })) }));
    const next = [];
    for (let i = 0; i < 6 * 24 * 2; i++) {            // next 48 h, tick by tick
      const t = now + i * TICK_MIN * 60000;
      const due = dueWorkflows(t);
      if (due.length) next.push({ at: new Date(t).toISOString(), due });
      if (next.length >= 10) break;
    }
    return new Response(JSON.stringify({ tickMinutes: TICK_MIN, next, kickoffStarts: starts.length, kickoffSweeps, settleSweeps, settleGames: games.length, settleRule: { gameMinutes: GAME_MINUTES, defaultMinutes: DEFAULT_GAME_MIN, checksAtMinutesAfterExpectedEnd: SETTLE_OFFSETS_MIN, dispatch: SETTLE_ENTRY.inputs } }, null, 2), { headers: { "content-type": "application/json" } });
  },
};
