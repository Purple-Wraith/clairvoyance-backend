#!/usr/bin/env python3
"""Ask Clairvoyance (owner chat, 2026-10-20): the in-browser tools (read-only, run against the ledger the page holds), the chat UI, error handling, safety and the phone layout. The Worker is stubbed.

    python3 scripts/test_chat_ui.py
"""
import functools, http.server, json, threading, unittest
from pathlib import Path
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
WORKER = "https://clairvoyance-scheduler.clairvoyance-reese.workers.dev"

LEDGER = [
    {"id": "a1", "date": "2026-10-12", "sport": "NHL", "betType": "OU", "betOn": "UNDER 6.5", "hA": "MIN", "awA": "VGK", "outcome": "win", "winProb": 0.653, "decOdds": 1.9, "lockedAt": 1790000000000, "hScore": 2, "aScore": 1, "reasoning": "PICK: UNDER 6.5 -- OPTIMAL"},
    {"id": "a2", "date": "2026-10-12", "sport": "NHL", "betType": "SPREAD", "betOn": "VGK +1.5", "hA": "MIN", "awA": "VGK", "outcome": "loss", "winProb": 0.72, "decOdds": 1.5, "lockedAt": 1790000001000},
    {"id": "a3", "date": "2026-10-13", "sport": "NHL", "betType": "OU", "betOn": "UNDER 5.5", "hA": "OTT", "awA": "STL", "outcome": "win", "winProb": 0.59, "decOdds": 1.8, "lockedAt": 1790000002000},
    {"id": "a4", "date": "2026-10-13", "sport": "CFB", "betType": "ML", "betOn": "Ohio State ML", "hA": "OSU", "awA": "MICH", "outcome": "pending", "winProb": 0.8, "decOdds": 1.3, "lockedAt": 1790000003000},
    {"id": "a5", "date": "2026-09-01", "sport": "NFL", "betType": "SPREAD", "betOn": "KC -3.5", "hA": "KC", "awA": "DEN", "outcome": "push", "winProb": 0.6, "decOdds": 1.91, "lockedAt": 1780000000000},
    {"id": "a6", "date": "2026-10-13", "sport": "NHL", "betType": "PARLAY", "betOn": "2-leg", "hA": "X", "awA": "Y", "outcome": "win", "winProb": 0.3, "decOdds": 5, "lockedAt": 1790000004000},
]


class Chat(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        H = type("Q", (http.server.SimpleHTTPRequestHandler,), {"log_message": lambda *a: None})
        cls.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(H, directory=str(ROOT / "docs")))
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        cls.pw = sync_playwright().start(); cls.b = cls.pw.chromium.launch()

    @classmethod
    def tearDownClass(cls):
        cls.b.close(); cls.pw.stop(); cls.srv.shutdown()

    def page(self, key="owner-key", width=390, stub=None):
        """stub(route, body) -> dict | (status, dict) for POST /chat; GET /chat returns a status blob."""
        ctx = self.b.new_context(viewport={"width": width, "height": 800}, is_mobile=width < 500)
        pg = ctx.new_page(); pg.posts = []; pg.errors = []
        pg.on("pageerror", lambda e: pg.errors.append(str(e)))
        def handler(route):
            req = route.request
            if req.url.startswith(WORKER):
                if req.method == "OPTIONS":
                    return route.fulfill(status=204, headers={"access-control-allow-origin": "*", "access-control-allow-headers": "*", "access-control-allow-methods": "*"})
                if req.method == "GET":
                    return route.fulfill(status=200, content_type="application/json", headers={"access-control-allow-origin": "*"}, body=json.dumps({"ok": True, "configured": {"anthropicKey": True}, "budget": {"spentToday": 0.12, "capToday": 1.5, "spentMonth": 0.5, "capMonth": 15}}))
                body = json.loads(req.post_data or "{}"); pg.posts.append(body)
                out = stub(body) if stub else {"kind": "final", "text": "ok", "budget": {"spentToday": 0.13, "capToday": 1.5}}
                st, js = out if isinstance(out, tuple) else (200, out)
                return route.fulfill(status=st, content_type="application/json", headers={"access-control-allow-origin": "*"}, body=json.dumps(js))
            return route.continue_() if req.url.startswith(self.base) else route.abort()
        pg.route("**/*", handler)
        pg.add_init_script(f"try{{localStorage.removeItem('cv_chat_v1');{'localStorage.setItem(\"cv_trigger_key\",\"%s\")' % key if key else 'localStorage.removeItem(\"cv_trigger_key\")'}}}catch(e){{}}")
        pg.goto(self.base + "/app.html?nosb=1&chat=1", wait_until="domcontentloaded", timeout=60000)
        pg.wait_for_function("typeof _chatSend==='function'&&typeof getP==='function'&&!!document.getElementById('cv-ask-fab')", timeout=60000)
        pg.evaluate("(l) => { window.getP = () => l.map(x => ({...x})); }", LEDGER)             # the page also pulls the real ledger in the background: pin it to the fixture
        return ctx, pg

    # ── visibility
    def test_hidden_without_the_owner_key_and_shown_with_it(self):
        ctx, pg = self.page(key=None); pg.wait_for_timeout(300)
        self.assertFalse(pg.evaluate("document.getElementById('cv-ask-fab').classList.contains('on')"))
        pg.evaluate("localStorage.setItem('cv_trigger_key','k'); _chatFab()")
        self.assertTrue(pg.evaluate("document.getElementById('cv-ask-fab').classList.contains('on')")); ctx.close()
        ctx, pg = self.page()
        self.assertTrue(pg.is_visible("#cv-ask-fab")); ctx.close()

    def test_automated_browsers_do_not_get_it_unless_asked(self):
        ctx = self.b.new_context(); pg = ctx.new_page(); pg.route("**/*", lambda r: r.continue_() if r.request.url.startswith(self.base) else r.abort())
        pg.goto(self.base + "/app.html?nosb=1", wait_until="domcontentloaded", timeout=60000); pg.wait_for_timeout(2500)
        self.assertFalse(pg.evaluate("!!document.getElementById('cv-ask-fab')")); ctx.close()

    # ── tools
    def test_ledger_query_totals_units_groups_and_filters(self):
        ctx, pg = self.page()
        r = pg.evaluate("_chatT_ledger_query({sport:'NHL', outcome:'settled'})")
        self.assertEqual(r["matched"], 3)                                                       # the PARLAY is never counted
        self.assertEqual((r["summary"]["wins"], r["summary"]["losses"], r["summary"]["pending"]), (2, 1, 0))
        self.assertAlmostEqual(r["summary"]["units"], 0.9 + 0.8 - 1, places=2)                  # +0.9 +0.8 -1
        self.assertEqual(r["summary"]["winPct"], 66.7)
        g = pg.evaluate("_chatT_ledger_query({sport:'NHL', group_by:'side'})")["groups"]
        self.assertEqual({x["key"]: x["n"] for x in g}, {"UNDER": 2, "favourite": 1})
        t = pg.evaluate("_chatT_ledger_query({date_from:'2026-10-13', date_to:'2026-10-13', outcome:'pending'})")
        self.assertEqual([x["id"] for x in t["rows"]], ["a4"])
        self.assertEqual(pg.evaluate("_chatT_ledger_query({text:'vgk'})")["matched"], 2)
        self.assertEqual(pg.evaluate("_chatT_ledger_query({min_prob:0.7})")["matched"], 2)
        self.assertEqual(pg.evaluate("_chatT_ledger_query({group_by:'month'})")["groups"][0]["key"], "2026-10")
        self.assertEqual(len(pg.evaluate("_chatT_ledger_query({limit:1})")["rows"]), 1)
        self.assertEqual(len(pg.evaluate("_chatT_ledger_query({limit:9999})")["rows"]), 5)    # capped at the rows that exist (40 max)
        ctx.close()

    def test_get_pick_and_unknown_tools(self):
        ctx, pg = self.page()
        p = pg.evaluate("_chatT_get_pick({id:'a1'})"); self.assertIn("OPTIMAL", p["reasoning"]); self.assertEqual(p["score"], "1-2")
        self.assertIn("error", pg.evaluate("_chatT_get_pick({id:'nope'})"))
        r = pg.evaluate("_chatRunTool({id:'x', name:'delete_everything', input:{}})"); self.assertTrue(r["is_error"])
        self.assertEqual(sorted(pg.evaluate("Object.keys(_CHAT_TOOLS)")), ["engine_status", "get_pick", "ledger_query", "schedule", "team_stats"])   # read-only: nothing that writes
        big = pg.evaluate("_chatRunTool({id:'y', name:'ledger_query', input:{}}).then(r=>r.content.length)"); self.assertLess(big, 11100)
        ctx.close()

    def test_schedule_and_team_tools_answer_from_loaded_data(self):
        ctx, pg = self.page()
        s = pg.evaluate("_chatT_schedule({sport:'nhl'})"); self.assertEqual(s["sport"], "nhl"); self.assertIn("games", s)
        t = pg.evaluate("_chatT_team_stats({sport:'nhl', team:'COL'})"); self.assertEqual(t["team"], "COL"); self.assertIn("gf60", t); self.assertIn("lastSeasonWeight", t)
        e = pg.evaluate("_chatT_team_stats({sport:'shl', team:'Farjestad'})"); self.assertIn("thisSeasonWeight", e); self.assertIn("display-only", e["note"])
        self.assertIn("error", pg.evaluate("_chatT_team_stats({sport:'nhl', team:'Nowhere'})"))
        self.assertIn("error", pg.evaluate("_chatT_schedule({sport:'curling'})"))
        n = pg.evaluate("_chatT_team_stats({sport:'nba', team:'DEN'})"); self.assertEqual(n["team"], "DEN"); self.assertIn("elo", n)
        ctx.close()

    # ── UI flow
    def test_a_question_runs_the_tool_loop_and_shows_the_answer(self):
        step = {"n": 0}
        def stub(body):
            step["n"] += 1
            if step["n"] == 1:
                return {"kind": "tool_use", "content": [{"type": "text", "text": "Looking."}, {"type": "tool_use", "id": "tu_1", "name": "ledger_query", "input": {"sport": "NHL", "outcome": "settled"}}],
                        "toolCalls": [{"id": "tu_1", "name": "ledger_query", "input": {"sport": "NHL", "outcome": "settled"}}], "budget": {"spentToday": 0.13, "capToday": 1.5}}
            return {"kind": "final", "text": "NHL is **2-1** (+0.7u).\n\n| Type | Record |\n|---|---|\n| OU | 2-0 |\n\n- small sample", "budget": {"spentToday": 0.14, "capToday": 1.5}}
        ctx, pg = self.page(stub=stub)
        pg.click("#cv-ask-fab"); pg.wait_for_selector("#cv-ask.on")
        pg.fill("#ask-text", "How is the NHL doing?"); pg.click("#ask-send")
        pg.wait_for_selector(".ask-m.a:has-text('NHL is')", timeout=15000)
        html = pg.inner_html("#ask-log")
        self.assertIn("<b>2-1</b>", html); self.assertIn("<table>", html); self.assertIn("<li>small sample</li>", html); self.assertIn("looked up: ledger query", html)
        self.assertEqual(len(pg.posts), 2)
        self.assertEqual(pg.posts[0]["model"], "balanced"); self.assertEqual(pg.posts[0]["messages"], [{"role": "user", "content": "How is the NHL doing?"}])
        tr = pg.posts[1]["messages"][-1]["content"][0]
        self.assertEqual(tr["type"], "tool_result"); self.assertEqual(tr["tool_use_id"], "tu_1"); self.assertEqual(json.loads(tr["content"])["summary"]["wins"], 2)    # real numbers from the real ledger
        self.assertEqual(pg.posts[1]["messages"][-2]["content"][1]["type"], "tool_use")                                                                              # the assistant turn goes back unchanged
        self.assertIn("$0.14 / $1.50", pg.inner_text("#ask-bud")); self.assertFalse(pg.is_disabled("#ask-send")); self.assertEqual(pg.errors, []); ctx.close()

    def test_history_model_choice_and_new_chat_persist(self):
        ctx, pg = self.page()
        pg.click("#cv-ask-fab"); pg.click("#ask-seg [data-m=deep]")
        pg.fill("#ask-text", "hello"); pg.press("#ask-text", "Enter"); pg.wait_for_selector(".ask-m.a:has-text('ok')")
        self.assertEqual(pg.posts[0]["model"], "deep")
        pg.fill("#ask-text", "and again"); pg.click("#ask-send"); pg.wait_for_function("document.querySelectorAll('.ask-m.a').length>=2")
        self.assertEqual(len(pg.posts[1]["messages"]), 3)                                        # prior exchange travels with the next question
        saved = pg.evaluate("JSON.parse(localStorage.getItem('cv_chat_v1'))"); self.assertEqual(saved["model"], "deep"); self.assertEqual(len(saved["hist"]), 4)
        pg.click("#ask-new"); self.assertEqual(pg.evaluate("JSON.parse(localStorage.getItem('cv_chat_v1')).hist.length"), 0)
        self.assertTrue(pg.is_visible(".ask-chip")); ctx.close()

    def test_errors_are_shown_plainly_and_a_bad_key_hides_chat(self):
        ctx, pg = self.page(stub=lambda b: (429, {"error": "Today's chat budget ($1.50) is used up. It resets at midnight Mountain time.", "budget": {"spentToday": 1.5, "capToday": 1.5}}))
        pg.click("#cv-ask-fab"); pg.fill("#ask-text", "q"); pg.click("#ask-send"); pg.wait_for_selector(".ask-m.e")
        self.assertIn("budget", pg.inner_text(".ask-m.e")); self.assertIn("$1.50 / $1.50", pg.inner_text("#ask-bud"))
        self.assertEqual(pg.evaluate("JSON.parse(localStorage.getItem('cv_chat_v1')).hist.length"), 0)   # the failed question is not kept as history
        ctx.close()
        ctx, pg = self.page(stub=lambda b: (401, {"error": "wrong key"}))
        pg.click("#cv-ask-fab"); pg.fill("#ask-text", "q"); pg.click("#ask-send"); pg.wait_for_selector(".ask-m.e")
        self.assertIsNone(pg.evaluate("localStorage.getItem('cv_trigger_key')")); self.assertFalse(pg.evaluate("document.getElementById('cv-ask-fab').classList.contains('on')"))
        self.assertIn("rejected", pg.inner_text(".ask-m.e")); ctx.close()

    def test_a_runaway_tool_loop_stops(self):
        tu = {"kind": "tool_use", "content": [{"type": "tool_use", "id": "t", "name": "engine_status", "input": {}}], "toolCalls": [{"id": "t", "name": "engine_status", "input": {}}]}
        ctx, pg = self.page(stub=lambda b: tu)
        pg.click("#cv-ask-fab"); pg.fill("#ask-text", "q"); pg.click("#ask-send"); pg.wait_for_selector(".ask-m.e", timeout=30000)
        self.assertLessEqual(len(pg.posts), 7); self.assertIn("too many lookups", pg.inner_text(".ask-m.e")); self.assertFalse(pg.is_disabled("#ask-send")); ctx.close()

    def test_replies_cannot_inject_html(self):
        evil = '<img src=x onerror="window.__pwned=1"> <script>window.__pwned=2</script> **ok** `<b>x</b>`'
        ctx, pg = self.page(stub=lambda b: {"kind": "final", "text": evil})
        pg.click("#cv-ask-fab"); pg.fill("#ask-text", "q"); pg.click("#ask-send"); pg.wait_for_selector(".ask-m.a:has-text('ok')"); pg.wait_for_timeout(300)
        self.assertIsNone(pg.evaluate("window.__pwned || null")); self.assertEqual(pg.evaluate("document.querySelectorAll('#ask-log img, #ask-log script').length"), 0)
        self.assertIn("&lt;img", pg.inner_html("#ask-log")); ctx.close()

    def test_a_missing_anthropic_key_shows_the_setup_step(self):
        ctx, pg = self.page()
        pg.click("#cv-ask-fab"); pg.wait_for_function("_chat.configured === true")                      # the status call has landed
        pg.evaluate("_chat.configured = false; _chatRender()")
        self.assertIn("wrangler secret put ANTHROPIC_API_KEY", pg.inner_text("#ask-log")); ctx.close()

    # ── phone layout
    def test_phone_layout_fits(self):
        for w in (320, 375, 414):
            ctx, pg = self.page(width=w)
            pg.click("#cv-ask-fab"); pg.wait_for_selector("#cv-ask.on")
            pg.evaluate("_chat.hist=[{role:'user',content:'q'},{role:'assistant',content:'| a | b | c | d | e |\\n|---|---|---|---|---|\\n| 1 | 2 | 3 | 4 | 5 |\\n\\n'+'longword'.repeat(30)}]; _chatRender()")
            m = pg.evaluate("""() => { const s = document.getElementById('cv-ask').getBoundingClientRect(), i = document.getElementById('ask-form').getBoundingClientRect();
              return {docW: document.documentElement.scrollWidth, vw: innerWidth, sheetR: s.right, sheetL: s.left, sheetB: s.bottom, vh: innerHeight, inputB: i.bottom, inputV: i.top >= 0,
                logOver: document.getElementById('ask-log').scrollWidth > document.getElementById('ask-log').clientWidth + 1}; }""")
            self.assertLessEqual(m["docW"], m["vw"] + 1, (w, m)); self.assertLessEqual(m["sheetR"], m["vw"] + 1, (w, m)); self.assertGreaterEqual(m["sheetL"], -1, (w, m))
            self.assertLessEqual(m["inputB"], m["vh"] + 1, (w, m)); self.assertTrue(m["inputV"], (w, m)); self.assertFalse(m["logOver"], (w, m))
            self.assertGreaterEqual(pg.evaluate("parseFloat(getComputedStyle(document.getElementById('ask-text')).fontSize)"), 16)       # 16px: iOS does not zoom the page on focus
            ctx.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
