#!/usr/bin/env python3
"""Mirror files to a Cloudflare R2 bucket (S3-compatible API, AWS Signature V4, standard library only -- no boto3 / requests needed on the runner).

    python3 scripts/r2_publish.py [--root DIR] [--prefix P] [--dry-run] [--force] FILE [FILE ...]

Each FILE (path relative to --root, default the repo root) is uploaded as object  <prefix><basename>  -- e.g. docs/picks_backup.json -> picks_backup.json -- with
    Content-Type   by extension (json -> application/json; charset=utf-8)
    Cache-Control  no-cache            (browsers/CDN revalidate every time; the data changes every few minutes)
    x-amz-meta-sha256   the content hash: the upload is SKIPPED when the object already carries the same hash (one cheap HEAD instead of a PUT).
Cross-origin access (CORS) is a bucket-level setting in Cloudflare, not a per-object header -- see docs/R2_SETUP.md for the rule to paste.

Credentials come from the environment (GitHub secrets):  R2_ACCOUNT_ID  R2_ACCESS_KEY_ID  R2_SECRET_ACCESS_KEY  R2_BUCKET   (optional: R2_PREFIX, R2_ENDPOINT).
With any of the four missing this is a LOGGED NO-OP that exits 0, so nothing breaks before the owner has set R2 up.  A .json file that does not parse is refused (never publish a
half-written file).  Exit status: 0 = everything uploaded/unchanged/skipped-as-unconfigured, 1 = at least one file failed.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import hmac
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REQUIRED = ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET")
REGION = "auto"            # R2's only region name
SERVICE = "s3"
CACHE_CONTROL = "no-cache"
CONTENT_TYPES = {".json": "application/json; charset=utf-8", ".csv": "text/csv; charset=utf-8", ".txt": "text/plain; charset=utf-8",
                 ".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".svg": "image/svg+xml", ".png": "image/png"}


def log(msg: str) -> None:
    print(f"[r2] {msg}", flush=True)


# ── AWS Signature V4 ─────────────────────────────────────────────────────────────────────────────────────────────────
def _hmac(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def sign_v4(method: str, host: str, path: str, headers: dict[str, str], payload_hash: str, access_key: str, secret_key: str,
            amz_date: str, region: str = REGION, service: str = SERVICE, query: str = "") -> dict[str, str]:
    """Returns the full header dict to send (the given headers + host, x-amz-date, x-amz-content-sha256, Authorization).  `path` is the already-URI-encoded path; `query` the canonical query string."""
    h = {k.lower(): " ".join(str(v).split()) for k, v in headers.items()}
    h["host"] = host
    h["x-amz-date"] = amz_date
    h["x-amz-content-sha256"] = payload_hash
    names = sorted(h)
    canonical_headers = "".join(f"{n}:{h[n]}\n" for n in names)
    signed_headers = ";".join(names)
    canonical_request = "\n".join([method, path, query, canonical_headers, signed_headers, payload_hash])
    scope = f"{amz_date[:8]}/{region}/{service}/aws4_request"
    to_sign = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(canonical_request.encode("utf-8")).hexdigest()])
    k = _hmac(("AWS4" + secret_key).encode("utf-8"), amz_date[:8])
    for part in (region, service, "aws4_request"):
        k = _hmac(k, part)
    signature = hmac.new(k, to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    out = dict(h)
    out["Authorization"] = f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, SignedHeaders={signed_headers}, Signature={signature}"
    return out


# ── transport (the only place that touches the network; tests swap it) ──────────────────────────────────────────────
def urllib_transport(method: str, url: str, headers: dict[str, str], body: bytes | None) -> tuple[int, dict[str, str], bytes]:
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, {k.lower(): v for k, v in r.getheaders()}, r.read()
    except urllib.error.HTTPError as e:
        return e.code, {k.lower(): v for k, v in (e.headers or {}).items()}, e.read() or b""


class Config:
    def __init__(self, account: str, key_id: str, secret: str, bucket: str, prefix: str = "", endpoint: str = ""):
        self.account, self.key_id, self.secret, self.bucket, self.prefix = account, key_id, secret, bucket, prefix
        self.endpoint = (endpoint or f"https://{account}.r2.cloudflarestorage.com").rstrip("/")


def config_from_env(env=None) -> tuple[Config | None, list[str]]:
    """-> (Config, []) when all four secrets are set, else (None, [names of the missing ones])."""
    env = os.environ if env is None else env
    missing = [n for n in REQUIRED if not (env.get(n) or "").strip()]
    if missing:
        return None, missing
    g = lambda n: env[n].strip()  # noqa: E731
    return Config(g("R2_ACCOUNT_ID"), g("R2_ACCESS_KEY_ID"), g("R2_SECRET_ACCESS_KEY"), g("R2_BUCKET"), (env.get("R2_PREFIX") or "").strip(), (env.get("R2_ENDPOINT") or "").strip()), []


class R2Client:
    def __init__(self, cfg: Config, transport=urllib_transport, sleep=time.sleep, now=None, retries: int = 3):
        self.cfg, self.transport, self.sleep, self.retries = cfg, transport, sleep, retries
        self.now = now or (lambda: dt.datetime.now(dt.timezone.utc))

    def _url_parts(self, key: str) -> tuple[str, str]:
        path = "/" + urllib.parse.quote(self.cfg.bucket, safe="") + "/" + urllib.parse.quote(key, safe="/-_.~")
        return self.cfg.endpoint + path, path

    def _call(self, method: str, key: str, headers: dict[str, str] | None = None, body: bytes | None = None) -> tuple[int, dict[str, str], bytes]:
        url, path = self._url_parts(key)
        host = urllib.parse.urlsplit(self.cfg.endpoint).netloc
        payload_hash = hashlib.sha256(body or b"").hexdigest()
        last: tuple[int, dict[str, str], bytes] = (0, {}, b"")
        for attempt in range(self.retries):
            amz_date = self.now().strftime("%Y%m%dT%H%M%SZ")
            signed = sign_v4(method, host, path, headers or {}, payload_hash, self.cfg.key_id, self.cfg.secret, amz_date)
            send = {("Host" if k == "host" else k): v for k, v in signed.items()}
            try:
                last = self.transport(method, url, send, body)
            except Exception as exc:                               # network error: retry
                last = (0, {}, str(exc).encode())
            if last[0] and last[0] < 500 and last[0] != 429:
                return last
            if attempt + 1 < self.retries:
                self.sleep(2 * (attempt + 1))
        return last

    def head_hash(self, key: str) -> str | None:
        """sha256 recorded on the stored object, '' if it exists without one, None if it does not exist (or could not be read)."""
        status, hdrs, _ = self._call("HEAD", key)
        if status == 200:
            return hdrs.get("x-amz-meta-sha256", "")
        return None

    def put(self, key: str, body: bytes, content_type: str, sha256: str) -> tuple[int, str]:
        status, _, resp = self._call("PUT", key, {"Content-Type": content_type, "Cache-Control": CACHE_CONTROL, "x-amz-meta-sha256": sha256}, body)
        return status, resp[:300].decode("utf-8", "replace")


def content_type_for(path: str) -> str:
    return CONTENT_TYPES.get(Path(path).suffix.lower(), "application/octet-stream")


def publish(client: R2Client | None, files: list[str], root: Path, prefix: str = "", dry_run: bool = False, force: bool = False) -> dict[str, str]:
    """-> {file: 'uploaded' | 'unchanged' | 'dry-run' | 'missing' | 'invalid-json' | 'failed'}.  client=None means R2 is not configured: every file is 'not-configured'."""
    result: dict[str, str] = {}
    for f in files:
        p = (root / f)
        key = prefix + Path(f).name
        if client is None:
            result[f] = "not-configured"
            continue
        if not p.is_file():
            log(f"{f}: not found under {root} -- skipped")
            result[f] = "missing"
            continue
        body = p.read_bytes()
        if p.suffix.lower() == ".json":
            try:
                json.loads(body)
            except Exception as exc:
                log(f"ERROR {f}: not valid JSON ({exc}) -- NOT uploaded")
                result[f] = "invalid-json"
                continue
        digest = hashlib.sha256(body).hexdigest()
        if not force and client.head_hash(key) == digest:
            log(f"{key}: unchanged ({len(body):,} B, sha256 {digest[:12]}) -- skipped")
            result[f] = "unchanged"
            continue
        if dry_run:
            log(f"{key}: would upload {len(body):,} B ({content_type_for(f)}) [dry-run]")
            result[f] = "dry-run"
            continue
        status, text = client.put(key, body, content_type_for(f), digest)
        if 200 <= status < 300:
            log(f"{key}: uploaded {len(body):,} B (sha256 {digest[:12]})")
            result[f] = "uploaded"
        else:
            log(f"ERROR {key}: upload failed, HTTP {status} {text}")
            result[f] = "failed"
    return result


def main(argv: list[str] | None = None, env=None, transport=urllib_transport, sleep=time.sleep) -> int:
    ap = argparse.ArgumentParser(description="Mirror files to Cloudflare R2 (no-op without R2_* credentials).")
    ap.add_argument("files", nargs="+", help="files to upload (relative to --root); the object key is the base name")
    ap.add_argument("--root", default=str(ROOT), help="directory the FILEs are relative to (default: the repo root)")
    ap.add_argument("--prefix", default=None, help="key prefix (default: $R2_PREFIX or none)")
    ap.add_argument("--dry-run", action="store_true", help="compare hashes and report, but upload nothing")
    ap.add_argument("--force", action="store_true", help="upload even when the stored hash matches")
    args = ap.parse_args(argv)
    cfg, missing = config_from_env(env)
    if cfg is None:
        log(f"R2 not configured (missing: {', '.join(missing)}) -- skipping upload of {len(args.files)} file(s); nothing is wrong, the repo copy is still the source of truth")
        return 0
    prefix = cfg.prefix if args.prefix is None else args.prefix
    client = R2Client(cfg, transport=transport, sleep=sleep)
    result = publish(client, args.files, Path(args.root), prefix, args.dry_run, args.force)
    bad = [f for f, r in result.items() if r in ("failed", "invalid-json")]
    log("summary: " + ", ".join(f"{Path(f).name}={r}" for f, r in result.items()))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
