// node scheduler/test_scheduler.mjs -- offline: slot matching, weekday filters, dedupe, dispatch, failure reporting (GitHub API faked)
import assert from "node:assert/strict";
import { dueWorkflows, runTick, SCHEDULE, TICK_MIN } from "./worker.js";
import { readFileSync, readdirSync } from "node:fs";

const at = (iso) => Date.parse(iso);
assert.deepEqual(dueWorkflows(at("2026-10-05T05:13:30Z")), ["scheduled-refresh.yml"]);
assert.deepEqual(dueWorkflows(at("2026-10-05T05:19:59Z")), ["scheduled-refresh.yml"]);          // same 10-minute slot
assert.deepEqual(dueWorkflows(at("2026-10-05T05:20:00Z")), []);
assert.deepEqual(dueWorkflows(at("2026-10-05T12:20:00Z")).sort(), ["nfl-roster-weekly.yml"]);   // Monday 12:20-12:29 slot
// weekday filter: nfl-roster-weekly is Monday (2026-10-05) only
assert.ok(dueWorkflows(at("2026-10-05T12:23:00Z")).includes("nfl-roster-weekly.yml"));             // a Monday
assert.ok(!dueWorkflows(at("2026-10-06T12:23:00Z")).includes("nfl-roster-weekly.yml"));            // a Tuesday

// every scheduled workflow file must exist, and its own cron must include the same time (the table is a mirror, not a second opinion)
const wfDir = new URL("../.github/workflows/", import.meta.url);
const files = new Set(readdirSync(wfDir));
for (const s of SCHEDULE) {
  assert.ok(files.has(s.wf), `${s.wf} does not exist`);
  const text = readFileSync(new URL(s.wf, wfDir), "utf8");
  assert.match(text, /workflow_dispatch/, `${s.wf} cannot be dispatched`);
  const crons = [...text.matchAll(/cron:\s*'(\d+) (\d+) \* \* ([^']+)'/g)].map((m) => `${String(m[2]).padStart(2, "0")}:${String(m[1]).padStart(2, "0")}`);
  for (const t of s.at) assert.ok(crons.includes(t), `${s.wf}: ${t} UTC is not one of its own cron times (${crons.join(", ")})`);
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
assert.equal(TICK_MIN, 10);
console.log("scheduler OK:", SCHEDULE.length, "workflows");
