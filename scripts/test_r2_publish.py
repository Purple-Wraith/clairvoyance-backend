#!/usr/bin/env python3
"""scripts/r2_publish.py -- uploads files to Cloudflare R2 over the S3 API (standard-library SigV4).  Nothing leaves the machine:

  * the signer is checked against the official AWS Signature V4 documentation example,
  * the logic runs against a fake transport (recorded requests) AND a real urllib round trip to a tiny in-process S3 imitation on 127.0.0.1,
  * covered: the no-credentials no-op (exit 0, clear log line, no request), Content-Type / Cache-Control / hash metadata, skip-when-unchanged, re-upload when changed, refusing corrupt
    JSON, retries, failures -> exit 1, dry-run, --force, key = prefix + basename.

    python3 scripts/test_r2_publish.py
"""
from __future__ import annotations

import contextlib
import hashlib
import http.server
import io
import json
import socketserver
import tempfile
import threading
import unittest
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
import r2_publish as R  # noqa: E402

ENV = {"R2_ACCOUNT_ID": "acct123", "R2_ACCESS_KEY_ID": "AKIAEXAMPLE", "R2_SECRET_ACCESS_KEY": "secret/key", "R2_BUCKET": "cv-data"}


class FakeTransport:
    """Records requests; behaves like a tiny bucket (HEAD returns the stored sha256 metadata, PUT stores)."""

    def __init__(self):
        self.calls = []
        self.store = {}                     # key -> (body, headers)
        self.fail_put_with = []             # statuses to return for the next PUTs (consumed in order)
        self.fail_head_with = []

    def __call__(self, method, url, headers, body):
        key = url.split("/", 4)[4]          # https://acct.r2.cloudflarestorage.com/bucket/<key>
        self.calls.append({"method": method, "url": url, "key": key, "headers": {k.lower(): v for k, v in headers.items()}, "body": body})
        if method == "HEAD":
            if self.fail_head_with:
                return self.fail_head_with.pop(0), {}, b""
            if key in self.store:
                return 200, {"x-amz-meta-sha256": self.store[key][1].get("x-amz-meta-sha256", "")}, b""
            return 404, {}, b""
        if method == "PUT":
            if self.fail_put_with:
                return self.fail_put_with.pop(0), {}, b"boom"
            self.store[key] = (body, {k.lower(): v for k, v in headers.items()})
            return 200, {}, b""
        return 405, {}, b""

    def puts(self):
        return [c for c in self.calls if c["method"] == "PUT"]


class Signer(unittest.TestCase):
    def test_matches_the_aws_documentation_example(self):
        """https://docs.aws.amazon.com/AmazonS3/latest/API/sig-v4-header-based-auth.html -- 'GET Object' example."""
        h = R.sign_v4("GET", "examplebucket.s3.amazonaws.com", "/test.txt", {"Range": "bytes=0-9"},
                      "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855", "AKIAIOSFODNN7EXAMPLE",
                      "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY", "20130524T000000Z", "us-east-1")
        self.assertEqual(h["Authorization"], "AWS4-HMAC-SHA256 Credential=AKIAIOSFODNN7EXAMPLE/20130524/us-east-1/s3/aws4_request, SignedHeaders=host;range;x-amz-content-sha256;x-amz-date, "
                                             "Signature=f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41")

    def test_put_example_from_the_documentation(self):
        """'PUT Object' example (body 'Welcome to Amazon S3.', storage class header)."""
        body = b"Welcome to Amazon S3."
        h = R.sign_v4("PUT", "examplebucket.s3.amazonaws.com", "/test%24file.text",
                      {"Date": "Fri, 24 May 2013 00:00:00 GMT", "x-amz-storage-class": "REDUCED_REDUNDANCY"},
                      hashlib.sha256(body).hexdigest(), "AKIAIOSFODNN7EXAMPLE", "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY", "20130524T000000Z", "us-east-1")
        self.assertTrue(h["Authorization"].endswith("Signature=98ad721746da40c64f1a55b78f14c238d841ea1380cd77a1b5971af0ece108bd"), h["Authorization"])


class NoCredentials(unittest.TestCase):
    def test_is_a_logged_noop_that_exits_zero(self):
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "live_data.json"
            f.write_text("{}")
            calls = []
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = R.main(["--root", td, "live_data.json"], env={}, transport=lambda *a: calls.append(a) or (200, {}, b""))
            self.assertEqual(rc, 0)
            self.assertEqual(calls, [])                                              # no request at all
            self.assertIn("R2 not configured", out.getvalue())
            self.assertIn("R2_ACCOUNT_ID", out.getvalue())

    def test_any_single_missing_variable_is_enough(self):
        for drop in ENV:
            env = {k: v for k, v in ENV.items() if k != drop}
            cfg, missing = R.config_from_env(env)
            self.assertIsNone(cfg)
            self.assertEqual(missing, [drop])
        self.assertIsNotNone(R.config_from_env(ENV)[0])
        self.assertIsNone(R.config_from_env({**ENV, "R2_BUCKET": "  "})[0])          # blank counts as missing (an unset GitHub secret is an empty string)


class Upload(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.root = Path(self.td.name)
        (self.root / "docs").mkdir()
        self.fake = FakeTransport()
        self.sleeps = []
        self.client = R.R2Client(R.config_from_env(ENV)[0], transport=self.fake, sleep=self.sleeps.append,
                                 now=lambda: __import__("datetime").datetime(2026, 10, 10, 12, 0, 0, tzinfo=__import__("datetime").timezone.utc))

    def tearDown(self):
        self.td.cleanup()

    def write(self, rel, text):
        p = self.root / rel
        p.write_text(text)
        return p

    def test_uploads_with_content_type_cache_control_hash_and_a_signature(self):
        body = json.dumps([{"id": 1}])
        self.write("docs/picks_backup.json", body)
        res = R.publish(self.client, ["docs/picks_backup.json"], self.root)
        self.assertEqual(res, {"docs/picks_backup.json": "uploaded"})
        put = self.fake.puts()[0]
        self.assertEqual(put["url"], "https://acct123.r2.cloudflarestorage.com/cv-data/picks_backup.json")          # key = base name, path-style
        self.assertEqual(put["body"], body.encode())
        h = put["headers"]
        self.assertEqual(h["content-type"], "application/json; charset=utf-8")
        self.assertEqual(h["cache-control"], "no-cache")
        self.assertEqual(h["x-amz-meta-sha256"], hashlib.sha256(body.encode()).hexdigest())
        self.assertEqual(h["x-amz-content-sha256"], hashlib.sha256(body.encode()).hexdigest())
        self.assertEqual(h["x-amz-date"], "20261010T120000Z")
        self.assertRegex(h["authorization"], r"^AWS4-HMAC-SHA256 Credential=AKIAEXAMPLE/20261010/auto/s3/aws4_request, SignedHeaders=cache-control;content-type;host;x-amz-content-sha256;x-amz-date;x-amz-meta-sha256, Signature=[0-9a-f]{64}$")

    def test_unchanged_content_is_skipped_changed_content_is_uploaded(self):
        self.write("docs/live_data.json", '{"ts": "1"}')
        self.assertEqual(R.publish(self.client, ["docs/live_data.json"], self.root)["docs/live_data.json"], "uploaded")
        n_puts = len(self.fake.puts())
        again = R.publish(self.client, ["docs/live_data.json"], self.root)
        self.assertEqual(again["docs/live_data.json"], "unchanged")
        self.assertEqual(len(self.fake.puts()), n_puts)                                  # one HEAD, no PUT
        self.write("docs/live_data.json", '{"ts": "2"}')
        self.assertEqual(R.publish(self.client, ["docs/live_data.json"], self.root)["docs/live_data.json"], "uploaded")
        self.assertEqual(len(self.fake.puts()), n_puts + 1)
        self.assertEqual(R.publish(self.client, ["docs/live_data.json"], self.root, force=True)["docs/live_data.json"], "uploaded")      # --force re-uploads even when equal

    def test_object_without_a_hash_is_replaced(self):
        self.write("docs/live_data.json", "{}")
        self.fake.store["live_data.json"] = (b"{}", {})                                  # exists, but was uploaded by something else (no metadata)
        self.assertEqual(R.publish(self.client, ["docs/live_data.json"], self.root)["docs/live_data.json"], "uploaded")

    def test_corrupt_json_is_never_uploaded(self):
        self.write("docs/live_data.json", '{"ts": ')
        self.assertEqual(R.publish(self.client, ["docs/live_data.json"], self.root)["docs/live_data.json"], "invalid-json")
        self.assertEqual(self.fake.calls, [])

    def test_missing_file_is_skipped_and_prefix_and_other_types(self):
        self.write("docs/bet_history.csv", "a,b\n1,2\n")
        res = R.publish(self.client, ["docs/nope.json", "docs/bet_history.csv"], self.root, prefix="v1/")
        self.assertEqual(res, {"docs/nope.json": "missing", "docs/bet_history.csv": "uploaded"})
        self.assertEqual(self.fake.puts()[0]["key"], "v1/bet_history.csv")
        self.assertEqual(self.fake.puts()[0]["headers"]["content-type"], "text/csv; charset=utf-8")

    def test_dry_run_compares_but_uploads_nothing(self):
        self.write("docs/live_data.json", "{}")
        self.assertEqual(R.publish(self.client, ["docs/live_data.json"], self.root, dry_run=True)["docs/live_data.json"], "dry-run")
        self.assertEqual(self.fake.puts(), [])

    def test_server_errors_are_retried_then_succeed(self):
        self.write("docs/live_data.json", "{}")
        self.fake.fail_put_with = [503, 500]
        self.assertEqual(R.publish(self.client, ["docs/live_data.json"], self.root)["docs/live_data.json"], "uploaded")
        self.assertEqual(len(self.fake.puts()), 3)
        self.assertEqual(self.sleeps, [2, 4])                                            # backoff

    def test_persistent_failure_is_reported_and_main_exits_one(self):
        self.write("docs/live_data.json", "{}")
        self.fake.fail_put_with = [403, 403, 403]
        self.assertEqual(R.publish(self.client, ["docs/live_data.json"], self.root)["docs/live_data.json"], "failed")
        self.assertEqual(len(self.fake.puts()), 1)                                       # 4xx is not retried
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = R.main(["--root", str(self.root), "docs/live_data.json"], env=ENV, transport=lambda m, u, h, b: (403, {}, b"AccessDenied"), sleep=lambda s: None)
        self.assertEqual(rc, 1)
        self.assertIn("AccessDenied", out.getvalue())

    def test_transport_exceptions_are_retried(self):
        self.write("docs/live_data.json", "{}")
        seq = [ConnectionError("reset"), ConnectionError("reset")]

        def flaky(method, url, headers, body):
            if seq:
                raise seq.pop(0)
            return self.fake(method, url, headers, body)
        client = R.R2Client(self.client.cfg, transport=flaky, sleep=lambda s: None)
        self.assertEqual(R.publish(client, ["docs/live_data.json"], self.root)["docs/live_data.json"], "uploaded")


class RealTransportRoundTrip(unittest.TestCase):
    """The default urllib transport against an in-process S3 imitation on 127.0.0.1 (custom endpoint via R2_ENDPOINT)."""

    @classmethod
    def setUpClass(cls):
        store = cls.store = {}
        cls.seen = []

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_HEAD(self):
                cls.seen.append(("HEAD", self.path, dict(self.headers)))
                if self.path in store:
                    self.send_response(200)
                    self.send_header("x-amz-meta-sha256", store[self.path][1])
                else:
                    self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def do_PUT(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                cls.seen.append(("PUT", self.path, dict(self.headers)))
                store[self.path] = (body, self.headers.get("x-amz-meta-sha256", ""))
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()
        socketserver.TCPServer.allow_reuse_address = True
        cls.srv = socketserver.TCPServer(("127.0.0.1", 0), H)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def test_main_end_to_end(self):
        env = {**ENV, "R2_ENDPOINT": f"http://127.0.0.1:{self.srv.server_address[1]}"}
        with tempfile.TemporaryDirectory() as td:
            Path(td, "picks_backup.json").write_text("[1]")
            Path(td, "picks_backup_meta.json").write_text('{"count": 1}')
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(R.main(["--root", td, "picks_backup.json", "picks_backup_meta.json"], env=env), 0)
                self.assertEqual(R.main(["--root", td, "picks_backup.json", "picks_backup_meta.json"], env=env), 0)          # second run: all unchanged
            self.assertEqual(self.store["/cv-data/picks_backup.json"][0], b"[1]")
            self.assertEqual(self.store["/cv-data/picks_backup_meta.json"][0], b'{"count": 1}')
            self.assertEqual(sum(1 for s in self.seen if s[0] == "PUT"), 2)                                                  # only the first run uploaded
            self.assertEqual(out.getvalue().count("-- skipped"), 2)                                                          # the second run skipped both files
            self.assertEqual(out.getvalue().count("=unchanged"), 2)                                                          # ... and its summary says so
            put = next(s for s in self.seen if s[0] == "PUT")
            self.assertEqual(put[2]["Cache-Control"], "no-cache")
            self.assertTrue(put[2]["Authorization"].startswith("AWS4-HMAC-SHA256 Credential=AKIAEXAMPLE/"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
