#!/usr/bin/env python3
"""docs/app.html + docs/config.js: the optional Cloudflare R2 mirror of the two high-churn files (live_data.json, picks_backup.json).

  * window.CV_R2_BASE empty (the shipped default): the app behaves exactly as before -- both files come from this site, nothing is ever requested from R2.
  * set: R2 is tried first; ANY problem (HTTP error, a 200 that is not the expected JSON, a missing CORS header, a network failure) falls back to the same-origin copy.
  * a base that is not https:// (or a localhost test URL) is ignored, so a mistyped / hostile value can never redirect the ledger fallback.

Runs the real app (Playwright, ?nosb=1 so Supabase is never touched) against a local server of docs/; the "R2 bucket" is a stubbed http://localhost:4010 origin (cross-origin to the
127.0.0.1 app, so CORS is genuinely enforced).  Google Fonts and every other host are aborted.

    python3 scripts/test_r2_app.py
"""
import functools, http.server, json, re, socketserver, threading, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
R2 = "http://localhost:4010"
REAL_LIVE = json.loads((ROOT / "docs" / "live_data.json").read_text())
REAL_PICKS = json.loads((ROOT / "docs" / "picks_backup.json").read_text())
CORS = {"access-control-allow-origin": "*"}


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a, **k):
        pass


class QuietServer(socketserver.ThreadingTCPServer):
    def handle_error(self, *a):                                  # a page closing mid-transfer is not an error worth a traceback
        pass


class R2App(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        socketserver.TCPServer.allow_reuse_address = True
        cls.srv = QuietServer(("127.0.0.1", 0), functools.partial(Quiet, directory=str(ROOT / "docs")))
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.pw = sync_playwright().start()
        cls.b = cls.pw.chromium.launch()

    @classmethod
    def tearDownClass(cls):
        cls.b.close(); cls.pw.stop(); cls.srv.shutdown(); cls.srv.server_close()

    def open(self, base=None, r2=None):
        """r2(route, kind) -> fulfils a stubbed-bucket request ('live' | 'picks').  base=None leaves CV_R2_BASE untouched (the shipped default)."""
        ctx = self.b.new_context(viewport={"width": 1300, "height": 900})
        if base is not None:
            ctx.add_init_script(f"window.CV_R2_BASE = {json.dumps(base)};")
        pg = ctx.new_page()
        pg.set_default_timeout(60000)
        self.errors, self.logs, self.same, self.r2_reqs, self.other = [], [], [], [], []
        pg.on("pageerror", lambda e: self.errors.append(str(e)))
        pg.on("console", lambda m: self.logs.append(m.text))
        origin = f"http://127.0.0.1:{self.srv.server_address[1]}/"

        def handle(route):
            url = route.request.url
            if url.startswith(origin):
                self.same.append(url[len(origin):])
                return route.continue_()
            if url.startswith(R2 + "/"):
                self.r2_reqs.append(url[len(R2):])
                kind = "live" if "/live_data.json" in url else "picks"
                if r2:
                    return r2(route, kind)
                return route.abort()
            self.other.append(url)
            return route.abort()                                                    # fonts, the live relay, ESPN, anything else
        pg.route("**/*", handle)
        pg.goto(origin + "app.html?nosb=1", wait_until="domcontentloaded")
        return pg

    def live_ts(self, pg):
        pg.wait_for_function("window.__CV_FILE_LIVE_TS !== undefined")
        return pg.evaluate("window.__CV_FILE_LIVE_TS")

    def same_live(self):
        return [u for u in self.same if u.startswith("live_data.json")]

    @staticmethod
    def ok_live(ts="R2-STUB-TS"):
        return lambda route, kind: route.fulfill(status=200, headers=CORS, content_type="application/json",
                                                 body=json.dumps({**REAL_LIVE, "ts": ts}) if kind == "live" else json.dumps(REAL_PICKS[:2]))

    def load_picks(self, pg):
        n0 = len(self.logs)
        pg.evaluate("loadPicksFromBackupJSON()")
        line = next(l for l in self.logs[n0:] if "from GitHub backup" in l)
        return int(re.search(r"Loaded (\d+) bets", line).group(1))

    # ── default: byte-for-byte today's behaviour ─────────────────────────────────────────────────────────────────────
    def test_default_base_is_empty_and_r2_is_never_contacted(self):
        self.assertRegex((ROOT / "docs" / "config.js").read_text(), r"window\.CV_R2_BASE = window\.CV_R2_BASE \|\| '';")
        pg = self.open()
        self.assertEqual(pg.evaluate("window.CV_R2_BASE"), "")
        self.assertEqual(self.live_ts(pg), REAL_LIVE["ts"])                          # the app booted and loaded the live file from this site
        self.assertTrue(self.same_live())
        self.assertEqual(self.r2_reqs, [])
        self.assertEqual(self.errors, [])
        self.assertEqual(self.load_picks(pg), len(REAL_PICKS))                       # the ledger fallback reads the same-origin backup
        self.assertEqual(self.r2_reqs, [])
        self.assertTrue(any(u.startswith("picks_backup.json?t=") for u in self.same))

    def test_empty_base_helper_is_exactly_the_old_fetch(self):
        pg = self.open()
        self.live_ts(pg)
        calls = pg.evaluate("""async () => {
            const seen = [], orig = window.fetch;
            window.fetch = (...a) => { seen.push([a[0], a[1] === undefined ? 'undef' : JSON.stringify(a[1])]); return Promise.resolve(new Response('{}')); };
            await _cvHotFetch('picks_backup.json', '?t=1', undefined, () => false);
            await _cvHotFetch('live_data.json', '?2', {cache: 'no-cache'}, () => false);
            window.fetch = orig;
            return seen;
        }""")
        self.assertEqual(calls, [["picks_backup.json?t=1", "undef"], ["live_data.json?2", '{"cache":"no-cache"}']])

    # ── base set: R2 first ───────────────────────────────────────────────────────────────────────────────────────────
    def test_base_set_reads_live_data_from_r2_first(self):
        pg = self.open(R2, self.ok_live())
        self.assertEqual(self.live_ts(pg), "R2-STUB-TS")
        self.assertTrue(any(u.startswith("/live_data.json?") for u in self.r2_reqs))
        self.assertEqual(self.same_live(), [])                                       # the same-origin copy was not even requested
        self.assertEqual(self.errors, [])

    def test_base_set_reads_the_ledger_backup_from_r2_first(self):
        pg = self.open(R2, self.ok_live())
        self.live_ts(pg)
        self.assertEqual(self.load_picks(pg), 2)                                     # the stub holds 2 rows, the repo copy thousands
        self.assertTrue(any(u.startswith("/picks_backup.json?t=") for u in self.r2_reqs))
        self.assertFalse(any(u.startswith("picks_backup.json") for u in self.same))

    def test_trailing_slash_in_the_base_is_tolerated(self):
        pg = self.open(R2 + "/", self.ok_live())
        self.assertEqual(self.live_ts(pg), "R2-STUB-TS")
        self.assertFalse(any(u.startswith("//") for u in self.r2_reqs), self.r2_reqs)

    # ── base set but R2 misbehaves: always the same-origin copy ──────────────────────────────────────────────────────
    def assert_falls_back(self, r2):
        pg = self.open(R2, r2)
        self.assertEqual(self.live_ts(pg), REAL_LIVE["ts"])
        self.assertTrue(self.r2_reqs and self.same_live())                           # R2 was tried, then this site
        self.assertEqual(self.errors, [])
        self.assertEqual(self.load_picks(pg), len(REAL_PICKS))

    def test_http_error_falls_back(self):
        self.assert_falls_back(lambda route, kind: route.fulfill(status=500, headers=CORS, body="oops"))

    def test_not_found_falls_back(self):
        self.assert_falls_back(lambda route, kind: route.fulfill(status=404, headers=CORS, body="nope"))

    def test_ok_response_that_is_not_json_falls_back(self):
        self.assert_falls_back(lambda route, kind: route.fulfill(status=200, headers=CORS, content_type="text/html", body="<html>custom domain error page</html>"))

    def test_json_of_the_wrong_shape_falls_back(self):
        self.assert_falls_back(lambda route, kind: route.fulfill(status=200, headers=CORS, content_type="application/json", body="{}" if kind == "live" else "[]"))

    def test_missing_cors_header_falls_back(self):
        """The bucket's CORS rule is not set up yet: the browser blocks the read, the app quietly uses this site.  Needs a REAL cross-origin server -- Playwright-fulfilled responses bypass CORS."""
        send_cors = {"on": False}

        class Bucket(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                body = json.dumps({**REAL_LIVE, "ts": "REAL-R2"} if "/live_data.json" in self.path else REAL_PICKS[:2]).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                if send_cors["on"]:
                    self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(body)
        bucket = QuietServer(("127.0.0.1", 0), Bucket)
        threading.Thread(target=bucket.serve_forever, daemon=True).start()
        self.addCleanup(lambda: (bucket.shutdown(), bucket.server_close()))
        base = f"http://localhost:{bucket.server_address[1]}"
        origin = f"http://127.0.0.1:{self.srv.server_address[1]}/"        # a different host name than the bucket's -> cross-origin

        def run(cors):
            send_cors["on"] = cors
            ctx = self.b.new_context()
            ctx.add_init_script(f"window.CV_R2_BASE = {json.dumps(base)};")
            pg = ctx.new_page()
            pg.set_default_timeout(60000)
            same = []
            pg.on("request", lambda r: same.append(r.url) if r.url.startswith(origin) else None)
            pg.route("**/*", lambda route: route.continue_() if route.request.url.startswith((origin, base + "/")) else route.abort())
            pg.goto(origin + "app.html?nosb=1", wait_until="domcontentloaded")
            pg.wait_for_function("window.__CV_FILE_LIVE_TS !== undefined")
            ts = pg.evaluate("window.__CV_FILE_LIVE_TS")
            ctx.close()
            return ts, [u for u in same if "/live_data.json" in u]
        ts, same_live = run(cors=False)
        self.assertEqual(ts, REAL_LIVE["ts"])                       # blocked by CORS -> the same-origin copy
        self.assertTrue(same_live)
        ts, same_live = run(cors=True)
        self.assertEqual(ts, "REAL-R2")                             # once the bucket's CORS rule exists the very same page reads R2
        self.assertEqual(same_live, [])

    def test_network_failure_falls_back(self):
        self.assert_falls_back(lambda route, kind: route.abort("connectionrefused"))

    # ── unsafe bases are ignored ─────────────────────────────────────────────────────────────────────────────────────
    def test_non_https_or_non_url_bases_are_ignored(self):
        for bad in ("http://evil.example.com", "javascript:alert(1)", "ftp://x.example.com", "//evil.example.com", "evil.example.com", "https://", "   "):
            with self.subTest(base=bad):
                pg = self.open(bad, self.ok_live("SHOULD-NOT-BE-USED"))
                self.assertEqual(pg.evaluate("_cvR2Base()"), "")
                self.assertEqual(self.live_ts(pg), REAL_LIVE["ts"])
                self.assertEqual(self.r2_reqs, [])
                self.assertFalse([u for u in self.other if "evil" in u or "x.example.com" in u], self.other)
                pg.context.close()

    def test_accepted_bases(self):
        pg = self.open()
        self.live_ts(pg)
        for base, want in (("https://pub-abc.r2.dev", "https://pub-abc.r2.dev"), ("https://data.clairvoyanceengine.info/", "https://data.clairvoyanceengine.info"),
                           ("http://localhost:4010", "http://localhost:4010"), ("http://127.0.0.1:9", "http://127.0.0.1:9"), (None, ""), (123, "")):
            with self.subTest(base=base):
                self.assertEqual(pg.evaluate("b => { window.CV_R2_BASE = b; return _cvR2Base(); }", base), want)

    def test_app_html_and_index_html_stay_identical(self):
        self.assertEqual((ROOT / "docs" / "app.html").read_bytes(), (ROOT / "docs" / "index.html").read_bytes())


if __name__ == "__main__":
    unittest.main(verbosity=2)
