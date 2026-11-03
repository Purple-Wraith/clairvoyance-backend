#!/usr/bin/env python3
"""scripts/_ledger_light.py: the light-column diff that replaced "download every row's raw JSON on every refresh" (2026-11-03 Supabase egress cut).

    python3 scripts/test_ledger_light.py

The Python signature must equal what docs/app.html `_supabaseBetRow` uploads, otherwise every refresh would re-fetch rows that did not change (a cost, not a bug) or -- worse -- treat a real change as equal.  The parity
test therefore runs the app's own function in node over awkward picks and compares column by column.
"""
import json
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _ledger_light as L  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent

PICKS = [
    {"id": "a", "hA": "NY", "awA": "BKN", "sport": "NBA", "betType": "ML", "betOn": "NY ML", "winProb": 0.71, "ml": -150, "decOdds": 1.67, "wager": 100, "outcome": "win", "date": "2026-11-02",
     "lockedAt": 1793665000000, "settledAt": 1793679073947, "grade": "A", "note": "n"},
    {"id": "b"},                                                                                  # nothing set: every default
    {"id": "c", "ml": 150.0, "winProb": 0, "wager": 0, "lockedAt": 0, "settledAt": 0, "grade": "", "note": "", "outcome": ""},   # falsy-but-present values
    {"id": "d", "ml": "-110", "decOdds": None, "winProb": None, "wager": None, "outcome": "pending", "regradedNote": "fixed"},
    {"id": "e", "ml": 0, "decOdds": 2.5, "winProb": 0.5, "wager": 50.5, "betType": "OU", "betOn": "OVER 5.5"},
    {"id": "f", "ml": -110.5, "sport": "NHL", "hA": "", "awA": ""},
]


def js_rows(picks):
    src = (ROOT / "docs" / "app.html").read_text()
    start = src.index("function _supabaseBetRow(p){")
    end = src.index("\n}\n", start) + 3
    prog = src[start:end] + "\nconst picks=" + json.dumps(picks) + ";\nconsole.log(JSON.stringify(picks.map(_supabaseBetRow)));"
    out = subprocess.run(["node", "-e", prog], capture_output=True, text=True, check=True).stdout
    return json.loads(out)


class Parity(unittest.TestCase):
    def test_signature_matches_the_apps_uploaded_row(self):
        for p, row in zip(PICKS, js_rows(PICKS)):
            row["rn"] = p.get("regradedNote")
            self.assertEqual(L.row_signature(row), L.pick_signature(p), f"pick {p['id']}: {row}")

    def test_row_from_pick_matches_the_apps_uploaded_row(self):
        for p, row in zip(PICKS, js_rows(PICKS)):
            mine = L.row_from_pick(p)
            for k, v in row.items():
                if k == "raw":
                    continue
                self.assertEqual(mine.get(k), v, f"pick {p['id']} column {k}")


HARNESS = r"""
const SUPABASE_URL='https://x.test', SUPABASE_KEY='k';
const TABLE=%(table)s, LOCAL=%(local)s, log=[];
const getP=()=>LOCAL;
function rowOf(p){const r=_supabaseBetRow(p);r.rn=p.regradedNote||null;return r;}
async function fetch(url,opt){
  log.push(decodeURIComponent(url.replace(SUPABASE_URL,'')));
  const q=decodeURIComponent(url);
  let rows;
  if(q.includes('&id=in.')){
    const ids=[...q.split('&id=in.')[1].matchAll(/"((?:[^"\\]|\\.)*)"/g)].map(m=>m[1].replace(/\\(.)/g,'$1'));
    rows=TABLE.filter(t=>ids.includes(t.id)).map(t=>({id:t.id,raw:t.raw,outcome:t.outcome,settled_at:t.settled_at||null,locked_at:t.locked_at||null}));
  } else {
    rows=TABLE.map(t=>t.outcome==='_removed'?{id:t.id,outcome:'_removed',locked_at:t.locked_at||null,settled_at:null}:Object.assign(rowOf(t.raw),{id:t.id,outcome:t.outcome}));
  }
  return {ok:true,json:async()=>rows};
}
%(src)s
_fetchSupabaseBetRowsLight(10).then(out=>console.log(JSON.stringify({out:out.map(r=>({id:r.id,outcome:r.outcome,hasRaw:!!r.raw})),log})));
"""


def run_light_pull(table, local):
    src = (ROOT / "docs" / "app.html").read_text()
    a = src.index("function _supabaseBetRow(p){")
    a_end = src.index("\n}\n", a) + 3
    b = src.index("const _LIGHT_COLS=")
    b_end = src.index("async function _fetchSupabaseBetRows(sinceDays){")
    prog = HARNESS % {"table": json.dumps(table), "local": json.dumps(local), "src": src[a:a_end] + src[b:b_end]}
    out = subprocess.run(["node", "-e", prog], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def trow(p, outcome=None):
    return {"id": p["id"], "outcome": outcome or p.get("outcome") or "pending", "raw": p, "locked_at": p.get("lockedAt")}


class AppLightPull(unittest.TestCase):
    """docs/app.html `_fetchSupabaseBetRowsLight`: the app's own function, run in node against a fake Supabase"""

    def test_unchanged_rows_are_never_downloaded_in_full(self):
        res = run_light_pull([trow(p) for p in PICKS], PICKS)
        self.assertEqual(res["out"], [])
        self.assertEqual(len(res["log"]), 1)                      # one light listing, no raw request
        self.assertIn("select=" + L.LIGHT_COLS, res["log"][0])

    def test_only_new_or_changed_rows_are_downloaded(self):
        pend = dict(PICKS[3], outcome="pending")
        local = [pend if p["id"] == "d" else p for p in PICKS]
        table = [trow(p) for p in PICKS if p["id"] != "d"] + [trow(dict(pend, outcome="win", settledAt=9)), trow({"id": "brand new", "outcome": "pending"})]
        res = run_light_pull(table, local)
        self.assertEqual(sorted(r["id"] for r in res["out"]), ["brand new", "d"])
        self.assertTrue(all(r["hasRaw"] for r in res["out"]))
        self.assertEqual(len(res["log"]), 2)

    def test_soft_deleted_rows_come_back_light(self):
        res = run_light_pull([trow({"id": "a", "lockedAt": 5}, "_removed")], PICKS)
        self.assertEqual(res["out"], [{"id": "a", "outcome": "_removed", "hasRaw": False}])
        self.assertEqual(len(res["log"]), 1)

    def test_ids_with_quotes_and_commas_survive_the_in_filter(self):
        weird = {"id": 'x,"y")', "outcome": "pending"}
        res = run_light_pull([trow(weird)], PICKS)
        self.assertEqual([r["id"] for r in res["out"]], ['x,"y")'])


class Plan(unittest.TestCase):
    def setUp(self):
        self.held = {p["id"]: p for p in PICKS}

    def light(self, p, **over):
        r = {k: v for k, v in L.row_from_pick(p).items() if k not in ("raw", "league")}
        r["rn"] = p.get("regradedNote")
        r.update(over)
        return r

    def test_identical_rows_fetch_nothing(self):
        self.assertEqual(L.plan([self.light(p) for p in PICKS], self.held), ([], []))

    def test_a_settlement_a_new_row_and_a_regrade_are_fetched(self):
        pend = dict(PICKS[3], outcome="pending")
        held = dict(self.held, d=pend)
        rows = [self.light(pend, outcome="win", settled_at=5),                       # pending -> settled on the server
                self.light({"id": "new", "outcome": "pending"}),                      # not held
                self.light(PICKS[0], rn="re-graded"),                                 # same outcome, regradedNote differs
                self.light(PICKS[1])]                                                 # unchanged
        self.assertEqual(L.plan(rows, held), ([], ["d", "new", "a"]))

    def test_tombstones_are_reported_not_fetched(self):
        rows = [self.light({"id": "gone"}, outcome="_removed"), self.light({"id": "a"}, outcome="_removed")]
        self.assertEqual(L.plan(rows, self.held), (["gone", "a"], []))

    def test_changed_column_is_enough(self):
        for col, val in (("wager", 7), ("dec_odds", 9.9), ("ml", "+500"), ("bet_on", "x"), ("grade", "Z"), ("note", "changed"), ("date", "2020-01-01"), ("sport", "MLB")):
            self.assertEqual(L.plan([self.light(PICKS[0], **{col: val})], self.held)[1], ["a"], col)

    def test_in_filter_quotes_awkward_ids(self):
        self.assertEqual(L.in_filter(["a", 'b,"c")']), '("a","b,\\"c\\")")')

    def test_chunks(self):
        self.assertEqual([len(c) for c in L.chunks([str(i) for i in range(95)])], [40, 40, 15])


try:
    import clairvoyance_update as CU                      # needs bs4 (installed in CI)
except Exception:                                         # pragma: no cover -- local machine without bs4
    CU = None


class FakeResp:
    def __init__(self, rows, status=200):
        self.rows, self.status_code = rows, status

    def json(self):
        return self.rows


@unittest.skipIf(CU is None, "clairvoyance_update needs bs4")
class ScheduledRefreshLightFetch(unittest.TestCase):
    """clairvoyance_update._fetch_bets_light: the scheduled refresh's ledger read (it used to download the whole table, ~6x a day)"""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = (CU.ROOT, CU._session, CU.log)
        CU.ROOT = Path(self.tmp.name)
        (CU.ROOT / "docs").mkdir()
        CU.log = lambda *a, **k: None
        self.base = [{"id": f"p{i}", "date": "2026-10-01", "outcome": "win", "betOn": f"x{i}", "sport": "NBA"} for i in range(250)]
        (CU.ROOT / "docs" / "picks_backup.json").write_text(json.dumps(self.base))
        self.calls = []

    def tearDown(self):
        CU.ROOT, CU._session, CU.log = self.saved
        self.tmp.cleanup()

    def serve(self, light, full_rows=(), light_status=200):
        calls = self.calls

        class S:
            def get(_s, url, headers=None, params=None, timeout=None):
                calls.append(params)
                if params["select"] == L.LIGHT_COLS:
                    return FakeResp(light, light_status)
                return FakeResp([r for r in full_rows if r["id"] in params["id"]])
        CU._session = S()

    def light_row(self, p, **over):
        r = {k: v for k, v in L.row_from_pick(p).items() if k not in ("raw", "league")}
        r["rn"] = None
        r.update(over)
        return r

    def test_unchanged_ledger_downloads_nothing_in_full(self):
        self.serve([self.light_row(p) for p in self.base])
        rows = CU._fetch_bets_light()
        self.assertEqual(len(rows), 250)
        self.assertEqual([c["select"] for c in self.calls], [L.LIGHT_COLS])
        self.assertEqual({r["id"] for r in rows}, {p["id"] for p in self.base})
        self.assertEqual(rows[0]["raw"]["id"], rows[0]["id"])           # rows keep the shape the settle/history code reads (raw, outcome, date, ...)

    def test_changed_new_and_removed_rows(self):
        settled = dict(self.base[0], outcome="loss", settledAt=7)
        full = {"id": "p0", "outcome": "loss", "raw": settled, "date": "2026-10-01"}
        new = {"id": "n1", "outcome": "pending", "raw": {"id": "n1", "date": "2026-11-03"}, "date": "2026-11-03"}
        light = [self.light_row(p) for p in self.base[1:]] + [self.light_row(settled), self.light_row({"id": "n1", "date": "2026-11-03"}), self.light_row({"id": "p1"}, outcome="_removed")]
        self.serve(light, [full, new])
        rows = {r["id"]: r for r in CU._fetch_bets_light()}
        self.assertEqual(rows["p0"]["outcome"], "loss")
        self.assertIn("n1", rows)
        self.assertNotIn("p1", rows)                                    # soft-deleted
        self.assertEqual(len(rows), 250)                                # 250 - removed + new
        self.assertEqual(sum(1 for c in self.calls if c["select"] == "*"), 1)

    def test_no_usable_backup_or_a_failed_request_means_none(self):
        self.serve([], light_status=500)
        self.assertIsNone(CU._fetch_bets_light())
        (CU.ROOT / "docs" / "picks_backup.json").write_text("[]")
        self.serve([])
        self.assertIsNone(CU._fetch_bets_light())


if __name__ == "__main__":
    unittest.main(verbosity=1)
