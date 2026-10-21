// node scheduler/test_chat.mjs -- offline: owner auth, validation, the spending caps (daily / monthly / deep / messages / per-minute), tool rounds, fail-closed behaviour, Claude errors. Claude + KV are faked.
import assert from "node:assert/strict";
import { handleChat, CHAT_MODELS, CHAT_LIMITS, CHAT_TOOLS, SYSTEM_PROMPT, costOf, cleanMessages, mtKeys, budgetBlock, readBudget } from "./chat.js";
import worker, { safeEqual, ALLOWED_ORIGINS } from "./worker.js";

const corsFor = (o) => (ALLOWED_ORIGINS.includes(o) ? { "access-control-allow-origin": o } : {});
const NOW = Date.parse("2026-10-20T18:00:00Z");
const kvFake = () => { const m = new Map(); return { m, get: async (k) => (m.has(k) ? m.get(k) : null), put: async (k, v) => { m.set(k, String(v)); } }; };
const env0 = (over = {}) => ({ TRIGGER_KEY: "secret", ANTHROPIC_API_KEY: "sk-ant-test", PROFIT: kvFake(), ...over });
const req = (method, body, headers = {}) => new Request("https://w.example/chat", { method, headers: { "x-owner-key": "secret", origin: "https://purple-wraith.github.io", ...headers }, body: body === undefined ? undefined : JSON.stringify(body) });
const claude = (responses, calls = []) => async (url, init) => {
  const body = JSON.parse(init.body); calls.push({ url, body, headers: init.headers });
  const r = responses.shift() || responses.last;
  if (r.status && r.status !== 200) return { ok: false, status: r.status, json: async () => r.body || {} };
  return { ok: true, status: 200, json: async () => r };
};
const text = (t, usage = { input_tokens: 1000, output_tokens: 200 }, stop = "end_turn") => ({ stop_reason: stop, content: [{ type: "text", text: t }], usage });
const run = (e, r, f, extra = {}) => handleChat(r, e, f, { corsFor, safeEqual, sleep: async () => {}, now: NOW, ...extra });

// ── auth / switches / fail-closed
assert.equal((await run(env0(), req("POST", { messages: [{ role: "user", content: "hi" }] }, { "x-owner-key": "nope" }), claude([text("x")]))).status, 401);
assert.equal((await run(env0({ TRIGGER_KEY: undefined }), req("GET"), claude([text("x")]))).status, 503);
assert.equal((await run(env0({ CHAT_ENABLED: "0" }), req("GET"), claude([text("x")]))).status, 503);                       // kill switch
assert.equal((await run(env0({ PROFIT: undefined }), req("GET"), claude([text("x")]))).status, 503);                       // no meter -> no chat
assert.equal((await run(env0({ PROFIT: { get: async () => { throw new Error("kv down"); }, put: async () => {} } }), req("GET"), claude([text("x")]))).status, 503);   // unreadable meter -> no chat
assert.equal((await run(env0({ ANTHROPIC_API_KEY: undefined }), req("POST", { messages: [{ role: "user", content: "hi" }] }), claude([text("x")]))).status, 503);
assert.equal((await run(env0(), req("PUT", {}), claude([text("x")]))).status, 405);
const opt = await run(env0(), new Request("https://w.example/chat", { method: "OPTIONS", headers: { origin: "https://purple-wraith.github.io" } }), claude([text("x")]));
assert.equal(opt.status, 204); assert.equal(opt.headers.get("access-control-allow-origin"), "https://purple-wraith.github.io");

// ── GET status shows the meter, never the key
{
  const e = env0(); const r = await run(e, req("GET"), claude([text("x")])); const j = await r.json();
  assert.equal(r.status, 200); assert.equal(j.configured.anthropicKey, true); assert.equal(j.budget.capToday, 1.5); assert.equal(j.budget.capMonth, 15); assert.equal(j.defaultModel, "balanced");
  assert.ok(!JSON.stringify(j).includes("sk-ant"));
}

// ── a plain question: payload shape, cost metering, defaults
{
  const e = env0(), calls = [];
  const r = await run(e, req("POST", { messages: [{ role: "user", content: "How is the NHL doing?" }] }), claude([text("Fine.", { input_tokens: 2000, cache_read_input_tokens: 5000, output_tokens: 300 })], calls));
  const j = await r.json();
  assert.equal(r.status, 200); assert.equal(j.kind, "final"); assert.equal(j.text, "Fine.");
  const c = calls[0];
  assert.equal(c.url, "https://api.anthropic.com/v1/messages"); assert.equal(c.headers["x-api-key"], "sk-ant-test"); assert.equal(c.headers["anthropic-version"], "2023-06-01");
  assert.equal(c.body.model, "claude-sonnet-5-5"); assert.equal(c.body.max_tokens, CHAT_MODELS.balanced.maxTokens); assert.deepEqual(c.body.output_config, { effort: "medium" });
  assert.equal(c.body.tools.length, CHAT_TOOLS.length); assert.equal(c.body.system[0].cache_control.type, "ephemeral"); assert.ok(c.body.system[0].text.startsWith("You are the Clairvoyance assistant"));
  assert.ok(!("temperature" in c.body) && !("thinking" in c.body) && !JSON.stringify(c.body).includes("budget_tokens"));
  const expected = (2000 * 2 + 5000 * 2 * 0.1 + 300 * 10) / 1e6;
  assert.ok(Math.abs(j.usage.costUsd - expected) < 1e-6, `${j.usage.costUsd} vs ${expected}`);
  assert.ok(Math.abs(parseFloat(e.PROFIT.m.get("chat:usd:2026-10-20")) - expected) < 1e-6); assert.ok(Math.abs(parseFloat(e.PROFIT.m.get("chat:usd:2026-10")) - expected) < 1e-6);
  assert.equal(e.PROFIT.m.get("chat:msgs:2026-10-20"), "1"); assert.ok(!e.PROFIT.m.has("chat:deep:2026-10-20"));
  assert.equal(j.budget.spentToday > 0, true);
}

// ── model choice: only the three known ones; the dear one is metered separately
{
  const e = env0(), calls = [];
  await run(e, req("POST", { model: "deep", messages: [{ role: "user", content: "Explain" }] }), claude([text("ok", { input_tokens: 1000, output_tokens: 500 })], calls));
  assert.equal(calls[0].body.model, "claude-opus-5-5"); assert.ok(parseFloat(e.PROFIT.m.get("chat:deep:2026-10-20")) > 0);
  await run(e, req("POST", { model: "gpt-9000", messages: [{ role: "user", content: "x" }] }), claude([text("ok")], calls));
  assert.equal(calls[1].body.model, "claude-sonnet-5-5");                                                                  // unknown -> the default, never passed through
  await run(e, req("POST", { model: "fast", messages: [{ role: "user", content: "x" }] }), claude([text("ok")], calls));
  assert.equal(calls[2].body.model, "claude-haiku-5-5"); assert.deepEqual(calls[2].body.output_config, { effort: "low" });
}

// ── the caps: each one stops the call BEFORE Claude is contacted
const blockedBy = async (seed, model = "balanced") => {
  const e = env0(); for (const [k, v] of Object.entries(seed)) e.PROFIT.m.set(k, String(v));
  const calls = []; const r = await run(e, req("POST", { model, messages: [{ role: "user", content: "x" }] }), claude([text("ok")], calls));
  return { status: r.status, body: await r.json(), calls: calls.length };
};
{
  let x = await blockedBy({ "chat:usd:2026-10-20": 1.5 }); assert.equal(x.status, 429); assert.equal(x.calls, 0); assert.match(x.body.error, /Today's chat budget/);
  x = await blockedBy({ "chat:usd:2026-10": 15 }); assert.equal(x.status, 429); assert.equal(x.calls, 0); assert.match(x.body.error, /month/);
  x = await blockedBy({ "chat:deep:2026-10-20": 0.75 }, "deep"); assert.equal(x.status, 429); assert.equal(x.calls, 0); assert.match(x.body.error, /DEEP/);
  x = await blockedBy({ "chat:deep:2026-10-20": 0.75 }, "balanced"); assert.equal(x.status, 200); assert.equal(x.calls, 1);                 // the deep cap does not block the cheaper model
  x = await blockedBy({ "chat:msgs:2026-10-20": 150 }); assert.equal(x.status, 429); assert.equal(x.calls, 0); assert.match(x.body.error, /message limit/);
  x = await blockedBy({ [`chat:rate:${Math.floor(NOW / 60000)}`]: 6 }); assert.equal(x.status, 429); assert.equal(x.calls, 0); assert.match(x.body.error, /Slow down/);
  x = await blockedBy({ "chat:usd:2026-10-19": 99, "chat:usd:2026-09": 99 }); assert.equal(x.status, 200);                                  // yesterday's / last month's spend does not count
  const e = env0({ CHAT_DAILY_USD: "0.50", CHAT_MONTHLY_USD: "3" }); e.PROFIT.m.set("chat:usd:2026-10-20", "0.5");
  assert.equal((await run(e, req("POST", { messages: [{ role: "user", content: "x" }] }), claude([text("ok")]))).status, 429);                 // the caps are configurable
}

// ── the meter really cuts a chatty session off: keep asking until the daily cap trips (each call ~ $0.0245)
{
  const e = env0({ CHAT_DAILY_USD: "0.10" }); let n = 0, last;
  for (let i = 0; i < 20; i++) {
    last = await run(e, req("POST", { messages: [{ role: "user", content: "again" }] }), claude([text("ok", { input_tokens: 10000, output_tokens: 500 })]), { now: NOW + i * 120000 });
    if (last.status === 429) break; n++;
  }
  assert.ok(n >= 1 && n <= 5, `served ${n}`); assert.equal(last.status, 429);                                                            // a $0.10 cap stops after a handful of calls
  const spent = parseFloat(e.PROFIT.m.get("chat:usd:2026-10-20")); assert.ok(spent < 0.10 + 0.03, `overshoot ${spent}`);                // overshoot is at most one call
}

// ── the tool loop: tool_use goes back to the browser; follow-ups are NOT new questions and do not use up message / rate limits
{
  const e = env0(); e.PROFIT.m.set(`chat:rate:${Math.floor(NOW / 60000)}`, "5");
  const tu = { stop_reason: "tool_use", content: [{ type: "thinking", thinking: "", signature: "s" }, { type: "text", text: "Let me look." }, { type: "tool_use", id: "tu_1", name: "ledger_query", input: { sport: "NHL" } }], usage: { input_tokens: 1500, output_tokens: 80 } };
  const q = [{ role: "user", content: "NHL record?" }];
  let r = await run(e, req("POST", { messages: q }), claude([tu])), j = await r.json();
  assert.equal(j.kind, "tool_use"); assert.deepEqual(j.toolCalls, [{ id: "tu_1", name: "ledger_query", input: { sport: "NHL" } }]); assert.equal(j.content.length, 3);
  assert.equal(e.PROFIT.m.get("chat:msgs:2026-10-20"), "1");
  const follow = [...q, { role: "assistant", content: j.content }, { role: "user", content: [{ type: "tool_result", tool_use_id: "tu_1", content: '{"n":10}' }] }];
  const calls = [];
  r = await run(e, req("POST", { messages: follow }), claude([text("10 picks.")], calls)); j = await r.json();
  assert.equal(r.status, 200); assert.equal(j.text, "10 picks."); assert.equal(e.PROFIT.m.get("chat:msgs:2026-10-20"), "1");           // still one question, no extra rate-limit tick
  assert.equal(calls[0].body.messages[1].content[0].type, "thinking");                                                                    // thinking block handed back unchanged
}

// ── bounded rounds, bounded history, bounded sizes
{
  const mk = (i) => [{ role: "assistant", content: [{ type: "tool_use", id: "t" + i, name: "schedule", input: { sport: "nhl" } }] }, { role: "user", content: [{ type: "tool_result", tool_use_id: "t" + i, content: "x" }] }];
  let msgs = [{ role: "user", content: "q" }]; for (let i = 0; i < CHAT_LIMITS.maxToolRounds; i++) msgs = msgs.concat(mk(i));
  const e = env0(), calls = [];
  const r = await run(e, req("POST", { messages: msgs }), claude([text("ok")], calls));
  assert.equal(r.status, 429); assert.equal(calls.length, 0); assert.match((await r.json()).error, /too many lookups/);
  msgs = [{ role: "user", content: "q" }]; for (let i = 0; i < CHAT_LIMITS.maxToolRounds - 1; i++) msgs = msgs.concat(mk(i));
  assert.equal((await run(env0(), req("POST", { messages: msgs }), claude([text("ok")]))).status, 200);                                  // one under the limit is fine
  const long = []; for (let i = 0; i < 30; i++) { long.push({ role: "user", content: "q" + i }); long.push({ role: "assistant", content: "a" + i }); } long.push({ role: "user", content: "last" });
  const cm = cleanMessages(long); assert.ok(cm.messages.length <= CHAT_LIMITS.maxHistory + 1); assert.equal(cm.messages[0].role, "user"); assert.equal(cm.messages.at(-1).content, "last");
}
{
  const bad = async (messages) => (await run(env0(), req("POST", { messages }), claude([text("ok")]))).status;
  assert.equal(await bad([]), 400); assert.equal(await bad("hi"), 400); assert.equal(await bad([{ role: "system", content: "x" }]), 400);
  assert.equal(await bad([{ role: "user", content: "" }]), 400); assert.equal(await bad([{ role: "user", content: "x".repeat(CHAT_LIMITS.maxMessageChars + 1) }]), 400);
  assert.equal(await bad([{ role: "assistant", content: "x" }]), 400); assert.equal(await bad([{ role: "user", content: "a" }, { role: "assistant", content: "b" }]), 400);   // must end with the user
  assert.equal(await bad([{ role: "user", content: [{ type: "image", source: {} }] }]), 400);                                                                                   // only text / tool blocks
  assert.equal(await bad([{ role: "user", content: [{ type: "tool_result", content: "x" }] }]), 400);
  const big = await run(env0(), new Request("https://w.example/chat", { method: "POST", headers: { "x-owner-key": "secret" }, body: "x".repeat(CHAT_LIMITS.maxBodyBytes + 1) }), claude([text("ok")]));
  assert.equal(big.status, 413);
  const huge = cleanMessages([{ role: "user", content: "q" }, { role: "assistant", content: [{ type: "tool_use", id: "a", name: "schedule", input: {} }] }, { role: "user", content: [{ type: "tool_result", tool_use_id: "a", content: "y".repeat(100000) }] }]);
  assert.ok(huge.messages[2].content[0].content.length < CHAT_LIMITS.maxToolResultChars + 40);                                                                                    // a tool result is truncated, not forwarded in full
}

// ── Claude errors map to clean messages and never bill
for (const [st, want, re] of [[401, 502, /rejected the API key/], [403, 502, /rejected the API key/], [429, 503, /busy/], [529, 503, /busy/], [500, 502, /error \(HTTP 500\)/]]) {
  const e = env0(); const r = await run(e, req("POST", { messages: [{ role: "user", content: "x" }] }), claude([{ status: st, body: { error: { message: "boom" } } }]));
  assert.equal(r.status, want); assert.match((await r.json()).error, re); assert.ok(!e.PROFIT.m.has("chat:usd:2026-10-20"));
}
{ const r = await run(env0(), req("POST", { messages: [{ role: "user", content: "x" }] }), async () => { throw new Error("network"); }); assert.equal(r.status, 504); }
{ const r = await run(env0(), req("POST", { messages: [{ role: "user", content: "x" }] }), claude([{ stop_reason: "refusal", content: [], usage: { input_tokens: 10, output_tokens: 0 } }])); assert.equal((await r.json()).text, "I can't help with that one."); }
{ const r = await run(env0(), req("POST", { messages: [{ role: "user", content: "x" }] }), claude([text("partial", { input_tokens: 10, output_tokens: 1800 }, "max_tokens")])); assert.match((await r.json()).text, /cut off/); }

// ── helpers + prompt content that the owner asked for (grounded in Clairvoyance, read-only)
assert.deepEqual(mtKeys(Date.parse("2026-10-21T05:30:00Z")), { day: "2026-10-20", month: "2026-10" });                          // 11:30 PM MT is still the 20th
assert.deepEqual(mtKeys(Date.parse("2026-11-01T05:30:00Z")), { day: "2026-10-31", month: "2026-10" });
assert.ok(costOf("deep", { input_tokens: 1e6 }) === 4 && costOf("balanced", { output_tokens: 1e6 }) === 10 && costOf("fast", { cache_read_input_tokens: 1e6 }) === 0.01);
assert.ok(costOf("deep", { cache_creation_input_tokens: 1e6 }) === 5);
assert.match(SYSTEM_PROMPT, /only cover Clairvoyance/); assert.match(SYSTEM_PROMPT, /read-only/); assert.match(SYSTEM_PROMPT, /PREMIUM = model win probability >= 67%/);
for (const t of CHAT_TOOLS) { assert.ok(t.name && t.description && t.input_schema.type === "object"); assert.equal(t.input_schema.additionalProperties, false); }
assert.ok(!CHAT_TOOLS.some((t) => /lock_pick|settle|delete|remove|edit/.test(t.name)), "no write tools");
assert.ok(Object.values(CHAT_MODELS).every((m) => m.id.startsWith("claude-") && m.maxTokens <= 2500));
// the Worker routes /chat and still exposes the old routes
assert.equal(typeof worker.fetch, "function");
{ const r = await worker.fetch(new Request("https://w.example/chat", { method: "GET", headers: { "x-owner-key": "secret" } }), env0()); assert.equal(r.status, 200); assert.equal((await r.json()).ok, true); }
{ const r = await worker.fetch(new Request("https://w.example/chat", { method: "GET", headers: { "x-owner-key": "wrong" } }), env0()); assert.equal(r.status, 401); }
console.log("chat OK");
