#!/usr/bin/env python3
"""Scores on every game card, live or final (docs/app.html _cvPaintCardScores), owner request 2026-10-23.

The NBA / NHL ESPN-event cards already print a live score; European hockey, the NHL upcoming card and soccer showed only a start time while a game was on (and no score at all on soccer).
One painter now fills any card whose header lacks the score from the live layer.  Cards below are shaped like the real ones: .gc > .gch > two .gtm (away first) + .np status block.

    python3 scripts/test_card_scores.py
"""
import functools, http.server, json, socketserver, threading, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a, **k):
        pass


def card(away, home, np_text, cid):
    return (f'<div class="gc" id="{cid}"><div class="gch"><div><span class="gtm">{away}</span> at <span class="gtm">{home}</span></div>'
            f'<div style="text-align:right"><div class="np mono">{np_text}</div><button class="btn">COMPARE</button></div></div></div>')


def game(home, away, hs, as_, state, note="", k="shl", tag="SHL", **kw):
    g = {"k": k, "tag": tag, "id": f"{tag}-{home}", "home": home, "away": away, "homeScore": hs, "awayScore": as_, "state": state, "note": note}
    g.update(kw)
    return g


class Scores(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        socketserver.TCPServer.allow_reuse_address = True
        cls.srv = socketserver.TCPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=str(ROOT / "docs")))
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.pw = sync_playwright().start(); cls.b = cls.pw.chromium.launch(); cls.pg = cls.b.new_page()
        cls.pg.goto(f"http://127.0.0.1:{cls.srv.server_address[1]}/app.html?nosb=1")
        cls.pg.wait_for_function("typeof _cvPaintCardScores==='function'&&typeof _LV!=='undefined'"); cls.pg.wait_for_timeout(1500)

    @classmethod
    def tearDownClass(cls):
        cls.b.close(); cls.pw.stop(); cls.srv.shutdown()

    def paint(self, cards, games):
        return self.pg.evaluate("""([html,games])=>{
          let host=document.getElementById('__t');if(!host){host=document.createElement('div');host.id='__t';document.body.appendChild(host);}
          host.innerHTML=html;_LV.games=games;_LV.relay={ts:0,games:[],ok:false};_cvPaintCardScores();
          const out={};host.querySelectorAll('.gc').forEach(c=>{const b=c.querySelector('.cvscore');out[c.id]=b?{text:b.innerText.replace(/\\n/g,' | '),cls:b.className}:null});return out}""", ["".join(cards), games])

    def test_a_live_card_with_only_a_start_time_gets_the_score_and_status(self):
        r = self.paint([card("Frolunda", "Rogle", "07:00 PM MDT", "c1")], [game("Rogle", "Frolunda", 2, 3, "in", "P2 12:34")])
        self.assertIn("Frolunda 3 – 2 Rogle", r["c1"]["text"])
        self.assertIn("LIVE" if "LIVE" in r["c1"]["text"] else "P2", r["c1"]["text"])
        self.assertIn("lv", r["c1"]["cls"])
        self.assertTrue(r["c1"]["text"].startswith("●"))

    def test_a_final_card_without_a_score_gets_final_and_the_score(self):
        r = self.paint([card("Arsenal", "Bayern", "FINAL", "c2")], [game("Bayern", "Arsenal", 1, 2, "post", "FT", k="soc", tag="UCL")])
        self.assertIn("FINAL", r["c2"]["text"])
        self.assertIn("Arsenal 2 – 1 Bayern", r["c2"]["text"])
        self.assertIn("fin", r["c2"]["cls"])

    def test_a_card_that_already_shows_the_score_is_left_alone(self):
        r = self.paint([card("CHI", "MIL", "Q3 2:52 · CHI 92–97 MIL", "c3"), card("Ilves", "Jukurit", "FINAL: Ilves 3 – Jukurit 0", "c4")],
                       [game("MIL", "CHI", 97, 92, "in", "2:52 - 3rd", k="nba", tag="NBA"), game("Jukurit", "Ilves", 0, 3, "post", "Final", k="liiga", tag="LIIGA")])
        self.assertIsNone(r["c3"])
        self.assertIsNone(r["c4"])

    def test_a_game_that_has_not_started_gets_nothing(self):
        r = self.paint([card("Frolunda", "Rogle", "07:00 PM MDT", "c5")], [game("Rogle", "Frolunda", 0, 0, "pre", "7:00 PM")])
        self.assertIsNone(r["c5"])

    def test_both_teams_must_match_in_the_right_order(self):
        r = self.paint([card("Rogle", "Frolunda", "07:00 PM MDT", "c6")], [game("Rogle", "Frolunda", 2, 3, "in", "P2 12:34")])      # card says Rogle at Frolunda; the feed says Frolunda at Rogle
        self.assertIsNone(r["c6"])
        r = self.paint([card("Frolunda", "Brynas", "07:00 PM MDT", "c7")], [game("Rogle", "Frolunda", 2, 3, "in", "P2 12:34")])    # only one team matches
        self.assertIsNone(r["c7"])

    def test_ranked_college_labels_still_match(self):
        r = self.paint([card("#12 DUKE", "(7) UVA", "07:00 PM", "c8")], [game("UVA", "DUKE", 10, 14, "in", "Q2 5:00", k="cfb", tag="CFB")])
        self.assertIn("DUKE", r["c8"]["text"])
        self.assertIn("14", r["c8"]["text"])

    def test_the_score_updates_in_place_and_is_never_duplicated(self):
        cards = [card("Frolunda", "Rogle", "07:00 PM MDT", "c9")]
        self.paint(cards, [game("Rogle", "Frolunda", 0, 1, "in", "P1 10:00")])
        r = self.pg.evaluate("""()=>{_LV.games=[{k:'shl',tag:'SHL',id:'x',home:'Rogle',away:'Frolunda',homeScore:2,awayScore:1,state:'in',note:'P2 01:00'}];
          _cvPaintCardScores();_cvPaintCardScores();const c=document.getElementById('c9');return {n:c.querySelectorAll('.cvscore').length,t:c.querySelector('.cvscore').innerText.replace(/\\n/g,' | ')}}""")
        self.assertEqual(r["n"], 1)
        self.assertIn("Frolunda 1 – 2 Rogle", r["t"])

    def test_the_box_is_removed_when_the_game_leaves_the_feed(self):
        cards = [card("Frolunda", "Rogle", "07:00 PM MDT", "c10")]
        self.assertIsNotNone(self.paint(cards, [game("Rogle", "Frolunda", 0, 1, "in", "P1 10:00")])["c10"])
        self.assertEqual(self.pg.evaluate("()=>{_LV.games=[];_cvPaintCardScores();return document.querySelectorAll('#c10 .cvscore').length}"), 0)

    def test_the_status_block_and_compare_button_stay_in_the_right_hand_block(self):
        self.paint([card("Frolunda", "Rogle", "07:00 PM MDT", "c11")], [game("Rogle", "Frolunda", 2, 3, "in", "P2 12:34")])
        order = self.pg.evaluate("[...document.querySelector('#c11 .gch > div:last-child').children].map(e=>e.className.split(' ')[0]||e.tagName)")
        self.assertEqual(order[:3], ["np", "cvscore", "btn"])                       # status, then the score, then COMPARE: nothing pushed out of place

    def test_it_runs_after_every_live_refresh_and_when_cards_render_later(self):
        src = (ROOT / "docs" / "app.html").read_text()
        self.assertIn("try{_scOnLive();}catch(e){}try{_cvPaintCardScores();}catch(e){}", src)
        self.assertIn("new MutationObserver(()=>{clearTimeout(t);t=setTimeout(_cvPaintCardScores,350);})", src)
        # a card inserted AFTER the feed is in place is picked up by the observer (no manual call)
        self.pg.evaluate("""()=>{_LV.games=[{k:'shl',tag:'SHL',id:'y',home:'Rogle',away:'Frolunda',homeScore:2,awayScore:3,state:'in',note:'P3 05:00'}]}""")
        self.pg.evaluate("(html)=>document.getElementById('__t').insertAdjacentHTML('beforeend',html)", card("Frolunda", "Rogle", "07:00 PM MDT", "c12"))
        self.pg.wait_for_function("!!document.querySelector('#c12 .cvscore')", timeout=4000)


if __name__ == "__main__":
    unittest.main(verbosity=2)
