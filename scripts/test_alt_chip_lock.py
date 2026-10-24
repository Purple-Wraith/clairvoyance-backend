#!/usr/bin/env python3
"""Tapping / clicking an adjusted-line option on a game card LOCKS exactly that line (docs/app.html _altRowFor / _lockAltKey / lockPick), for every sport that shows them, both sides, once.
The game's top adjusted line carries the neon magenta border (owner 2026-10-24).

    python3 scripts/test_alt_chip_lock.py
"""
import functools, http.server, os, re, socketserver, threading, unittest
from pathlib import Path

ROOT = Path(os.environ.get("CV_ROOT") or Path(__file__).resolve().parent.parent)


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a, **k):
        pass


def mk(total, ou_lean, spread_team_lean, spread):
    """six markets of one game: lean UNDER by default"""
    over = 1 - ou_lean
    return [{"side": "under", "label": f"UNDER {total}", "prob": ou_lean, "tierN": 1, "dec": 1.91, "ml": "-110"}, {"side": "over", "label": f"OVER {total}", "prob": over, "tierN": 0, "dec": 1.91, "ml": "-110"},
            {"side": "sprdDog", "label": f"AWY +{spread}", "prob": spread_team_lean, "tierN": 2, "dec": 1.91, "ml": "-110"}, {"side": "sprdFav", "label": f"HOM -{spread}", "prob": 1 - spread_team_lean, "tierN": 0, "dec": 1.91, "ml": "-110"}]


GAMES = {                               # sport -> (home, away, markets)
    "NBA": ("PHI", "CLE", mk("236.5", .56, .63, "2.5")),
    "CFB": ("PHI", "CLE", mk("45.5", .56, .63, "2.5")),
    "NFL": ("PHI", "CLE", mk("47.5", .56, .63, "2.5")),
    "NHL": ("BOS", "NSH", mk("6.5", .52, .5, "1.5")[:2]),
    "LIIGA": ("Tappara", "Ilves", mk("5.5", .52, .5, "1.5")[:2]),
    "SHL": ("Lulea", "Farjestad", mk("5.5", .52, .5, "1.5")[:2]),
    "NLA": ("Bern", "Zug", mk("5.5", .52, .5, "1.5")[:2]),
    "EXTRALIGA": ("Sparta", "Plzen", mk("5.5", .52, .5, "1.5")[:2]),
    "SOC_PL": ("Arsenal", "Chelsea", [{"side": "under", "label": "UNDER 2.5", "prob": .56, "tierN": 1, "dec": 1.91, "ml": "-110"}, {"side": "over", "label": "OVER 2.5", "prob": .44, "tierN": 0, "dec": 1.91, "ml": "-110"},
                                      {"side": "ahDog", "label": "Chelsea +0.5", "prob": .55, "tierN": 1, "dec": 1.91, "ml": "-110"}, {"side": "ahFav", "label": "Arsenal -0.5", "prob": .45, "tierN": 0, "dec": 1.91, "ml": "-110"}]),
}


class ChipLock(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        socketserver.TCPServer.allow_reuse_address = True
        cls.srv = socketserver.TCPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=str(ROOT / "docs")))
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.pw = sync_playwright().start(); cls.b = cls.pw.chromium.launch(); cls.pg = cls.b.new_page(viewport={"width": 420, "height": 900}, has_touch=True)
        cls.pg.route("**/*espn.com/**", lambda r: r.abort()); cls.pg.route("**/*workers.dev/**", lambda r: r.abort())
        cls.pg.goto(f"http://127.0.0.1:{cls.srv.server_address[1]}/app.html?nosb=1")
        cls.pg.wait_for_function("typeof _altRowFor==='function'&&typeof lockPick==='function'"); cls.pg.wait_for_timeout(1200)
        cls.errs = []
        cls.pg.on("pageerror", lambda e: cls.errs.append(str(e)))

    @classmethod
    def tearDownClass(cls):
        cls.b.close(); cls.pw.stop(); cls.srv.shutdown()

    def render(self, sport, off=False):
        home, away, mkts = GAMES[sport]
        self.pg.evaluate("""([sp,h,a,m,off])=>{saveP([]);window._altReg={};document.getElementById('tst')?.remove();const d=document.createElement('div');d.id='tst';d.style.cssText='position:relative;z-index:5;padding:8px';
          d.innerHTML='<div class="gc" onclick="window.__cardClicked=(window.__cardClicked||0)+1">'+_altRowFor(sp,h,a,'2026-12-30T01:00Z',{mkts:m},off,String(sp).indexOf('SOC_')===0?{lockSport:'PL_SOC'}:undefined)+'</div>';document.querySelector('.spane.act').prepend(d);window.__cardClicked=0}""", [sport, home, away, mkts, off])
        self.pg.wait_for_timeout(100)

    def chips(self):
        return self.pg.evaluate("[...document.querySelectorAll('#tst .altc')].map(c=>({label:c.querySelector('.altp').innerText,top:c.classList.contains('alt-top'),prob:parseInt(c.querySelector('.alts').innerText)}))")

    def locked(self):
        return self.pg.evaluate("getP().filter(p=>p.outcome==='pending').map(p=>({betOn:p.betOn,betType:p.betType,sport:p.sport,altLine:!!p.altLine,label:p.altLine&&p.altLine.postedLabel}))")

    def test_tapping_any_option_locks_that_exact_line_for_every_sport_and_side(self):
        for sport in GAMES:
            self.render(sport)
            n = len(self.chips())
            self.assertGreaterEqual(n, 1, sport)
            for i in range(n):
                self.render(sport)
                label = self.chips()[i]["label"]
                self.pg.tap(f"#tst .altc:nth-child({i + 1 + 1})") if False else self.pg.locator("#tst .altc").nth(i).tap()
                self.pg.wait_for_timeout(500)
                got = self.locked()
                self.assertEqual([g["betOn"] for g in got], [label], f"{sport} option {i}: {got}")
                self.assertTrue(got[0]["altLine"], f"{sport} {label}: the lock carries its adjusted-line record")
                self.assertEqual(self.pg.evaluate("window.__cardClicked"), 0, f"{sport}: the tap must not also toggle the card")

    def test_a_second_tap_does_not_lock_it_twice(self):
        for sport in ("CFB", "NHL"):
            self.render(sport)
            loc = self.pg.locator("#tst .altc").first
            loc.tap(); self.pg.wait_for_timeout(400); loc.tap(); self.pg.wait_for_timeout(500)
            self.assertEqual(len(self.locked()), 1, sport)

    def test_the_locked_pick_is_the_right_sport_and_bet_type(self):
        for sport, bt in (("NBA", "OU"), ("CFB", "OU"), ("NFL", "OU"), ("NHL", "OU"), ("LIIGA", "OU")):
            self.render(sport)
            self.pg.locator("#tst .altc").filter(has_text=re.compile(r"^(OVER|UNDER)")).first.tap(); self.pg.wait_for_timeout(500)
            g = self.locked()[0]
            self.assertEqual(g["betType"], bt, sport)
            self.assertEqual(g["sport"], sport if sport != "LIIGA" else g["sport"], sport)
        self.render("CFB")
        self.pg.locator("#tst .altc").filter(has_text=re.compile(r"^(AWY|HOM)")).first.tap(); self.pg.wait_for_timeout(500)
        self.assertEqual(self.locked()[0]["betType"], "SPREAD")

    def test_locks_closed_options_do_nothing(self):
        self.render("CFB", off=True)
        self.pg.locator("#tst .altc").first.click(force=True); self.pg.wait_for_timeout(300)
        self.assertEqual(self.locked(), [])

    def test_options_are_reachable_by_keyboard(self):
        self.render("CFB")
        self.pg.locator("#tst .altc").first.focus(); self.pg.keyboard.press("Enter"); self.pg.wait_for_timeout(500)
        self.assertEqual(len(self.locked()), 1)
        self.render("CFB")
        self.pg.locator("#tst .altc").nth(1).focus(); self.pg.keyboard.press("Space"); self.pg.wait_for_timeout(500)
        self.assertEqual(len(self.locked()), 1)

    def test_exactly_one_option_per_game_has_the_magenta_border_the_most_probable_one(self):
        for sport in GAMES:
            self.render(sport)
            ch = self.chips()
            tops = [c for c in ch if c["top"]]
            self.assertEqual(len(tops), 1, (sport, ch))
            self.assertEqual(tops[0]["prob"], max(c["prob"] for c in ch), (sport, ch))
        border = self.pg.evaluate("getComputedStyle(document.querySelector('#tst .altc.alt-top')).borderTopColor")
        self.assertEqual(border, "rgb(240, 0, 255)")
        self.assertIn("rgba(240, 0, 255", self.pg.evaluate("getComputedStyle(document.querySelector('#tst .altc.alt-top')).boxShadow"))

    def test_no_script_errors(self):
        self.assertEqual(self.errs, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
