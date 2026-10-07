// Clairvoyance scheduler -- fires the DATA-REFRESH workflows on time.
//
// Why: GitHub's own `schedule:` triggers start 3-6 hours late (documented in this repo's lock-workflow headers), so a refresh that should land at
// 05:13 UTC lands mid-morning and the freshness checks have to be loose enough to tolerate it. A Cloudflare cron trigger is accurate to about a
// minute: this Worker wakes every TICK_MIN minutes, works out which refresh workflows are due in that slot, and starts them through the GitHub API
// (workflow_dispatch). The workflows' own `schedule:` entries stay in place as a fallback, so nothing is lost if this Worker is ever off.
//
// Deliberately NOT here: the lock / settle / email workflows. Their scheduled runs choose behaviour from `github.event.schedule`, and a dispatched run
// does not carry it; they are left on GitHub's cron plus the watchdog. The WATCHDOG itself is here (evening slots only): it does not read `github.event.schedule`,
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

/** Starts every workflow due at `when`. Returns [{wf, result}] where result is "dispatched" | "skipped: ..." | "error: ...". */
export async function runTick(env, when, fetchImpl = fetch) {
  const out = [];
  for (const { wf, inputs } of dueEntries(when)) {
    if (!env.GH_TOKEN) { out.push({ wf, result: "skipped: GH_TOKEN secret not set" }); continue; }
    try {
      if (await alreadyRunning(env, wf, fetchImpl)) { out.push({ wf, result: "skipped: already queued or running" }); continue; }
      const r = await gh(env, `/actions/workflows/${wf}/dispatches`, { method: "POST", body: JSON.stringify(inputs ? { ref: "main", inputs } : { ref: "main" }) }, fetchImpl);
      out.push({ wf, result: r.status === 204 ? "dispatched" : `error: HTTP ${r.status}` });
    } catch (e) {
      out.push({ wf, result: `error: ${e.message}` });
    }
  }
  return out;
}

export default {
  async scheduled(event, env, ctx) {
    const results = await runTick(env, event.scheduledTime);
    for (const r of results) console.log(`${r.wf}: ${r.result}`);
    const failed = results.filter((r) => r.result.startsWith("error"));
    if (failed.length) throw new Error("scheduler: " + failed.map((f) => `${f.wf} ${f.result}`).join("; "));   // shows as a failed invocation in the dashboard
  },
  // GET / -> what the next ticks would start (no secrets, no side effects); handy for checking the table.
  async fetch(request) {
    const now = Date.now();
    const next = [];
    for (let i = 0; i < 6 * 24 * 2; i++) {            // next 48 h, tick by tick
      const t = now + i * TICK_MIN * 60000;
      const due = dueWorkflows(t);
      if (due.length) next.push({ at: new Date(t).toISOString(), due });
      if (next.length >= 10) break;
    }
    return new Response(JSON.stringify({ tickMinutes: TICK_MIN, next }, null, 2), { headers: { "content-type": "application/json" } });
  },
};
