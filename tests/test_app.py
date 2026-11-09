import json
import re
import unittest
from unittest import mock

from fastapi.testclient import TestClient
from helpers import TempEnv, make_pdf

from study import ai, db, worker
from study.app import app
from study.config import get_settings

ALICE = {"X-authentik-username": "alice"}
BOB = {"X-authentik-username": "bob"}


class AppTests(TempEnv):
    def setUp(self):
        super().setUp()
        self.client = TestClient(app, follow_redirects=False)
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        super().tearDown()

    def work(self):
        while worker.process_one(get_settings()):
            pass

    def deck(self, headers=ALICE, name="Biochemie"):
        res = self.client.post("/decks", data={"name": name}, headers=headers)
        self.assertEqual(res.status_code, 303)
        return int(re.search(r"/decks/(\d+)", res.headers["location"]).group(1))

    def upload(self, deck_id, pages, headers=ALICE):
        path = make_pdf(self.tmp / "folien.pdf", pages)
        with path.open("rb") as fh:
            res = self.client.post(f"/decks/{deck_id}/upload", files={"file": ("folien.pdf", fh, "application/pdf")},
                                   headers=headers)
        self.assertEqual(res.status_code, 303)
        return res

    def test_health_without_login(self):
        res = self.client.get("/api/health")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["status"], "ok")

    def test_login_header_required(self):
        self.assertEqual(self.client.get("/").status_code, 401)
        self.assertEqual(self.client.get("/", headers={"X-authentik-username": "a/b"}).status_code, 400)
        self.assertEqual(self.client.get("/", headers=ALICE).status_code, 200)

    def test_cross_site_post_blocked(self):
        res = self.client.post("/decks", data={"name": "x"}, headers={**ALICE, "Sec-Fetch-Site": "cross-site"})
        self.assertEqual(res.status_code, 403)

    def test_full_flow(self):
        deck_id = self.deck()
        res = self.upload(deck_id, ["Zellatmung\nfindet in den Mitochondrien statt",
                                    "Glykolyse\nzerlegt Glukose in Pyruvat",
                                    "Danke"])
        doc_id = int(re.search(r"/documents/(\d+)", res.headers["location"]).group(1))
        self.work()
        page = self.client.get(f"/documents/{doc_id}", headers=ALICE)
        self.assertIn("Kartenvorschläge erzeugen", page.text)

        res = self.client.post(f"/documents/{doc_id}/generate", data={"first": 1, "last": 3}, headers=ALICE)
        self.assertEqual(res.status_code, 303)
        self.work()
        page = self.client.get(f"/documents/{doc_id}", headers=ALICE)
        self.assertIn("Worum geht es bei «Zellatmung»?", page.text)
        self.assertIn("Seiten ohne Vorschläge: 3", page.text)

        conn = db.connect(get_settings().db_path)
        suggestions = conn.execute("SELECT id FROM suggestions ORDER BY id").fetchall()
        self.assertEqual(len(suggestions), 2)
        first, second = suggestions[0]["id"], suggestions[1]["id"]
        self.client.post(f"/suggestions/{first}/accept",
                         data={"type": "basic", "front": "Wo findet die Zellatmung statt?", "back": "In den Mitochondrien"},
                         headers=ALICE)
        self.client.post(f"/suggestions/{second}/reject", headers=ALICE)
        cards = conn.execute("SELECT * FROM cards").fetchall()
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]["front"], "Wo findet die Zellatmung statt?")
        self.assertIsNotNone(cards[0]["page_id"])

        learn = self.client.get(f"/learn?deck={deck_id}", headers=ALICE)
        self.assertIn("Wo findet die Zellatmung statt?", learn.text)
        self.assertIn("/files/pages/", learn.text)
        res = self.client.post(f"/learn/{cards[0]['id']}", data={"rating": 3, "duration_ms": 4000, "deck": str(deck_id)},
                               headers=ALICE)
        self.assertEqual(res.headers["location"], f"/learn?deck={deck_id}")
        card = conn.execute("SELECT * FROM cards").fetchone()
        self.assertEqual(card["reps"], 1)
        self.assertEqual(conn.execute("SELECT was_new FROM reviews").fetchone()["was_new"], 1)
        learn = self.client.get(f"/learn?deck={deck_id}", headers=ALICE)
        self.assertIn("Fertig für jetzt", learn.text)

        image = self.client.get(f"/files/pages/{card['page_id']}.jpg", headers=ALICE)
        self.assertEqual(image.status_code, 200)
        self.assertEqual(image.headers["content-type"], "image/jpeg")
        self.assertIn("no-cache", image.headers["cache-control"])
        original = self.client.get(f"/files/documents/{doc_id}.pdf", headers=ALICE)
        self.assertEqual(original.status_code, 200)
        self.assertTrue(original.content.startswith(b"%PDF-"))
        self.assertIn("inline", original.headers["content-disposition"])
        conn.close()

    def test_users_are_separated(self):
        deck_id = self.deck()
        self.upload(deck_id, ["Titel\nInhalt"])
        self.work()
        self.assertEqual(self.client.get(f"/decks/{deck_id}", headers=BOB).status_code, 404)
        self.assertEqual(self.client.get("/documents/1", headers=BOB).status_code, 404)
        self.assertEqual(self.client.get("/files/pages/1.jpg", headers=BOB).status_code, 404)
        self.assertEqual(self.client.get("/files/documents/1.pdf", headers=BOB).status_code, 404)
        self.assertEqual(self.client.post(f"/decks/{deck_id}/delete", headers=BOB).status_code, 404)
        self.assertNotIn(f'href="/decks/{deck_id}"', self.client.get("/", headers=BOB).text)

    def test_upload_rejects_non_pdf(self):
        deck_id = self.deck()
        res = self.client.post(f"/decks/{deck_id}/upload", files={"file": ("x.pdf", b"PK\x03\x04zip", "application/pdf")},
                               headers=ALICE)
        self.assertIn("msg=not-pdf", res.headers["location"])

    def test_manual_card_edit_delete(self):
        deck_id = self.deck()
        self.client.post(f"/decks/{deck_id}/cards", data={"type": "cloze", "front": "Die {{Leber}} entgiftet", "back": ""},
                         headers=ALICE)
        res = self.client.post(f"/decks/{deck_id}/cards", data={"type": "basic", "front": "Ohne Rückseite", "back": ""},
                               headers=ALICE)
        self.assertIn("msg=empty", res.headers["location"])
        conn = db.connect(get_settings().db_path)
        card = conn.execute("SELECT * FROM cards").fetchone()
        self.assertEqual(card["type"], "cloze")
        learn = self.client.get("/learn", headers=ALICE)
        self.assertIn("[…]", learn.text)
        self.client.post(f"/cards/{card['id']}", data={"type": "basic", "front": "Was entgiftet?", "back": "Die Leber"},
                         headers=ALICE)
        self.assertEqual(conn.execute("SELECT front FROM cards").fetchone()["front"], "Was entgiftet?")
        self.client.post(f"/cards/{card['id']}/delete", headers=ALICE)
        self.assertIsNone(conn.execute("SELECT * FROM cards").fetchone())
        conn.close()

    def test_new_cards_per_day_limit(self):
        deck_id = self.deck()
        for i in range(3):
            self.client.post(f"/decks/{deck_id}/cards", data={"front": f"F{i}", "back": "A"}, headers=ALICE)
        import os
        os.environ["STUDY_NEW_PER_DAY"] = "1"
        conn = db.connect(get_settings().db_path)
        card_id = conn.execute("SELECT id FROM cards ORDER BY id").fetchone()["id"]
        self.client.post(f"/learn/{card_id}", data={"rating": 4}, headers=ALICE)
        self.assertIn("Tageslimit", self.client.get("/learn", headers=ALICE).text)
        conn.close()

    def cards(self, deck_id, n=2):
        for i in range(n):
            self.client.post(f"/decks/{deck_id}/cards", data={"front": f"Frage {i}", "back": "A"}, headers=ALICE)
        conn = db.connect(get_settings().db_path)
        rows = conn.execute("SELECT * FROM cards ORDER BY id").fetchall()
        conn.close()
        return rows

    def test_undo_restores_state_step_by_step(self):
        deck_id = self.deck()
        first, second = self.cards(deck_id)
        self.assertNotIn("Rückgängig", self.client.get("/learn", headers=ALICE).text)
        self.client.post(f"/learn/{first['id']}", data={"rating": 3, "reps": 0}, headers=ALICE)
        self.client.post(f"/learn/{second['id']}", data={"rating": 1, "reps": 0, "deck": str(deck_id)}, headers=ALICE)
        page = self.client.get(f"/learn?deck={deck_id}", headers=ALICE)
        self.assertIn("Rückgängig", page.text)
        self.assertIn("Nochmal", page.text)

        res = self.client.post("/learn/undo", data={"deck": str(deck_id)}, headers=ALICE)
        self.assertEqual(res.headers["location"], f"/learn?deck={deck_id}&card={second['id']}&msg=undone")
        conn = db.connect(get_settings().db_path)
        restored = conn.execute("SELECT * FROM cards WHERE id = ?", (second["id"],)).fetchone()
        self.assertEqual((restored["fsrs"], restored["due"], restored["reps"]),
                         (second["fsrs"], second["due"], 0))
        self.assertIn("Frage 1", self.client.get(res.headers["location"], headers=ALICE).text)
        # zweimal: auch die erste Bewertung, dann ist nichts mehr da
        self.client.post("/learn/undo", headers=ALICE)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM reviews").fetchone()[0], 0)
        self.assertEqual(conn.execute("SELECT reps FROM cards WHERE id = ?", (first["id"],)).fetchone()[0], 0)
        res = self.client.post("/learn/undo", headers=ALICE)
        self.assertIn("msg=undo-none", res.headers["location"])
        conn.close()

    def test_old_reviews_cannot_be_undone(self):
        deck_id = self.deck()
        (card,) = self.cards(deck_id, 1)
        self.client.post(f"/learn/{card['id']}", data={"rating": 3}, headers=ALICE)
        conn = db.connect(get_settings().db_path)
        conn.execute("UPDATE reviews SET reviewed_at = '2026-01-01T00:00:00+00:00'")
        conn.commit()
        self.assertIn("msg=undo-none", self.client.post("/learn/undo", headers=ALICE).headers["location"])
        self.assertEqual(conn.execute("SELECT reps FROM cards").fetchone()[0], 1)
        conn.close()

    def test_double_submit_rates_once(self):
        deck_id = self.deck()
        (card,) = self.cards(deck_id, 1)
        for _ in range(2):
            res = self.client.post(f"/learn/{card['id']}", data={"rating": 1, "reps": 0}, headers=ALICE)
            self.assertEqual(res.status_code, 303)
        conn = db.connect(get_settings().db_path)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM reviews").fetchone()[0], 1)
        self.assertEqual(conn.execute("SELECT reps FROM cards").fetchone()[0], 1)
        conn.close()

    def test_suspend_from_learning_and_resume(self):
        deck_id = self.deck()
        first, second = self.cards(deck_id)
        res = self.client.post(f"/cards/{first['id']}/suspend", data={"on": 1, "learn": str(deck_id)}, headers=ALICE)
        self.assertEqual(res.headers["location"], f"/learn?deck={deck_id}&suspended={first['id']}")
        page = self.client.get(res.headers["location"], headers=ALICE)
        self.assertIn("Wieder aufnehmen", page.text)
        self.assertIn("Frage 1", page.text)
        self.assertIn(f'action="/learn/{second["id"]}"', page.text)
        deck = self.client.get(f"/decks/{deck_id}", headers=ALICE).text
        self.assertIn("ausgesetzt", deck)
        # ausgesetzte Karten zählen nicht als neu und lassen sich nicht bewerten
        self.client.post(f"/learn/{first['id']}", data={"rating": 3}, headers=ALICE)
        conn = db.connect(get_settings().db_path)
        self.assertEqual(conn.execute("SELECT reps FROM cards WHERE id = ?", (first["id"],)).fetchone()[0], 0)
        self.assertIn('<span class="new">1</span> neu', page.text)
        res = self.client.post(f"/cards/{first['id']}/suspend", data={"on": 0, "learn": str(deck_id)}, headers=ALICE)
        self.assertIn("msg=card-resumed", res.headers["location"])
        self.assertEqual(conn.execute("SELECT suspended FROM cards WHERE id = ?", (first["id"],)).fetchone()[0], 0)
        conn.close()

    def test_edit_from_learning_returns_to_card(self):
        deck_id = self.deck()
        (card,) = self.cards(deck_id, 1)
        page = self.client.get(f"/cards/{card['id']}/edit?learn=all", headers=ALICE)
        self.assertIn(f'href="/learn?card={card["id"]}"', page.text)
        res = self.client.post(f"/cards/{card['id']}", data={"front": "Neu", "back": "B", "learn": "all"}, headers=ALICE)
        self.assertEqual(res.headers["location"], f"/learn?card={card['id']}&msg=card-saved")
        res = self.client.post(f"/cards/{card['id']}", data={"front": "Neu", "back": "B"}, headers=ALICE)
        self.assertEqual(res.headers["location"], f"/decks/{deck_id}?msg=card-saved")
        # nur bekannte Werte; alles andere führt zur Kursseite
        page = self.client.get(f"/cards/{card['id']}/edit?learn=//evil.example", headers=ALICE)
        self.assertNotIn("evil", page.text)

    def test_suggestion_decisions_as_json(self):
        deck_id = self.deck()
        self.upload(deck_id, ["Eins\nText", "Zwei\nText", "Drei\nText"])
        self.work()
        self.client.post("/documents/1/generate", data={"first": 1, "last": 3}, headers=ALICE)
        self.work()
        page = self.client.get("/documents/1", headers=ALICE).text
        self.assertIn("data-open-count>3<", page)
        self.assertIn("keys-hint", page)
        as_json = {**ALICE, "Accept": "application/json"}
        res = self.client.post("/suggestions/1/accept", data={"type": "basic", "front": "F **fett**", "back": "B"},
                               headers=as_json)
        self.assertEqual(res.json(), {"ok": True, "status": "accepted", "text": "F **fett**", "open": 2})
        res = self.client.post("/suggestions/2/reject", headers=as_json)
        self.assertEqual(res.json()["status"], "rejected")
        self.assertEqual(res.json()["open"], 1)
        res = self.client.post("/suggestions/3/accept", data={"type": "basic", "front": " ", "back": ""}, headers=as_json)
        self.assertEqual(res.status_code, 400)
        self.assertFalse(res.json()["ok"])
        # schon entschieden: Stand zurück, nichts doppelt
        res = self.client.post("/suggestions/1/accept", data={"front": "X", "back": "Y"}, headers=as_json)
        self.assertEqual(res.json()["status"], "accepted")
        conn = db.connect(get_settings().db_path)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM cards").fetchone()[0], 1)
        conn.close()
        # ohne JSON wie bisher zurück zur Folie
        self.assertEqual(self.client.post("/suggestions/3/reject", headers=ALICE).headers["location"],
                         "/documents/1#p3")

    def generated(self, types=None):
        deck_id = self.deck()
        self.upload(deck_id, ["Eins\nErster Text", "Zwei\nZweiter Text"])
        self.work()
        data = {"first": 1, "last": 2}
        if types is not None:
            data["types"] = ["", *types]  # wie das Formular, mit leerem Merker
        res = self.client.post("/documents/1/generate", data=data, headers=ALICE)
        self.work()
        return deck_id, res

    def test_card_types_chosen_before_generating(self):
        deck_id, res = self.generated(["twoway"])
        self.assertIn("msg=generating", res.headers["location"])
        conn = db.connect(get_settings().db_path)
        self.assertEqual({r["type"] for r in conn.execute("SELECT type FROM suggestions")}, {"twoway"})
        job = conn.execute("SELECT params FROM jobs WHERE kind = 'generate'").fetchone()
        self.assertEqual(json.loads(job["params"])["types"], ["twoway"])
        page = self.client.get("/documents/1", headers=ALICE).text
        self.assertRegex(page, r'value="twoway" checked')
        self.assertNotRegex(page, r'value="basic" checked>\s*<span>')
        rejected = self.client.post("/suggestions/2/reject", headers={**ALICE, "Accept": "application/json"})
        self.assertEqual(rejected.json()["text"], "Zwei ↔ Zweiter Text")
        # Zwei-Wege übernehmen: zwei Karten, eine pro Richtung
        self.client.post("/suggestions/1/accept", data={"type": "twoway", "front": "Eins", "back": "Erster Text"},
                         headers=ALICE)
        cards = [(r["type"], r["front"], r["back"]) for r in conn.execute("SELECT * FROM cards ORDER BY id")]
        self.assertEqual(cards, [("basic", "Eins", "Erster Text"), ("basic", "Erster Text", "Eins")])
        conn.close()

    def test_no_type_chosen_starts_nothing(self):
        _deck_id, res = self.generated([])
        self.assertIn("msg=types-empty", res.headers["location"])
        conn = db.connect(get_settings().db_path)
        self.assertIsNone(conn.execute("SELECT 1 FROM jobs WHERE kind = 'generate'").fetchone())
        conn.close()

    def test_without_types_field_old_default_applies(self):
        self.generated()
        conn = db.connect(get_settings().db_path)
        job = conn.execute("SELECT params FROM jobs WHERE kind = 'generate'").fetchone()
        self.assertEqual(json.loads(job["params"])["types"], ["basic", "cloze"])
        conn.close()

    def test_resume_keeps_types(self):
        self.generated(["cloze"])
        conn = db.connect(get_settings().db_path)
        conn.execute("UPDATE documents SET status = 'failed', gen_last = 2, progress = 1")
        conn.commit()
        self.client.post("/documents/1/resume", headers=ALICE)
        params = json.loads(conn.execute("SELECT params FROM jobs ORDER BY id DESC").fetchone()["params"])
        self.assertEqual(params["types"], ["cloze"])
        conn.close()

    def test_reject_can_be_undone(self):
        self.generated()
        as_json = {**ALICE, "Accept": "application/json"}
        self.client.post("/suggestions/1/reject", headers=as_json)
        conn = db.connect(get_settings().db_path)
        self.assertEqual(len(worker.guidance(conn, 1).rejected), 1)
        page = self.client.get("/documents/1", headers=ALICE).text
        self.assertIn('action="/suggestions/1/reopen"', page)
        res = self.client.post("/suggestions/1/reopen", headers=as_json)
        self.assertEqual(res.json()["status"], "open")
        self.assertEqual(res.json()["open"], 2)
        row = conn.execute("SELECT status, decided FROM suggestions WHERE id = 1").fetchone()
        self.assertEqual((row["status"], row["decided"]), ("open", None))
        self.assertEqual(worker.guidance(conn, 1).rejected, [])
        # Übernommene bleiben übernommen; ohne JSON zurück zur Folie
        self.client.post("/suggestions/2/accept", data={"type": "basic", "front": "F", "back": "B"}, headers=ALICE)
        res = self.client.post("/suggestions/2/reopen", headers=ALICE)
        self.assertEqual(res.headers["location"], "/documents/1#p2")
        self.assertEqual(conn.execute("SELECT status FROM suggestions WHERE id = 2").fetchone()[0], "accepted")
        self.assertEqual(self.client.post("/suggestions/1/reopen", headers=BOB).status_code, 404)
        conn.close()

    def test_manual_twoway_card_and_edit_without_twoway(self):
        deck_id = self.deck()
        res = self.client.post(f"/decks/{deck_id}/cards", data={"type": "twoway", "front": "Hund", "back": "dog"},
                               headers=ALICE)
        self.assertIn("msg=cards-added", res.headers["location"])
        conn = db.connect(get_settings().db_path)
        rows = conn.execute("SELECT id, front, back FROM cards ORDER BY id").fetchall()
        self.assertEqual([(r["front"], r["back"]) for r in rows], [("Hund", "dog"), ("dog", "Hund")])
        self.assertNotIn('value="twoway"', self.client.get(f"/cards/{rows[0]['id']}/edit", headers=ALICE).text)
        self.client.post(f"/cards/{rows[0]['id']}", data={"type": "twoway", "front": "Hund", "back": "dog"},
                         headers=ALICE)
        self.assertEqual(conn.execute("SELECT type FROM cards WHERE id = ?", (rows[0]["id"],)).fetchone()[0], "basic")
        conn.close()

    def test_manifest_and_icons(self):
        res = self.client.get("/static/manifest.webmanifest")
        self.assertEqual(res.status_code, 200)
        manifest = res.json()
        self.assertEqual(manifest["display"], "standalone")
        for icon in manifest["icons"]:
            image = self.client.get(icon["src"])
            self.assertEqual(image.status_code, 200)
            self.assertTrue(image.content.startswith(b"\x89PNG"))
        self.assertEqual(self.client.get("/static/apple-touch-icon.png").status_code, 200)
        self.assertIn('rel="manifest"', self.client.get("/", headers=ALICE).text)

    def test_theme_per_account(self):
        page = self.client.get("/", headers=ALICE).text
        self.assertIn('<html lang="de">', page)
        self.assertIn("/static/theme.js", page)
        as_json = {**ALICE, "Accept": "application/json"}
        res = self.client.post("/settings/theme", data={"accent": "#AA3366", "background": "20"}, headers=as_json)
        self.assertEqual(res.json(), {"ok": True, "accent": "#aa3366", "background": 20})
        page = self.client.get("/learn", headers=ALICE).text
        self.assertIn('<html lang="de" data-accent="#aa3366" data-bg="20">', page)
        self.assertIn('<html lang="de">', self.client.get("/", headers=BOB).text)
        # nur gültige Werte
        for bad in ({"accent": "red"}, {"accent": "#12345g"}, {"background": "101"}, {"background": "-5"}):
            self.assertEqual(self.client.post("/settings/theme", data=bad, headers=ALICE).status_code, 400)
        # Hintergrund schwarz (0) ist ein Wert, kein «Standard»
        self.client.post("/settings/theme", data={"accent": "", "background": "0"}, headers=ALICE)
        self.assertIn('<html lang="de" data-bg="0">', self.client.get("/", headers=ALICE).text)
        res = self.client.post("/settings/theme", data={"accent": "", "background": ""}, headers=ALICE)
        self.assertEqual(res.status_code, 303)
        self.assertIn('<html lang="de">', self.client.get("/", headers=ALICE).text)
        self.assertEqual(self.client.get("/static/theme.js").status_code, 200)

    def test_delete_document_keeps_cards(self):
        deck_id = self.deck()
        self.upload(deck_id, ["Titel\nInhalt"])
        self.work()
        self.client.post("/documents/1/generate", data={"first": 1, "last": 1}, headers=ALICE)
        self.work()
        self.client.post("/documents/1/accept-all", headers=ALICE)
        self.client.post("/documents/1/delete", headers=ALICE)
        conn = db.connect(get_settings().db_path)
        card = conn.execute("SELECT * FROM cards").fetchone()
        self.assertIsNotNone(card)
        self.assertIsNone(card["page_id"])
        self.assertFalse(worker.doc_dir(get_settings(), 1, 1).exists())
        conn.close()


class GuidanceTests(TempEnv):
    def test_instructions_and_decisions_reach_the_next_request(self):
        with TestClient(app, follow_redirects=False) as client:
            client.post("/decks", data={"name": "Recht"}, headers=ALICE)
            res = client.post("/decks/1/instructions", data={"instructions": "  Artikel mit Gesetz nennen.  "},
                              headers=ALICE)
            self.assertEqual(res.headers["location"], "/decks/1?msg=instructions-saved#anweisungen")
            page = client.get("/decks/1", headers=ALICE).text
            self.assertIn("Artikel mit Gesetz nennen.", page)
            self.assertIn('class="chip" data-template="', page)
            self.assertEqual(
                client.post("/decks/1/instructions", data={"instructions": "x"}, headers=BOB).status_code, 404)

            path = make_pdf(self.tmp / "f.pdf", ["Privatrecht\nzwischen Privaten", "Titel\nOrganisatorisches",
                                                 "Staat\nhoheitlich"])
            with path.open("rb") as fh:
                client.post("/decks/1/upload", files={"file": ("f.pdf", fh, "application/pdf")}, headers=ALICE)
            settings = get_settings()
            worker.process_one(settings)
            client.post("/documents/1/generate", data={"first": 1, "last": 3}, headers=ALICE)
            worker.process_one(settings)

            conn = db.connect(settings.db_path)
            ids = [r["id"] for r in conn.execute("SELECT id FROM suggestions ORDER BY id")]
            client.post(f"/suggestions/{ids[0]}/accept",
                        data={"type": "basic", "front": "Was regelt das Privatrecht?",
                              "back": "Beziehungen unter Privaten"},
                        headers=ALICE)
            client.post(f"/suggestions/{ids[1]}/reject", headers=ALICE)
            s = conn.execute("SELECT * FROM suggestions WHERE id = ?", (ids[0],)).fetchone()
            self.assertEqual(s["orig_front"], "Worum geht es bei «Privatrecht»?")
            self.assertEqual(s["front"], "Was regelt das Privatrecht?")
            self.assertIsNotNone(s["decided"])

            g = worker.guidance(conn, 1)
            self.assertEqual(g.instructions, "Artikel mit Gesetz nennen.")
            self.assertEqual(g.edited[0][1].front, "Was regelt das Privatrecht?")
            self.assertEqual(g.rejected[0].front, "Worum geht es bei «Titel»?")
            self.assertEqual(g.kept, [])
            self.assertIn("1 geändert, 0 unverändert übernommen, 1 verworfen",
                          client.get("/decks/1", headers=ALICE).text)

            # Die nächste Anfrage bekommt Anweisungen und Entscheide mit.
            client.post("/documents/1/generate", data={"first": 3, "last": 3}, headers=ALICE)
            spy = SpyFake()
            worker.process_one(settings, provider=spy)
            self.assertEqual(spy.contexts[0].guidance.instructions, "Artikel mit Gesetz nennen.")
            self.assertEqual(len(spy.contexts[0].guidance.edited), 1)
            conn.close()


class SpyFake:
    name = "fake"
    model = "fake"

    def __init__(self):
        self.contexts = []
        self.pages = []

    def generate(self, pages, ctx):
        self.contexts.append(ctx)
        self.pages.append(pages)
        return ai.Result(model=self.model)


class ProxySecretTests(TempEnv):
    env = {"PROXY_SECRET": "geheim", "STUDY_DEV_USER": "dev"}

    def test_secret_required_and_dev_user_ignored(self):
        with TestClient(app, follow_redirects=False) as client:
            self.assertEqual(client.get("/", headers=ALICE).status_code, 403)
            self.assertEqual(client.get("/api/health").status_code, 200)
            self.assertEqual(client.get("/", headers={**ALICE, "X-Proxy-Secret": "geheim"}).status_code, 200)
            # Mit Secret gilt der Entwicklungsbenutzer nicht.
            self.assertEqual(client.get("/", headers={"X-Proxy-Secret": "geheim"}).status_code, 401)


class BudgetTests(TempEnv):
    env = {"STUDY_MONTHLY_BUDGET_USD": "0.01"}

    def test_budget_stops_generation(self):
        with TestClient(app, follow_redirects=False) as client:
            client.post("/decks", data={"name": "K"}, headers=ALICE)
            path = make_pdf(self.tmp / "f.pdf", ["A\nB", "C\nD"])
            with path.open("rb") as fh:
                client.post("/decks/1/upload", files={"file": ("f.pdf", fh, "application/pdf")}, headers=ALICE)
            settings = get_settings()
            worker.process_one(settings)
            client.post("/documents/1/generate", data={"first": 1, "last": 2}, headers=ALICE)
            paid = PaidFake()
            worker.process_one(settings, provider=paid)
            conn = db.connect(settings.db_path)
            doc = conn.execute("SELECT * FROM documents").fetchone()
            self.assertEqual(doc["status"], "failed")
            self.assertIn("Monatsbudget", doc["error"])
            self.assertEqual(paid.calls, 0)
            conn.close()


class RetryTests(TempEnv):
    env = {"STUDY_PAGES_PER_REQUEST": "1"}

    def test_retry_continues_after_last_good_chunk(self):
        with TestClient(app, follow_redirects=False) as client:
            client.post("/decks", data={"name": "K"}, headers=ALICE)
            path = make_pdf(self.tmp / "f.pdf", ["A\nB", "C\nD", "E\nF"])
            with path.open("rb") as fh:
                client.post("/decks/1/upload", files={"file": ("f.pdf", fh, "application/pdf")}, headers=ALICE)
            settings = get_settings()
            worker.process_one(settings)
            client.post("/documents/1/generate", data={"first": 1, "last": 3}, headers=ALICE)
            flaky = FlakyFake(fail_on_call=2)
            worker.process_one(settings, provider=flaky)
            conn = db.connect(settings.db_path)
            job = conn.execute("SELECT * FROM jobs WHERE kind = 'generate'").fetchone()
            self.assertEqual(job["status"], "queued")
            self.assertEqual(json.loads(job["params"])["first"], 2)
            conn.execute("UPDATE jobs SET run_after = '2000-01-01T00:00:00+00:00'")
            conn.commit()
            worker.process_one(settings, provider=flaky)
            doc = conn.execute("SELECT * FROM documents").fetchone()
            self.assertEqual(doc["status"], "done")
            self.assertEqual(flaky.pages, [1, 2, 2, 3])
            self.assertEqual(conn.execute("SELECT COUNT(*) AS n FROM suggestions").fetchone()["n"], 3)
            conn.close()


class ModelChoiceTests(TempEnv):
    def setUp(self):
        super().setUp()
        self.client = TestClient(app, follow_redirects=False)
        self.client.__enter__()
        self.client.post("/decks", data={"name": "K"}, headers=ALICE)
        path = make_pdf(self.tmp / "f.pdf", ["A\nB", "C\nD"])
        with path.open("rb") as fh:
            self.client.post("/decks/1/upload", files={"file": ("f.pdf", fh, "application/pdf")}, headers=ALICE)
        worker.process_one(get_settings())

    def tearDown(self):
        self.client.__exit__(None, None, None)
        super().tearDown()

    def last_params(self):
        conn = db.connect(get_settings().db_path)
        row = conn.execute("SELECT params FROM jobs WHERE kind = 'generate' ORDER BY id DESC LIMIT 1").fetchone()
        conn.close()
        return json.loads(row["params"])

    def test_page_offers_models_with_costs(self):
        page = self.client.get("/documents/1", headers=ALICE).text
        for _, label in ai.MODELS:
            self.assertIn(label, page)
        self.assertIn('<option value="claude-opus-5-5" selected>', page)
        self.assertIn('<option value="medium" selected>', page)

    def test_choice_reaches_provider_and_suggestions(self):
        self.client.post("/documents/1/generate",
                         data={"first": 1, "last": 2, "model": "claude-sonnet-5-5", "effort": "high"}, headers=ALICE)
        self.assertEqual(self.last_params()["model"], "claude-sonnet-5-5")
        calls = []

        def fake_provider(name, model, effort):
            calls.append((model, effort))
            return ai.FakeProvider(model=model)

        with mock.patch.object(worker.ai, "get_provider", fake_provider):
            worker.process_one(get_settings())
        self.assertEqual(calls, [("claude-sonnet-5-5", "high")])
        conn = db.connect(get_settings().db_path)
        self.assertEqual({r["model"] for r in conn.execute("SELECT model FROM suggestions")}, {"claude-sonnet-5-5"})
        conn.close()
        page = self.client.get("/documents/1", headers=ALICE).text
        self.assertIn("zuletzt Sonnet 5.5", page)
        self.assertIn('<span class="badge model">Sonnet 5.5</span>', page)

    def test_unknown_values_fall_back_to_defaults(self):
        self.client.post("/documents/1/generate",
                         data={"first": 1, "last": 2, "model": "gpt-irgendwas", "effort": "max"}, headers=ALICE)
        params = self.last_params()
        self.assertEqual((params["model"], params["effort"]), ("claude-opus-5-5", "medium"))

    def test_resume_keeps_model(self):
        self.client.post("/documents/1/generate",
                         data={"first": 1, "last": 2, "model": "claude-haiku-5-5", "effort": "low"}, headers=ALICE)
        conn = db.connect(get_settings().db_path)
        conn.execute("UPDATE jobs SET status = 'failed'")
        conn.execute("UPDATE documents SET status = 'failed', progress = 1, error = 'x'")
        conn.commit()
        conn.close()
        self.client.post("/documents/1/resume", headers=ALICE)
        params = self.last_params()
        self.assertEqual((params["first"], params["model"], params["effort"]), (2, "claude-haiku-5-5", "low"))


class RestartTests(TempEnv):
    env = {"STUDY_PAGES_PER_REQUEST": "1"}

    def test_restart_mid_job_continues_after_done_pages(self):
        with TestClient(app, follow_redirects=False) as client:
            client.post("/decks", data={"name": "K"}, headers=ALICE)
            path = make_pdf(self.tmp / "f.pdf", ["A\nB", "C\nD", "E\nF"])
            with path.open("rb") as fh:
                client.post("/decks/1/upload", files={"file": ("f.pdf", fh, "application/pdf")}, headers=ALICE)
            settings = get_settings()
            worker.process_one(settings)
            client.post("/documents/1/generate", data={"first": 1, "last": 3}, headers=ALICE)
            conn = db.connect(settings.db_path)
            # Zustand nach einem Abbruch: Seiten 1–2 fertig, Job lief noch.
            conn.execute("UPDATE documents SET progress = 2")
            conn.execute("UPDATE jobs SET status = 'running' WHERE kind = 'generate'")
            conn.commit()
            worker.reset_running(conn)
            spy = SpyFake()
            worker.process_one(settings, provider=spy)
            self.assertEqual([p.number for ctx_pages in spy.pages for p in ctx_pages], [3])
            self.assertEqual(conn.execute("SELECT status FROM documents").fetchone()["status"], "done")
            # Ein neuer Auftrag für dieselben Seiten läuft wieder ab der ersten.
            client.post("/documents/1/generate", data={"first": 1, "last": 2}, headers=ALICE)
            spy = SpyFake()
            worker.process_one(settings, provider=spy)
            self.assertEqual([p.number for ctx_pages in spy.pages for p in ctx_pages], [1, 2])
            conn.close()


class FlakyFake:
    name = "fake"
    model = "fake"

    def __init__(self, fail_on_call):
        self.fail_on_call = fail_on_call
        self.calls = 0
        self.pages = []

    def generate(self, pages, ctx):
        self.calls += 1
        self.pages += [p.number for p in pages]
        if self.calls == self.fail_on_call:
            raise ai.AIError("kurz weg", retry=True)
        return ai.FakeProvider().generate(pages, ctx)


class PaidFake:
    name = "anthropic"
    model = "claude-opus-5-5"

    def __init__(self):
        self.calls = 0

    def generate(self, *args):
        self.calls += 1
        raise AssertionError("darf nicht aufgerufen werden")


if __name__ == "__main__":
    unittest.main()
