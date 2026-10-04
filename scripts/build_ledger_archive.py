#!/usr/bin/env python3
"""Build docs/ledger_archive.json: the settled picks of RETIRED leagues (MLB, WNBA, WTA, ATP, World Cup, tennis) that the app keeps OUT of its working ledger.

The app (docs/app.html: _archivable / _slimPick / getP / saveP) hides those picks from the working ledger because ~60% of it was retired-league history that no
displayed figure uses. Nothing is deleted: Supabase and docs/picks_backup.json keep every pick; this file is the same data in a lazily loaded form for the
"RETIRED-LEAGUE ARCHIVE" folder in Overall > history.

Source: docs/picks_backup.json, filtered with the APP'S OWN predicate (evaluated in a headless page, so the rule can never drift from the app).
Also rewrites the `_ARCH_SUM` constant in docs/app.html + docs/index.html (settled count / wins / losses of the archived picks) that keeps the header accuracy unchanged.

  python3 scripts/build_ledger_archive.py           # write the archive + constant
  python3 scripts/build_ledger_archive.py --check   # exit 1 if either is out of date (used by scripts/test_ledger_archive.py)
"""
from __future__ import annotations
import functools, http.server, json, re, socketserver, sys, threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
BACKUP = DOCS / "picks_backup.json"
ARCHIVE = DOCS / "ledger_archive.json"
SUM_RE = re.compile(r"const _ARCH_SUM=\{n:\d+,w:\d+,l:\d+\};")


def compute() -> tuple[list[dict], dict]:
    from playwright.sync_api import sync_playwright
    picks = json.loads(BACKUP.read_text())
    class _Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *a, **k):  # keep the build output readable
            pass
    handler = functools.partial(_Quiet, directory=str(DOCS))
    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer(("127.0.0.1", 0), handler) as srv:
        port = srv.server_address[1]
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        with sync_playwright() as p:
            b = p.chromium.launch()
            pg = b.new_page()
            pg.goto(f"http://127.0.0.1:{port}/app.html?nosb=1&slim=0")
            pg.wait_for_function("typeof _archivable==='function'&&typeof _normSport==='function'")
            ids = pg.evaluate("(ps)=>ps.filter(p=>_archivable(p)).map(p=>p.id)", picks)
            b.close()
        srv.shutdown()
    keep = set(ids)
    arch = sorted((p for p in picks if p.get("id") in keep), key=lambda p: str(p["id"]))
    summ = {"n": len(arch), "w": sum(p["outcome"] == "win" for p in arch), "l": sum(p["outcome"] == "loss" for p in arch)}
    return arch, summ


def main() -> int:
    arch, summ = compute()
    const = "const _ARCH_SUM={n:%d,w:%d,l:%d};" % (summ["n"], summ["w"], summ["l"])
    if "--check" in sys.argv:
        ok = ARCHIVE.exists() and {p["id"] for p in json.loads(ARCHIVE.read_text())["picks"]} >= {p["id"] for p in arch}
        for f in ("app.html", "index.html"):
            ok = ok and const in (DOCS / f).read_text(encoding="utf-8")
        print("archive up to date" if ok else "archive OUT OF DATE", summ)
        return 0 if ok else 1
    body = {"note": "Settled picks of retired leagues, kept out of the app's working ledger (see scripts/build_ledger_archive.py). Supabase and picks_backup.json still hold them.",
            "summary": summ, "picks": arch}
    ARCHIVE.write_text(json.dumps(body, separators=(",", ":")))
    for f in ("app.html", "index.html"):
        path = DOCS / f
        t = path.read_text(encoding="utf-8")
        assert SUM_RE.search(t), f"{f}: _ARCH_SUM line not found"
        path.write_text(SUM_RE.sub(const, t), encoding="utf-8")
    print(f"wrote {ARCHIVE.name}: {summ} ({ARCHIVE.stat().st_size // 1024} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
