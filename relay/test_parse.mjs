// node relay/test_parse.mjs  -- offline check of the feed parser against a saved real sample (relay/fixtures/feed_sample.txt)
import { readFileSync } from "node:fs";
import { parseFeed } from "./worker.js";
import assert from "node:assert/strict";

const text = readFileSync(new URL("./fixtures/feed_sample.txt", import.meta.url), "utf8");
const games = parseFeed(text);
assert.ok(games.length > 0, "no games parsed");
const leagues = new Set(games.map(g => g.lg));
for (const lg of leagues) assert.ok(["LIIGA", "SHL", "NLA", "EXTRALIGA"].includes(lg), "unexpected league " + lg);
assert.ok(!games.some(g => /USHL|OHL|Belgian/.test(g.lg)), "foreign league leaked");
for (const g of games) {
  assert.ok(g.id && g.home && g.away, "missing identity");
  assert.ok(["pre", "in", "post"].includes(g.state));
  if (g.state === "post") assert.ok(Number.isFinite(g.homeScore) && Number.isFinite(g.awayScore), "final without score");
  if (g.state !== "in") assert.equal(g.period, null);
}
const liiga = games.filter(g => g.lg === "LIIGA" && g.state === "post");
assert.ok(liiga.length >= 1, "expected finished Liiga games in the sample");

// synthetic live record: period codes + period start
const live = parseFeed("SA÷4¬~ZA÷SWEDEN: SHL¬~AA÷x1¬AD÷1791000000¬AB÷2¬AC÷15¬AE÷Frolunda¬AF÷Rogle¬AG÷2¬AH÷1¬AO÷1791002400¬~");
assert.equal(live.length, 1);
assert.deepEqual([live[0].lg, live[0].state, live[0].period, live[0].periodStart, live[0].homeScore, live[0].awayScore], ["SHL", "in", 2, 1791002400, 2, 1]);
const ot = parseFeed("ZA÷FINLAND: Liiga¬~AA÷x2¬AD÷1¬AB÷2¬AC÷17¬AE÷A¬AF÷B¬AG÷1¬AH÷1¬AO÷5¬~");
assert.equal(ot[0].period, "OT");
assert.deepEqual(parseFeed("ZA÷CANADA: OHL¬~AA÷x3¬AD÷1¬AB÷2¬AC÷15¬AE÷A¬AF÷B¬AG÷0¬AH÷0¬~"), []);
// shootout / overtime / postponed handling
const so = parseFeed("ZA÷SWEDEN: SHL¬~AA÷s1¬AD÷1¬AB÷3¬AC÷11¬AE÷A¬AF÷B¬AG÷4¬AH÷3¬AT÷3¬AU÷3¬~");
assert.deepEqual([so[0].state, so[0].so, so[0].ot, so[0].regHome, so[0].regAway, so[0].homeScore, so[0].awayScore], ["post", true, false, 3, 3, 4, 3]);
const ot2 = parseFeed("ZA÷SWEDEN: SHL¬~AA÷s2¬AD÷1¬AB÷3¬AC÷10¬AE÷A¬AF÷B¬AG÷2¬AH÷1¬~");
assert.deepEqual([ot2[0].state, ot2[0].so, ot2[0].ot, ot2[0].regHome], ["post", false, true, null]);
const pp = parseFeed("ZA÷SWEDEN: SHL¬~AA÷s3¬AD÷1¬AB÷3¬AC÷4¬AE÷A¬AF÷B¬~");
assert.equal(pp[0].state, "pre", "a finished record with no score must not be a FINAL");
for (const g of games) if (g.so) assert.equal(Math.abs(g.homeScore - g.awayScore), 1, "a shootout final is decided by exactly one goal");
console.log("relay parser OK:", games.length, "games,", [...leagues].join("/"));
