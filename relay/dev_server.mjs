// node relay/dev_server.mjs [port]  -- local stand-in for the worker (same parser, fetches Flashscore live) so the app can be tested before/without deploying.
import http from "node:http";
import { parseFeed } from "./worker.js";

const port = Number(process.argv[2]) || 8766;
let cache = { t: 0, body: "" };
http.createServer(async (req, res) => {
  const cors = { "Access-Control-Allow-Origin": "*", "Access-Control-Allow-Methods": "GET, OPTIONS", "Access-Control-Allow-Headers": "Content-Type" };
  if (req.method === "OPTIONS") { res.writeHead(204, cors); return res.end(); }
  const url = new URL(req.url, "http://localhost");
  if (url.pathname !== "/hockey") { res.writeHead(404, cors); return res.end("{}"); }
  const tz = Math.round(Number(url.searchParams.get("tz"))) || -6;
  if (Date.now() - cache.t > 20000) {
    const r = await fetch(`https://local-global.flashscore.ninja/2/x/feed/f_4_0_${tz}_en_1`, { headers: { "X-Fsign": "SW9D1eZo" } });
    cache = { t: Date.now(), body: JSON.stringify({ ts: Date.now(), tz, source: "flashscore", games: parseFeed(await r.text()) }) };
  }
  res.writeHead(200, { "Content-Type": "application/json", ...cors });
  res.end(cache.body);
}).listen(port, () => console.log("dev relay on http://localhost:" + port + "/hockey"));
