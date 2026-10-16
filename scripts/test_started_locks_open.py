#!/usr/bin/env python3
"""Lock rows + ADJ. LINE rows stay open (and keep their pre-game prices) once a game has started or is in progress, in every league; only FINAL games dim (owner, 2026-10-16).

Renders each league's real card builder for the same upcoming game as pre / in / post and compares, so it needs no fixed data.
"""
import functools, http.server, threading, unittest
from pathlib import Path
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent

JS = """() => {
  const past = new Date(Date.now() - 3600e3).toISOString(), out = {};
  const flat = D => { const o = []; Object.keys((D && D.weeks) || {}).forEach(w => (D.weeks[w] || []).forEach(g => o.push(g))); return o; };
  const cnt = (h, re) => (h.match(re) || []).length;
  const run = (name, games, fn) => {
    const r = {n: 0, preAlt: 0, inAlt: 0, inOff: 0, inInert: 0, postOff: 0, inBrow: 0, preBrow: 0};
    for (const g of games.filter(x => x.state === 'pre').slice(0, 10)) {
      const mk = st => Object.assign({}, g, {state: st, date: past, homeScore: st === 'pre' ? null : 3, awayScore: st === 'pre' ? null : 2});
      const hp = fn(mk('pre')), hi = fn(mk('in')), ho = fn(mk('post'));
      r.n++; r.preAlt += cnt(hp, /class="altrow/g); r.inAlt += cnt(hi, /class="altrow/g);
      r.inOff += cnt(hi, /lockoff/g); r.inInert += cnt(hi, /inert/g); r.postOff += cnt(ho, /lockoff/g);
      r.inBrow += cnt(hi, /class="brow/g); r.preBrow += cnt(hp, /class="brow/g);
    }
    out[name] = r;
  };
  run('nhl', _NHL_SCHEDULE_DATA.games, g => _nhlUpcomingCard(g, 0));
  run('shl', _SHL_DATA.games || [], _shlMatchCard); run('liiga', _LIIGA_DATA.games || [], _liigaMatchCard);
  run('nla', _NLA_DATA.games || [], _nlaMatchCard); run('extraliga', _EXTRALIGA_DATA.games || [], _extraligaMatchCard);
  run('nfl', flat(_NFL_DATA), _nflGameCard2); run('cfb', flat(_CFB_DATA), _cfbGameCard);
  return out;
}"""


class Started(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        H = type("Q", (http.server.SimpleHTTPRequestHandler,), {"log_message": lambda *a: None})
        cls.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(H, directory=str(ROOT / "docs")))
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.pw = sync_playwright().start(); cls.b = cls.pw.chromium.launch(); pg = cls.b.new_page()
        pg.route("**/*", lambda r: r.continue_() if r.request.url.startswith("http://127.0.0.1") else r.abort())
        pg.goto(f"http://127.0.0.1:{cls.srv.server_address[1]}/app.html?nosb=1", wait_until="domcontentloaded")
        pg.wait_for_function("typeof _nhlUpcomingCard==='function'&&typeof _shlMatchCard==='function'&&typeof _NFL_DATA!=='undefined'&&!!_NFL_DATA&&typeof _CFB_DATA!=='undefined'&&!!_CFB_DATA&&!!_NHL_SCHEDULE_DATA", timeout=90000)
        pg.wait_for_timeout(2500)
        cls.R = pg.evaluate(JS)

    @classmethod
    def tearDownClass(cls):
        cls.b.close(); cls.pw.stop(); cls.srv.shutdown()

    def test_in_progress_keeps_every_lock_and_adj_row_open(self):
        for name, r in self.R.items():
            if not r["n"]:
                continue
            self.assertEqual(r["inOff"], 0, (name, r))                       # nothing dimmed while the game is on
            self.assertEqual(r["inInert"], 0, (name, r))                     # nothing disabled
            self.assertGreaterEqual(r["inBrow"], r["preBrow"], (name, r))    # lock buttons still rendered
            self.assertGreaterEqual(r["inAlt"], r["preAlt"], (name, r))      # ADJ. LINE rows not lost when the game starts (NHL used to drop them: pre-game prices were gated on state=='pre')

    def test_final_games_stay_dimmed(self):
        for name, r in self.R.items():
            if r["n"]:
                self.assertGreater(r["postOff"], 0, (name, r))

    def test_something_was_actually_checked(self):
        self.assertGreaterEqual(sum(1 for r in self.R.values() if r["n"]), 4, self.R)


if __name__ == "__main__":
    unittest.main(verbosity=2)
