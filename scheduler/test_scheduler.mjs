// node scheduler/test_scheduler.mjs -- offline: slot matching, weekday filters, dedupe, dispatch, failure reporting (GitHub API faked)
import assert from "node:assert/strict";
import worker, { dueWorkflows, dueEntries, runTick, SCHEDULE, TICK_MIN, handleTrigger, handleProfit, safeEqual, TRIGGERS, ALLOWED_ORIGINS, sweepsDue, KICKOFFS_URL, SWEEP_LEAD_MIN, GAME_MINUTES, DEFAULT_GAME_MIN, SETTLE_OFFSETS_MIN, SETTLE_ENTRY, expectedEndMs, settleSweepsDue, upcomingSettleSweeps, mountain } from "./worker.js";
import { readFileSync, readdirSync } from "node:fs";

const at = (iso) => Date.parse(iso);
assert.deepEqual(dueWorkflows(at("2026-10-05T05:13:30Z")), ["scheduled-refresh.yml"]);
assert.deepEqual(dueWorkflows(at("2026-10-05T05:19:59Z")), ["scheduled-refresh.yml"]);          // same 10-minute slot
assert.deepEqual(dueWorkflows(at("2026-10-05T05:20:00Z")), []);
assert.deepEqual(dueWorkflows(at("2026-10-05T12:20:00Z")), []);   // Monday 12:20-12:29 slot: nothing (the old NFL roster slot was merged away)
// weekday filters: nfl-weekly-refresh is Tuesday only (2026-10-06); the Sunday-evening CFB stats run (02:17 UTC) is Monday UTC only
assert.ok(dueWorkflows(at("2026-10-06T13:53:00Z")).includes("nfl-weekly-refresh.yml"));            // a Tuesday
assert.ok(!dueWorkflows(at("2026-10-05T13:53:00Z")).includes("nfl-weekly-refresh.yml"));           // a Monday
assert.ok(dueWorkflows(at("2026-10-05T02:17:00Z")).includes("cfb-refresh.yml"));                   // a Monday
assert.ok(!dueWorkflows(at("2026-10-06T02:17:00Z")).includes("cfb-refresh.yml"));                  // a Tuesday
// the two soccer jobs share one workflow and are told apart by an input
assert.deepEqual(dueEntries(at("2026-10-05T03:50:00Z")), [{ wf: "soccer-refresh.yml", at: ["03:50"], inputs: { which: "tomorrow" } }]);
assert.deepEqual(dueEntries(at("2026-10-05T11:33:00Z")).map((e) => e.inputs), [{ which: "opta" }]);

// every scheduled workflow file must exist, and its own cron must include the same time (the table is a mirror, not a second opinion)
const wfDir = new URL("../.github/workflows/", import.meta.url);
const files = new Set(readdirSync(wfDir));
for (const s of SCHEDULE) {
  assert.ok(files.has(s.wf), `${s.wf} does not exist`);
  const text = readFileSync(new URL(s.wf, wfDir), "utf8");
  assert.match(text, /workflow_dispatch/, `${s.wf} cannot be dispatched`);
  const crons = [...text.matchAll(/cron:\s*'(\d+) (\d+) \* \* ([^']+)'/g)].map((m) => `${String(m[2]).padStart(2, "0")}:${String(m[1]).padStart(2, "0")}`);
  if (!s.workerOnly) for (const t of s.at) assert.ok(crons.includes(t), `${s.wf}: ${t} UTC is not one of its own cron times (${crons.join(", ")})`);   // workerOnly slots deliberately have no GitHub cron twin
}

// fake GitHub
function fakeGitHub({ running = [], dispatchStatus = 204, lookupStatus = 200 } = {}) {
  const calls = [];
  const fetchImpl = async (url, init = {}) => {
    calls.push({ url, method: init.method || "GET", body: init.body });
    if (url.includes("/runs?")) {
      const wf = url.match(/workflows\/([^/]+)\/runs/)[1];
      return { ok: lookupStatus === 200, status: lookupStatus, json: async () => ({ total_count: running.includes(wf) && url.includes("in_progress") ? 1 : 0 }) };
    }
    return { ok: true, status: dispatchStatus, json: async () => ({}) };
  };
  return { fetchImpl, calls };
}
const T = at("2026-10-05T05:13:00Z");
let g = fakeGitHub();
assert.deepEqual(await runTick({ GH_TOKEN: "t" }, T, g.fetchImpl), [{ wf: "scheduled-refresh.yml", result: "dispatched" }]);
assert.ok(g.calls.some((c) => c.method === "POST" && c.url.endsWith("/workflows/scheduled-refresh.yml/dispatches") && JSON.parse(c.body).ref === "main"));
g = fakeGitHub({ running: ["scheduled-refresh.yml"] });
assert.match((await runTick({ GH_TOKEN: "t" }, T, g.fetchImpl))[0].result, /already queued or running/);
assert.ok(!g.calls.some((c) => c.method === "POST"), "must not dispatch on top of a running one");
g = fakeGitHub({ dispatchStatus: 403 });
assert.equal((await runTick({ GH_TOKEN: "t" }, T, g.fetchImpl))[0].result, "error: HTTP 403");
g = fakeGitHub({ lookupStatus: 500 });
assert.match((await runTick({ GH_TOKEN: "t" }, T, g.fetchImpl))[0].result, /^error:/);
g = fakeGitHub();
assert.match((await runTick({}, T, g.fetchImpl))[0].result, /GH_TOKEN secret not set/);
assert.equal(g.calls.length, 0, "no token -> no network calls");
assert.deepEqual(await runTick({ GH_TOKEN: "t" }, at("2026-10-05T00:01:00Z"), g.fetchImpl), []);   // nothing due
g = fakeGitHub();
await runTick({ GH_TOKEN: "t" }, at("2026-10-05T03:50:00Z"), g.fetchImpl);
assert.deepEqual(JSON.parse(g.calls.find((c) => c.method === "POST").body), { ref: "main", inputs: { which: "tomorrow" } });
// the evening watchdog slots fire on the dot, gated (scheduled-run behaviour), and the workflow accepts that input
assert.deepEqual(dueEntries(at("2026-10-07T23:30:00Z")).map((e) => [e.wf, e.inputs]), [["lock-watchdog.yml", { gated: "true" }]]);
assert.ok(dueWorkflows(at("2026-10-08T01:30:00Z")).includes("lock-watchdog.yml") && dueWorkflows(at("2026-10-07T21:30:00Z")).includes("lock-watchdog.yml"));
assert.match(readFileSync(new URL("lock-watchdog.yml", wfDir), "utf8"), /gated:[\s\S]*type: boolean/);
// pre-drop sweeps: one tick ~40 min before each common evening start, dispatched gated, worker-only
for (const hhmm of ["22:20", "22:50", "23:20", "23:50", "00:20", "01:20", "01:50"]) {
  const iso = `2026-10-08T${hhmm}:30Z`;
  const when = hhmm < "10:00" ? iso.replace("2026-10-08", "2026-10-09") : iso;
  assert.ok(dueWorkflows(at(when)).includes("lock-watchdog.yml"), `${hhmm} should dispatch the watchdog`);
}
assert.ok(SCHEDULE.filter((e) => e.wf === "lock-watchdog.yml" && e.workerOnly).every((e) => e.inputs && e.inputs.gated === "true"));
assert.equal(TICK_MIN, 10);

// ── owner trigger (POST /trigger) ──
const req = (body, { method = "POST", origin = "https://clairvoyanceengine.info" } = {}) =>
  new Request("https://w.example/trigger", { method, headers: { "content-type": "application/json", origin }, body: method === "POST" ? JSON.stringify(body) : undefined });
const env = { GH_TOKEN: "t", TRIGGER_KEY: "k".repeat(32) };
const nosleep = async () => {};
const ghFake = ({ active = false, dispatch = 204 } = {}) => {
  const calls = [];
  return { calls, fetchImpl: async (url, init = {}) => {
    calls.push({ url, method: init.method || "GET", body: init.body });
    if (url.includes("/runs?")) return { ok: true, status: 200, json: async () => ({ workflow_runs: active ? [{ status: "in_progress" }] : [{ status: "completed" }] }) };
    return { ok: true, status: dispatch, json: async () => ({}) };
  } };
};
assert.ok(safeEqual("abc", "abc") && !safeEqual("abc", "abd") && !safeEqual("abc", "abcd") && !safeEqual("", "x") && !safeEqual(undefined, "x"));
assert.deepEqual(Object.keys(TRIGGERS).sort(), ["lock", "settle"]);
let r = await handleTrigger(req({ action: "lock", key: "wrong" }), env, ghFake().fetchImpl, nosleep);
assert.equal(r.status, 401);
r = await handleTrigger(req({ action: "lock", key: env.TRIGGER_KEY }), { GH_TOKEN: "t" }, ghFake().fetchImpl, nosleep);
assert.equal(r.status, 503);                                                                           // key secret not set yet
r = await handleTrigger(req({ action: "lock", key: env.TRIGGER_KEY }), { TRIGGER_KEY: env.TRIGGER_KEY }, ghFake().fetchImpl, nosleep);
assert.equal(r.status, 503);                                                                           // GH token missing
r = await handleTrigger(req({ action: "reboot", key: env.TRIGGER_KEY }), env, ghFake().fetchImpl, nosleep);
assert.equal(r.status, 400);                                                                           // only the two fixed actions
for (const [action, mode] of [["lock", "lock"], ["settle", "settle"]]) {
  const f = ghFake();
  r = await handleTrigger(req({ action, key: env.TRIGGER_KEY, inputs: { mode: "digest" }, wf: "evil.yml" }), env, f.fetchImpl, nosleep);
  assert.equal(r.status, 200);
  const post = f.calls.find((c) => c.method === "POST");
  assert.ok(post.url.endsWith("/workflows/auto-lock-settle.yml/dispatches"));
  assert.deepEqual(JSON.parse(post.body), { ref: "main", inputs: { mode, live: "true" } });             // nothing the caller sends is passed through
}
r = await handleTrigger(req({ action: "lock", key: env.TRIGGER_KEY }), env, ghFake({ active: true }).fetchImpl, nosleep);
assert.equal(r.status, 409);
r = await handleTrigger(req({ action: "lock", key: env.TRIGGER_KEY }), env, ghFake({ dispatch: 403 }).fetchImpl, nosleep);
assert.equal(r.status, 502);
r = await handleTrigger(req({}, { method: "OPTIONS" }), env, ghFake().fetchImpl, nosleep);
assert.equal(r.status, 204);
assert.equal(r.headers.get("access-control-allow-origin"), "https://clairvoyanceengine.info");
r = await handleTrigger(req({ action: "lock", key: "x" }, { origin: "https://evil.example" }), env, ghFake().fetchImpl, nosleep);
assert.equal(r.headers.get("access-control-allow-origin"), null);                                      // browsers on other sites cannot read the answer
r = await handleTrigger(req(null, { method: "GET" }), env, ghFake().fetchImpl, nosleep);
assert.equal(r.status, 405);
assert.ok(ALLOWED_ORIGINS.includes("https://clairvoyanceengine.info") && ALLOWED_ORIGINS.includes("https://purple-wraith.github.io"));
// ── kickoff-aware sweeps ──
assert.equal(SWEEP_LEAD_MIN, 40);
const starts = [{ t: "2026-10-10T11:30Z", sports: ["PL"] }, { t: "2026-10-10T16:00Z", sports: ["CFB"] }, { t: "2026-10-10T16:05Z", sports: ["CFB"] }];
// PL start 11:30Z -> sweep tick contains 10:50Z = the 10:50-10:59 tick
assert.equal(sweepsDue(at("2026-10-10T10:50:30Z"), starts).length, 1);
assert.equal(sweepsDue(at("2026-10-10T10:59:59Z"), starts).length, 1);
assert.equal(sweepsDue(at("2026-10-10T10:40:00Z"), starts).length, 0);
assert.equal(sweepsDue(at("2026-10-10T11:00:00Z"), starts).length, 0);
// two starts 5 min apart whose sweep ticks coincide -> one tick, both listed (one dispatch)
assert.equal(sweepsDue(at("2026-10-10T15:20:00Z"), starts).length, 2);
assert.deepEqual(sweepsDue(at("2026-10-10T10:50:00Z"), ["2026-10-10T11:30Z"]).length, 1);                  // plain ISO strings work too
assert.deepEqual(sweepsDue(at("2026-10-10T10:50:00Z"), [{ t: "garbage" }, null]), []);
function kickoffGitHub(startsList, opts = {}) {
  const base = ghFake(opts);
  const calls = base.calls;
  const fetchImpl = async (url, init = {}) => {
    if (url === KICKOFFS_URL) { calls.push({ url, method: "GET" }); return { ok: !opts.kickoffsDown, status: opts.kickoffsDown ? 500 : 200, json: async () => ({ starts: startsList }) }; }
    return base.fetchImpl(url, init);
  };
  return { calls, fetchImpl };
}
let kg = kickoffGitHub(starts);
let res = await runTick({ GH_TOKEN: "t" }, at("2026-10-10T10:50:30Z"), kg.fetchImpl);
assert.deepEqual(res.map((e) => [e.wf, e.result]), [["lock-watchdog.yml", "dispatched"]]);
assert.deepEqual(JSON.parse(kg.calls.find((c) => c.method === "POST").body), { ref: "main", inputs: { gated: "true" } });
kg = kickoffGitHub(starts);
assert.deepEqual(await runTick({ GH_TOKEN: "t" }, at("2026-10-10T11:20:00Z"), kg.fetchImpl), []);        // no start 40 min away -> nothing
// a fixed sweep tick that ALSO has a kickoff sweep dispatches the watchdog once, not twice
kg = kickoffGitHub([{ t: "2026-10-08T23:00Z", sports: ["NHL"] }]);
res = await runTick({ GH_TOKEN: "t" }, at("2026-10-08T22:20:30Z"), kg.fetchImpl);
assert.equal(res.filter((e) => e.wf === "lock-watchdog.yml").length, 1);
// kickoffs file unreadable -> fixed slots still run, nothing breaks
kg = kickoffGitHub(starts, { kickoffsDown: true });
res = await runTick({ GH_TOKEN: "t" }, at("2026-10-08T22:20:30Z"), kg.fetchImpl);
assert.deepEqual(res.map((e) => e.result), ["dispatched"]);
kg = kickoffGitHub(starts, { kickoffsDown: true });
assert.deepEqual(await runTick({ GH_TOKEN: "t" }, at("2026-10-10T10:50:30Z"), kg.fetchImpl), []);        // and no phantom dispatch
// no token -> no network at all (also not for the kickoffs file)
kg = kickoffGitHub(starts);
await runTick({}, at("2026-10-10T10:50:30Z"), kg.fetchImpl);
assert.equal(kg.calls.length, 0);
// ── end-of-game settle sweeps ──
// expected-end math per sport (kickoff + the conservative typical length)
const endOf = (t, sport) => new Date(expectedEndMs({ t, sport })).toISOString();
assert.deepEqual(
  Object.fromEntries(["NHL", "NBA", "NFL", "CFB", "PL", "LIGA", "SERIEA", "CL", "SHL", "LIIGA", "NLA", "EXTRALIGA"].map((k) => [k, GAME_MINUTES[k]])),
  { NHL: 165, NBA: 150, NFL: 200, CFB: 225, PL: 115, LIGA: 115, SERIEA: 115, CL: 115, SHL: 155, LIIGA: 155, NLA: 155, EXTRALIGA: 155 });
assert.equal(endOf("2026-10-10T23:00Z", "NHL"), "2026-10-11T01:45:00.000Z");        // 2h45
assert.equal(endOf("2026-10-10T23:00Z", "NBA"), "2026-10-11T01:30:00.000Z");        // 2h30
assert.equal(endOf("2026-10-11T17:00Z", "NFL"), "2026-10-11T20:20:00.000Z");        // 3h20
assert.equal(endOf("2026-10-10T16:00Z", "CFB"), "2026-10-10T19:45:00.000Z");        // 3h45
assert.equal(endOf("2026-10-10T11:30Z", "PL"), "2026-10-10T13:25:00.000Z");         // 1h55
assert.equal(endOf("2026-10-10T11:30Z", "pl"), "2026-10-10T13:25:00.000Z");         // case-insensitive
assert.equal(endOf("2026-10-10T13:00Z", "SHL"), "2026-10-10T15:35:00.000Z");        // 2h35
assert.equal(endOf("2026-10-10T13:00Z", "MARS"), "2026-10-10T16:00:00.000Z");       // unknown tag -> DEFAULT_GAME_MIN (180)
assert.equal(DEFAULT_GAME_MIN, 180);
assert.ok(Number.isNaN(expectedEndMs({ t: "garbage", sport: "NHL" })) && Number.isNaN(expectedEndMs(null)));
assert.deepEqual(SETTLE_OFFSETS_MIN, [0, 20, 45]);
assert.deepEqual(SETTLE_ENTRY, { wf: TRIGGERS.settle.wf, inputs: TRIGGERS.settle.inputs });           // exactly the SETTLE NOW button's dispatch
assert.deepEqual(SETTLE_ENTRY.inputs, { mode: "settle", live: "true" });
assert.match(readFileSync(new URL("auto-lock-settle.yml", wfDir), "utf8"), /options: \['both', 'lock', 'settle',/);   // the workflow accepts mode=settle

// NHL puck drop 23:00Z -> expected end 01:45Z (2026-10-11). Checks: 01:50 tick (0), 02:10 tick (+20 = 02:05), 02:30 tick (+45 = 02:30)
const nhl = [{ t: "2026-10-10T23:00Z", sport: "NHL" }];
const dueAt = (iso, games) => settleSweepsDue(at(iso), games).map((d) => d.offsetMin);
assert.deepEqual(dueAt("2026-10-11T01:40:00Z", nhl), []);                    // the tick BEFORE the expected end: never early
assert.deepEqual(dueAt("2026-10-11T01:49:59Z", nhl), []);
assert.deepEqual(dueAt("2026-10-11T01:50:00Z", nhl), [0]);                   // first tick at/after 01:45
assert.deepEqual(dueAt("2026-10-11T01:59:59Z", nhl), [0]);                   // same tick, any second
assert.deepEqual(dueAt("2026-10-11T02:00:00Z", nhl), []);
assert.deepEqual(dueAt("2026-10-11T02:10:00Z", nhl), [20]);                  // follow-up 1: 02:05 -> 02:10 tick
assert.deepEqual(dueAt("2026-10-11T02:30:00Z", nhl), [45]);                  // follow-up 2: exactly 02:30 -> the 02:30 tick itself
assert.deepEqual(dueAt("2026-10-11T02:40:00Z", nhl), []);                    // and nothing after the last follow-up
assert.deepEqual(dueAt("2026-10-10T23:50:00Z", nhl), []);                    // mid-game: nothing
assert.deepEqual(dueAt("2026-10-10T22:50:00Z", nhl), []);                    // pre-game: nothing (that is the lock sweep's job)
// a boundary expected end fires in its own tick, not the next
assert.deepEqual(dueAt("2026-10-10T13:20:00Z", [{ t: "2026-10-10T10:35Z", sport: "NBA" }]), []);        // 10:35 + 150 = 13:05 -> the 13:10 tick, not 13:20
assert.deepEqual(dueAt("2026-10-10T13:10:00Z", [{ t: "2026-10-10T10:35Z", sport: "NBA" }]), [0]);
assert.deepEqual(dueAt("2026-10-10T13:10:00Z", [{ t: "2026-10-10T10:40Z", sport: "NBA" }]), [0]);        // end 13:10 exactly -> the 13:10 tick
assert.deepEqual(dueAt("2026-10-10T13:00:00Z", [{ t: "2026-10-10T10:40Z", sport: "NBA" }]), []);
// the other sports' windows
assert.deepEqual(dueAt("2026-10-10T13:30:00Z", [{ t: "2026-10-10T11:30Z", sport: "PL" }]), [0]);        // soccer: end 13:25 -> 13:30 tick
assert.deepEqual(dueAt("2026-10-10T19:50:00Z", [{ t: "2026-10-10T16:00Z", sport: "CFB" }]), [0]);        // CFB: end 19:45 -> 19:50 tick
assert.deepEqual(dueAt("2026-10-11T20:20:00Z", [{ t: "2026-10-11T17:00Z", sport: "NFL" }]), [0]);        // NFL: end 20:20 exactly -> the 20:20 tick
assert.deepEqual(dueAt("2026-10-10T15:40:00Z", [{ t: "2026-10-10T13:00Z", sport: "LIIGA" }]), [0]);      // euro hockey: end 15:35 -> 15:40 tick
// bad rows are ignored, not fatal
assert.equal(settleSweepsDue(at("2026-10-11T01:50:00Z"), [null, { t: "x", sport: "NHL" }, {}, ...nhl]).length, 1);
assert.deepEqual(settleSweepsDue(at("2026-10-11T01:50:00Z"), undefined), []);

// fake GitHub for the settle sweep: kickoffs.json (games), the runs list, and the dispatch
function settleGitHub(gamesList, { runs = [], runsStatus = 200, runsThrows = false, dispatchStatus = 204, starts: st = [] } = {}) {
  const calls = [];
  const fetchImpl = async (url, init = {}) => {
    calls.push({ url, method: init.method || "GET", body: init.body });
    if (url === KICKOFFS_URL) return { ok: true, status: 200, json: async () => ({ starts: st, games: gamesList }) };
    if (url.includes("/workflows/auto-lock-settle.yml/runs")) {
      if (runsThrows) throw new Error("network down");
      return { ok: runsStatus === 200, status: runsStatus, json: async () => ({ workflow_runs: runs }) };
    }
    if (url.includes("/runs")) return { ok: true, status: 200, json: async () => ({ total_count: 0, workflow_runs: [] }) };
    return { ok: true, status: dispatchStatus, json: async () => ({}) };
  };
  const allPosts = () => calls.filter((c) => c.method === "POST");
  const posts = () => allPosts().filter((c) => c.url.includes("/workflows/auto-lock-settle.yml/dispatches"));
  return { calls, fetchImpl, posts, allPosts };
}
const TOK = { GH_TOKEN: "t" };
// 01:50 UTC is also one of the fixed pre-drop watchdog slots, so these helpers look at the settle sweep's own row only
const runS = async (...a) => (await runTick(...a)).filter((e) => e.wf === "auto-lock-settle.yml");
// a tick that SHOULD dispatch: exactly the SETTLE NOW inputs, ref main, auto-lock-settle.yml
let sg = settleGitHub(nhl);
res = await runS(TOK, at("2026-10-11T01:50:00Z"), sg.fetchImpl);
assert.equal(res.length, 1);
assert.deepEqual([res[0].wf, res[0].result, res[0].games, res[0].sports, res[0].offsets], ["auto-lock-settle.yml", "dispatched", 1, ["NHL"], [0]]);
assert.equal(sg.posts().length, 1);
assert.ok(sg.posts()[0].url.endsWith("/workflows/auto-lock-settle.yml/dispatches"));
assert.deepEqual(JSON.parse(sg.posts()[0].body), { ref: "main", inputs: { mode: "settle", live: "true" } });
// ...and the two follow-ups, each dispatching on its own tick
for (const [iso, off] of [["2026-10-11T02:10:00Z", 20], ["2026-10-11T02:30:00Z", 45]]) {
  sg = settleGitHub(nhl);
  res = await runS(TOK, at(iso), sg.fetchImpl);
  assert.deepEqual([res[0].result, res[0].offsets], ["dispatched", [off]], iso);
  assert.equal(sg.posts().length, 1);
}
// ticks that should NOT dispatch: before the end, between checks, after the last follow-up
for (const iso of ["2026-10-11T01:40:00Z", "2026-10-11T02:00:00Z", "2026-10-11T02:20:00Z", "2026-10-11T02:40:00Z", "2026-10-11T05:00:00Z", "2026-10-10T23:00:00Z"]) {
  sg = settleGitHub(nhl);
  assert.deepEqual(await runS(TOK, at(iso), sg.fetchImpl), [], iso);
  assert.equal(sg.posts().length, 0, iso);
  assert.ok(!sg.calls.some((c) => c.url.includes("/runs")), "idle tick: no GitHub API call at all");
}
sg = settleGitHub([]);
assert.deepEqual(await runS(TOK, at("2026-10-11T01:50:00Z"), sg.fetchImpl), []);
sg = settleGitHub(undefined);
assert.deepEqual(await runS(TOK, at("2026-10-11T01:50:00Z"), sg.fetchImpl), []);                      // older kickoffs.json without `games`
// many games ending in the same tick, plus follow-ups of earlier games -> ONE dispatch
const slate = [
  { t: "2026-10-10T23:00Z", sport: "NHL" }, { t: "2026-10-10T23:00Z", sport: "NHL" }, { t: "2026-10-10T23:05Z", sport: "NHL" },   // end 01:45 / 01:45 / 01:50 -> check 0 in the 01:50 tick
  { t: "2026-10-10T23:09Z", sport: "NHL" },                                                          // end 01:54 -> check 0 is the 02:00 tick (not this one)
  { t: "2026-10-10T22:45Z", sport: "NHL" },                                                          // end 01:30: its +20 (01:50) lands in the 01:50 tick too
  { t: "2026-10-10T22:50Z", sport: "NBA" },                                                          // end 01:20: nothing in this tick
];
const tick0150 = settleSweepsDue(at("2026-10-11T01:50:00Z"), slate);
assert.equal(tick0150.length, 4);
sg = settleGitHub(slate);
res = await runS(TOK, at("2026-10-11T01:50:00Z"), sg.fetchImpl);
assert.equal(res.length, 1, "one result row");
assert.equal(res[0].result, "dispatched");
assert.equal(res[0].games, 4);
assert.equal(sg.posts().length, 1, "many games ending in one tick -> exactly one dispatch");
assert.deepEqual(res[0].offsets, [0, 20]);                                                          // first checks and a follow-up merged
// dedupe: a queued / in-progress / waiting auto-lock-settle run (cron, manual or ours) blocks the dispatch
for (const status of ["in_progress", "queued", "waiting", "pending", "requested"]) {
  sg = settleGitHub(nhl, { runs: [{ status: "completed" }, { status }] });
  res = await runS(TOK, at("2026-10-11T01:50:00Z"), sg.fetchImpl);
  assert.deepEqual([res[0].wf, res[0].result], ["auto-lock-settle.yml", "skipped: already queued or running"], status);
  assert.equal(sg.posts().length, 0, `must not dispatch on top of a ${status} run`);
}
sg = settleGitHub(nhl, { runs: [{ status: "completed" }, { status: "completed" }] });                // only finished runs -> go
assert.equal((await runS(TOK, at("2026-10-11T01:50:00Z"), sg.fetchImpl))[0].result, "dispatched");
sg = settleGitHub(nhl, { runs: [] });
assert.equal((await runS(TOK, at("2026-10-11T01:50:00Z"), sg.fetchImpl))[0].result, "dispatched");
// FAIL-OPEN: the runs lookup failing (HTTP error or network) still dispatches
for (const opt of [{ runsStatus: 500 }, { runsStatus: 403 }, { runsThrows: true }]) {
  sg = settleGitHub(nhl, opt);
  res = await runS(TOK, at("2026-10-11T01:50:00Z"), sg.fetchImpl);
  assert.equal(res[0].result, "dispatched", JSON.stringify(opt));
  assert.equal(sg.posts().length, 1);
}
// a refused dispatch is reported as an error (shows as a failed Worker invocation)
sg = settleGitHub(nhl, { dispatchStatus: 403 });
res = await runS(TOK, at("2026-10-11T01:50:00Z"), sg.fetchImpl);
assert.equal(res[0].result, "error: HTTP 403");
// no token -> no dispatch and no network at all
sg = settleGitHub(nhl);
assert.deepEqual(await runS({}, at("2026-10-11T01:50:00Z"), sg.fetchImpl), []);
assert.equal(sg.calls.length, 0);
// kickoffs.json unreadable -> no settle sweep, nothing breaks
kg = kickoffGitHub(starts, { kickoffsDown: true });
assert.deepEqual(await runS(TOK, at("2026-10-11T01:50:00Z"), kg.fetchImpl), []);
// a lock sweep and a settle sweep in the same tick both run (different workflows); the 01:50 tick has the fixed watchdog slot AND a kickoff sweep for a 02:30Z start, still ONE watchdog dispatch
sg = settleGitHub(nhl, { starts: [{ t: "2026-10-11T02:30Z", sports: ["NHL"] }] });
res = await runTick(TOK, at("2026-10-11T01:50:00Z"), sg.fetchImpl);
assert.deepEqual(res.map((e) => [e.wf, e.result]), [["lock-watchdog.yml", "dispatched"], ["auto-lock-settle.yml", "dispatched"]]);
assert.equal(sg.allPosts().length, 2);
// the result rows carry no credential
assert.ok(!JSON.stringify(res).includes("Bearer"));

// status page: next settle sweeps in UTC AND Mountain Time, next to kickoffSweeps
assert.equal(mountain(at("2026-10-11T01:50:00Z")), "2026-10-10 07:50 PM MT");                         // MDT (UTC-6)
assert.equal(mountain(at("2026-12-01T13:05:00Z")), "2026-12-01 06:05 AM MT");                         // MST (UTC-7)
const gamesFix = [{ t: "2026-10-10T23:00Z", sport: "NHL" }, { t: "2026-10-10T23:05Z", sport: "NHL" }, { t: "2026-10-11T17:00Z", sport: "NFL" }, { t: "2026-10-09T01:00Z", sport: "NHL" }];
const nowFix = at("2026-10-10T20:03:00Z");
const ups = upcomingSettleSweeps(nowFix, gamesFix);
assert.deepEqual(ups.map((u) => u.at), ["2026-10-11T01:50:00.000Z", "2026-10-11T02:10:00.000Z", "2026-10-11T02:30:00.000Z", "2026-10-11T02:40:00.000Z", "2026-10-11T20:20:00.000Z", "2026-10-11T20:40:00.000Z", "2026-10-11T21:10:00.000Z"]);
assert.deepEqual(ups[0].forGames.map((g) => g.offsetMin), [0, 0]);                                    // both NHL games end in the 01:50 tick -> one row
assert.deepEqual(ups[4].forGames, [{ t: "2026-10-11T17:00Z", sport: "NFL", endsAt: "2026-10-11T20:20:00.000Z", offsetMin: 0 }]);
assert.deepEqual(ups[4].sports, ["NFL"]);
assert.equal(upcomingSettleSweeps(nowFix, gamesFix, { max: 2 }).length, 2);
assert.deepEqual(upcomingSettleSweeps(nowFix, gamesFix, { horizonHours: 1 }), []);                    // past games and out-of-horizon ones are not listed
const realFetch = globalThis.fetch;
globalThis.fetch = async (url) => (url === KICKOFFS_URL ? { ok: true, status: 200, json: async () => ({ starts: [{ t: "2099-01-01T00:00Z", sports: ["NHL"] }], games: [{ t: new Date(Date.now() - 100 * 60000).toISOString().slice(0, 16) + "Z", sport: "NHL" }] }) } : { ok: false, status: 404, json: async () => ({}) });
try {
  const page = await worker.fetch(new Request("https://w.example/"), {});
  const body = await page.json();
  assert.ok(Array.isArray(body.kickoffSweeps) && Array.isArray(body.settleSweeps), "status page lists both");
  assert.equal(body.settleGames, 1);
  assert.ok(body.settleSweeps.length >= 1, "an NHL game that started 100 min ago has its end sweeps ahead");
  for (const sw of body.settleSweeps) {
    assert.match(sw.at, /^\d{4}-\d\d-\d\dT\d\d:\d\d:00\.000Z$/);
    assert.match(sw.atMT, /^\d{4}-\d\d-\d\d \d\d:\d\d [AP]M MT$/);
    assert.ok(sw.forGames.every((g) => g.sport === "NHL" && /MT$/.test(g.endsAtMT) && SETTLE_OFFSETS_MIN.includes(g.offsetMin)));
  }
  assert.equal(body.settleRule.gameMinutes.NHL, 165);
  assert.deepEqual(body.settleRule.checksAtMinutesAfterExpectedEnd, [0, 20, 45]);
} finally { globalThis.fetch = realFetch; }
// ── owner profit-tracker sync (GET / PUT /profit, KV binding PROFIT) ──
const fakeKV = () => { const m = new Map(); return { m, get: async (k, t) => (m.has(k) ? (t === "json" ? JSON.parse(m.get(k)) : m.get(k)) : null), put: async (k, v) => { m.set(k, v); } }; };
const preq = (method, { key, body, origin = "https://clairvoyanceengine.info", raw } = {}) =>
  new Request("https://w.example/profit", { method, headers: { origin, ...(key ? { "x-owner-key": key } : {}), ...(body || raw ? { "content-type": "application/json" } : {}) }, body: raw ?? (body ? JSON.stringify(body) : undefined) });
const penv = () => ({ TRIGGER_KEY: env.TRIGGER_KEY, PROFIT: fakeKV() });
const E1 = { id: "a1", ts: 1000, date: "2026-10-05", type: "DEPOSIT", book: "DraftKings", amount: 100 };
const E2 = { id: "a2", ts: 2000, date: "2026-10-06", type: "PROFIT_LOSS", book: "Hard Rock", amount: -12.5, stake: 25 };
let pe = penv();
r = await handleProfit(preq("GET", { key: "wrong" }), pe, nosleep);
assert.equal(r.status, 401);                                                                          // wrong key
r = await handleProfit(preq("GET"), pe, nosleep);
assert.equal(r.status, 401);                                                                          // no key at all
r = await handleProfit(preq("GET", { key: env.TRIGGER_KEY }), { TRIGGER_KEY: env.TRIGGER_KEY }, nosleep);
assert.equal(r.status, 503);
assert.deepEqual(await r.json(), { error: "sync not configured" });                                    // KV namespace not bound yet
r = await handleProfit(preq("PUT", { key: env.TRIGGER_KEY, body: { entries: [], tombstones: [], baseRev: 0 } }), { TRIGGER_KEY: env.TRIGGER_KEY }, nosleep);
assert.equal(r.status, 503);
r = await handleProfit(preq("GET", { key: env.TRIGGER_KEY }), { PROFIT: fakeKV() }, nosleep);
assert.equal(r.status, 503);                                                                          // TRIGGER_KEY secret missing -> never "open"
r = await handleProfit(preq("GET", { key: env.TRIGGER_KEY }), pe, nosleep);
assert.equal(r.status, 200);
assert.deepEqual(await r.json(), { entries: [], tombstones: [], rev: 0 });                            // empty store
// round trip
r = await handleProfit(preq("PUT", { key: env.TRIGGER_KEY, body: { entries: [E1, E2], tombstones: [{ id: "gone", ts: 5 }], baseRev: 0 } }), pe, nosleep);
assert.equal(r.status, 200);
let stored = await r.json();
assert.equal(stored.rev, 1);
assert.deepEqual(stored.entries, [E1, E2]);
assert.deepEqual(stored.tombstones, [{ id: "gone", ts: 5 }]);
r = await handleProfit(preq("GET", { key: env.TRIGGER_KEY }), pe, nosleep);
const got = await r.json();
assert.deepEqual([got.rev, got.entries, got.tombstones], [1, [E1, E2], [{ id: "gone", ts: 5 }]]);
assert.equal(r.headers.get("cache-control"), "no-store");
assert.equal(pe.PROFIT.m.size, 1, "exactly ONE blob in KV");
// stale baseRev -> 409 carrying the stored blob, nothing overwritten; the right baseRev bumps the rev again
r = await handleProfit(preq("PUT", { key: env.TRIGGER_KEY, body: { entries: [E1], tombstones: [], baseRev: 0 } }), pe, nosleep);
assert.equal(r.status, 409);
const conflict = await r.json();
assert.equal(conflict.rev, 1);
assert.deepEqual(conflict.entries, [E1, E2]);
assert.equal(JSON.parse(pe.PROFIT.m.get("ledger")).entries.length, 2);
r = await handleProfit(preq("PUT", { key: env.TRIGGER_KEY, body: { entries: [E1], tombstones: [], baseRev: 1 } }), pe, nosleep);
assert.equal(r.status, 200);
assert.equal((await r.json()).rev, 2);
// wrong key on PUT never writes; junk bodies are 400/413
const before = pe.PROFIT.m.get("ledger");
r = await handleProfit(preq("PUT", { key: "nope", body: { entries: [E2], tombstones: [], baseRev: 2 } }), pe, nosleep);
assert.equal(r.status, 401);
assert.equal(pe.PROFIT.m.get("ledger"), before);
for (const bad of [{ entries: "x", tombstones: [], baseRev: 2 }, { entries: [{ ts: 1 }], tombstones: [], baseRev: 2 }, { entries: [], tombstones: [], baseRev: "2" }, { entries: [], tombstones: [], baseRev: -1 }, null]) {
  r = await handleProfit(preq("PUT", { key: env.TRIGGER_KEY, body: bad, raw: bad === null ? "not json" : undefined }), pe, nosleep);
  assert.equal(r.status, 400, JSON.stringify(bad));
}
r = await handleProfit(preq("PUT", { key: env.TRIGGER_KEY, raw: JSON.stringify({ entries: [{ id: "x", ts: 1, note: "y".repeat(2_100_000) }], tombstones: [], baseRev: 2 }) }), pe, nosleep);
assert.equal(r.status, 413);
assert.equal(pe.PROFIT.m.get("ledger"), before);
r = await handleProfit(preq("POST", { key: env.TRIGGER_KEY, body: {} }), pe, nosleep);
assert.equal(r.status, 405);
r = await handleProfit(preq("DELETE", { key: env.TRIGGER_KEY }), pe, nosleep);
assert.equal(r.status, 405);
// CORS: the browser preflight for GET/PUT with the x-owner-key header is answered for every allowed origin, and ONLY for them
for (const o of ALLOWED_ORIGINS) {
  r = await handleProfit(preq("OPTIONS", { origin: o }), pe, nosleep);
  assert.equal(r.status, 204);
  assert.equal(r.headers.get("access-control-allow-origin"), o);
  assert.match(r.headers.get("access-control-allow-headers"), /x-owner-key/i);
  assert.match(r.headers.get("access-control-allow-headers"), /content-type/i);
  assert.match(r.headers.get("access-control-allow-methods"), /\bGET\b/);
  assert.match(r.headers.get("access-control-allow-methods"), /\bPUT\b/);
}
for (const o of ["https://evil.example", "https://clairvoyanceengine.info.evil.example", "null", ""]) {
  for (const m of ["OPTIONS", "GET", "PUT"]) {
    r = await handleProfit(preq(m, { origin: o, key: m === "OPTIONS" ? undefined : env.TRIGGER_KEY, body: m === "PUT" ? { entries: [], tombstones: [], baseRev: 2 } : undefined }), pe, nosleep);
    assert.equal(r.headers.get("access-control-allow-origin"), null, `${m} from ${o} must not be reflected`);
    assert.equal(r.headers.get("access-control-allow-headers"), null);
  }
}
// the old /trigger preflight still works (same corsFor, now also advertising the new header + methods)
r = await handleTrigger(req({}, { method: "OPTIONS" }), env, ghFake().fetchImpl, nosleep);
assert.equal(r.status, 204);
assert.match(r.headers.get("access-control-allow-methods"), /POST/);
// routing through the Worker's own fetch(): /profit reaches the handler, and an unbound deploy answers 503 instead of crashing
r = await worker.fetch(new Request("https://w.example/profit", { headers: { origin: "https://clairvoyanceengine.info", "x-owner-key": env.TRIGGER_KEY } }), { TRIGGER_KEY: env.TRIGGER_KEY });
assert.equal(r.status, 503);
r = await worker.fetch(new Request("https://w.example/profit", { headers: { "x-owner-key": "bad" } }), { TRIGGER_KEY: env.TRIGGER_KEY, PROFIT: fakeKV() });
assert.equal(r.status, 401);
console.log("scheduler OK:", SCHEDULE.length, "workflows + owner trigger");
