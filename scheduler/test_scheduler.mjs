// node scheduler/test_scheduler.mjs -- offline: slot matching, weekday filters, dedupe, dispatch, failure reporting (GitHub API faked)
import assert from "node:assert/strict";
import { dueWorkflows, dueEntries, runTick, SCHEDULE, TICK_MIN, handleTrigger, safeEqual, TRIGGERS, ALLOWED_ORIGINS } from "./worker.js";
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
console.log("scheduler OK:", SCHEDULE.length, "workflows + owner trigger");
