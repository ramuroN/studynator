import csv
import io
import json
import re
import unittest
import zipfile

from fastapi.testclient import TestClient
from helpers import TempEnv, make_pdf

from study import db, exchange, srs, worker
from study.app import app
from study.config import get_settings

ALICE = {"X-authentik-username": "alice"}
BOB = {"X-authentik-username": "bob"}


class ParserTests(unittest.TestCase):
    def test_quizlet_tab_without_header(self):
        parsed = exchange.parse_table("Hund\tdog\nKatze\tcat\n\n")
        self.assertEqual(parsed.delimiter, "tab")
        self.assertFalse(parsed.header)
        self.assertEqual([(c.front, c.back) for c in parsed.cards], [("Hund", "dog"), ("Katze", "cat")])

    def test_semicolon_with_header_and_columns(self):
        text = "Nr;Frage;Antwort\n1;Was ist BV?;Bundesverfassung\n2;\"Mit ; drin\";ja\n"
        parsed = exchange.parse_table(text)
        self.assertEqual(parsed.delimiter, "semicolon")
        self.assertTrue(parsed.header)
        self.assertEqual((parsed.front, parsed.back), (1, 2))
        self.assertEqual(parsed.cards[1].front, "Mit ; drin")

    def test_anki_cloze_and_html(self):
        text = "Die {{c1::Leber}} und die {{c2::Niere::Organ}} filtern<br><b>Blut</b>\tExtra &amp; mehr\n"
        parsed = exchange.parse_table(text)
        self.assertEqual([c.type for c in parsed.cards], ["cloze", "cloze"])
        self.assertEqual(parsed.cards[0].front, "Die {{Leber}} und die Niere filtern\n**Blut**")
        self.assertEqual(parsed.cards[1].front, "Die Leber und die {{Niere}} filtern\n**Blut**")
        self.assertEqual(parsed.cards[0].back, "Extra & mehr")

    def test_rows_without_back_are_counted(self):
        parsed = exchange.parse_table("Nur vorne\nVorne\tHinten\nDie {{Lücke}} genügt\n")
        self.assertEqual(parsed.no_back, 1)
        self.assertEqual([c.type for c in parsed.cards], ["basic", "cloze"])

    def test_manual_settings_override(self):
        parsed = exchange.parse_table("a,b,c\nx,y,z\n", delimiter="comma", header="no", front=2, back=0)
        self.assertEqual([(c.front, c.back) for c in parsed.cards], [("c", "a"), ("z", "x")])

    def test_duplicates(self):
        cards = exchange.parse_table("A\t1\na\t2\nB\t3\n").cards
        fresh, dupes = exchange.split_duplicates(cards, ["b"])
        self.assertEqual([c.front for c in fresh], ["A"])
        self.assertEqual(dupes, 2)


class RemNoteTests(unittest.TestCase):
    TEXT = (
        "# Biologie\n"
        "- Zellen\n"
        "    - Was ist ein Mitochondrium? >> Kraftwerk der Zelle\n"
        "    - Glukose << C6H12O6\n"
        "    - Hund <> dog\n"
        "    - Ausgeschaltet >- egal\n"
        "    - Phasen der Mitose >>>\n"
        "        - Prophase\n"
        "        - Metaphase\n"
        "    - Die {{Leber}}{({Organ})} und die {{Niere}} filtern\n"
        "    - Begriff :: Erklärung mit http://x.ch\n"
        "    - Ohne Antwort >>\n"
    )

    def test_detect_and_parse(self):
        self.assertTrue(exchange.looks_like_remnote(self.TEXT))
        parsed = exchange.parse_table(self.TEXT)
        self.assertEqual(parsed.delimiter, "remnote")
        cards = [(c.type, c.front, c.back) for c in parsed.cards]
        self.assertEqual(cards, [
            ("basic", "Was ist ein Mitochondrium?", "Kraftwerk der Zelle"),
            ("basic", "C6H12O6", "Glukose"),
            ("basic", "Hund", "dog"),
            ("basic", "dog", "Hund"),
            ("basic", "Phasen der Mitose", "Prophase\nMetaphase"),
            ("cloze", "Die {{Leber}} und die Niere filtern", ""),
            ("cloze", "Die Leber und die {{Niere}} filtern", ""),
            ("basic", "Begriff", "Erklärung mit http://x.ch"),
        ])
        self.assertEqual(parsed.no_back, 1)

    def test_not_confused_with_tables_or_css(self):
        self.assertFalse(exchange.looks_like_remnote("Hund\tdog\nKatze\tcat\n"))
        self.assertFalse(exchange.looks_like_remnote("Frage;Antwort\nWas?;Das\n"))
        self.assertFalse(exchange.looks_like_remnote("a::before setzt Inhalt\nDie {{c1::Leber}} filtert\n"))

    def test_export_reimports(self):
        rows = [
            {"type": "basic", "front": "Was ist FSRS?", "back": "Ein Planer"},
            {"type": "basic", "front": "Phasen der Mitose", "back": "Prophase\nMetaphase"},
            {"type": "cloze", "front": "Die {{Leber}} entgiftet", "back": "Organ im Bauch"},
            {"type": "basic", "front": "Zwei\nZeilen vorne", "back": "hinten"},
        ]
        text = exchange.export_remnote("Bio", rows).decode("utf-8")
        self.assertIn("- Was ist FSRS? >> Ein Planer", text)
        self.assertIn("- Phasen der Mitose >>>\n    - Prophase\n    - Metaphase", text)
        self.assertIn("- Die {{Leber}} entgiftet\n    - Organ im Bauch", text)
        parsed = exchange.parse_table(text)
        self.assertEqual([(c.type, c.front, c.back) for c in parsed.cards], [
            ("basic", "Was ist FSRS?", "Ein Planer"),
            ("basic", "Phasen der Mitose", "Prophase\nMetaphase"),
            ("cloze", "Die {{Leber}} entgiftet", "Organ im Bauch"),
            ("basic", "Zwei Zeilen vorne", "hinten"),
        ])

    def test_tsv_for_goodnotes_is_plain(self):
        rows = [{"type": "basic", "front": "Was macht **gap**?", "back": "Abstand in `display: grid`\nund Flexbox"}]
        text = exchange.export_tsv(rows).decode("utf-8")
        self.assertEqual(text, "Was macht gap?\tAbstand in display: grid · und Flexbox\n")


class ExportRoundTripTests(TempEnv):
    def setUp(self):
        super().setUp()
        self.client = TestClient(app, follow_redirects=False)
        self.client.__enter__()
        self.client.post("/decks", data={"name": "Recht & Staat"}, headers=ALICE)
        self.client.post("/decks/1/instructions", data={"instructions": "Artikel nennen."}, headers=ALICE)
        path = make_pdf(self.tmp / "f.pdf", ["Privatrecht\nzwischen Privaten", "Staat\nhoheitlich"])
        with path.open("rb") as fh:
            self.client.post("/decks/1/upload", files={"file": ("folien.pdf", fh, "application/pdf")}, headers=ALICE)
        settings = get_settings()
        worker.process_one(settings)
        self.client.post("/documents/1/generate", data={"first": 1, "last": 2}, headers=ALICE)
        worker.process_one(settings)
        self.client.post("/documents/1/accept-all", headers=ALICE)
        self.client.post("/decks/1/cards", data={"type": "cloze", "front": "=SUMME {{Formel}}", "back": ""},
                         headers=ALICE)
        conn = db.connect(settings.db_path)
        first = conn.execute("SELECT id FROM cards ORDER BY id").fetchone()["id"]
        conn.close()
        self.client.post(f"/learn/{first}", data={"rating": 3, "duration_ms": 1200}, headers=ALICE)

    def tearDown(self):
        self.client.__exit__(None, None, None)
        super().tearDown()

    def test_csv_export_reimports(self):
        res = self.client.get("/decks/1/export.csv", headers=ALICE)
        self.assertEqual(res.status_code, 200)
        self.assertIn("attachment", res.headers["content-disposition"])
        text = res.content.decode("utf-8-sig")
        rows = list(csv.reader(io.StringIO(text), delimiter=";"))
        self.assertEqual(rows[0], exchange.CSV_HEADER)
        self.assertIn("'=SUMME {{Formel}}", [r[1] for r in rows])
        parsed = exchange.parse_table(text)
        self.assertIn("=SUMME {{Formel}}", [c.front for c in parsed.cards])
        self.assertEqual(len(parsed.cards), 3)

    def test_tsv_for_quizlet(self):
        text = self.client.get("/decks/1/export.tsv", headers=ALICE).content.decode("utf-8")
        lines = text.strip().split("\n")
        self.assertEqual(len(lines), 3)
        self.assertTrue(all(line.count("\t") == 1 for line in lines))
        self.assertIn("=SUMME […]\tFormel", lines)

    def test_backup_round_trip(self):
        last = self.client.post("/cards/3/suspend", data={"on": 1}, headers=ALICE)
        self.assertEqual(last.status_code, 303)
        self.client.post("/decks/1/plan", data={"exam_date": "2027-01-20", "new_per_day": "7"}, headers=ALICE)
        raw = self.client.get("/decks/1/export.zip", headers=ALICE).content
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            data = json.loads(zf.read("deck.json"))
            self.assertEqual(len([n for n in zf.namelist() if n.startswith("pages/")]), 2)
        self.assertEqual(data["deck"]["instructions"], "Artikel nennen.")
        self.assertEqual(sum(len(c["reviews"]) for c in data["cards"]), 1)

        res = self.client.post("/import", files={"file": ("backup.zip", raw, "application/zip")}, headers=ALICE)
        self.assertEqual(res.status_code, 303)
        new_deck = int(re.search(r"/decks/(\d+)", res.headers["location"]).group(1))
        self.assertIn("n=3", res.headers["location"])
        conn = db.connect(get_settings().db_path)
        deck = conn.execute("SELECT * FROM decks WHERE id = ?", (new_deck,)).fetchone()
        self.assertEqual(deck["name"], "Recht & Staat (Import)")
        self.assertEqual(deck["instructions"], "Artikel nennen.")
        self.assertEqual((deck["exam_date"], deck["new_per_day"]), ("2027-01-20", 7))
        old = conn.execute("SELECT front, due, reps, fsrs, suspended FROM cards WHERE deck_id = 1 ORDER BY id").fetchall()
        new = conn.execute("SELECT front, due, reps, fsrs, suspended, page_id FROM cards WHERE deck_id = ? ORDER BY id",
                           (new_deck,)).fetchall()
        self.assertEqual([(r["front"], r["due"], r["reps"]) for r in old], [(r["front"], r["due"], r["reps"]) for r in new])
        self.assertEqual([r["suspended"] for r in new], [0, 0, 1])
        self.assertEqual(srs.from_json(old[0]["fsrs"]).stability, srs.from_json(new[0]["fsrs"]).stability)
        self.assertEqual(conn.execute("SELECT COUNT(*) AS n FROM reviews r JOIN cards c ON c.id = r.card_id "
                                      "WHERE c.deck_id = ?", (new_deck,)).fetchone()["n"], 1)
        page_id = new[0]["page_id"]
        self.assertIsNotNone(page_id)
        self.assertEqual(self.client.get(f"/files/pages/{page_id}.jpg", headers=ALICE).status_code, 200)
        doc_id = conn.execute("SELECT document_id FROM pages WHERE id = ?", (page_id,)).fetchone()["document_id"]
        self.assertIn("aus einer Sicherung", self.client.get(f"/documents/{doc_id}", headers=ALICE).text)
        conn.close()

    def test_remnote_and_goodnotes_downloads(self):
        res = self.client.get("/decks/1/export.md", headers=ALICE)
        self.assertEqual(res.status_code, 200)
        self.assertIn("RemNote", res.headers["content-disposition"])
        self.assertTrue(res.content.decode("utf-8").startswith("# Recht & Staat\n"))
        res = self.client.get("/decks/1/export.tsv", headers=ALICE)
        self.assertIn("GoodNotes", res.headers["content-disposition"])
        self.assertTrue(res.headers["content-type"].startswith("text/tab-separated-values"))

    def test_others_cannot_export(self):
        for fmt in ("csv", "tsv", "md", "zip"):
            self.assertEqual(self.client.get(f"/decks/1/export.{fmt}", headers=BOB).status_code, 404)
        self.assertEqual(self.client.get("/decks/1/export.pdf", headers=ALICE).status_code, 404)


class ImportFlowTests(TempEnv):
    def setUp(self):
        super().setUp()
        self.client = TestClient(app, follow_redirects=False)
        self.client.__enter__()
        self.client.post("/decks", data={"name": "Vokabeln"}, headers=ALICE)
        self.client.post("/decks/1/cards", data={"front": "Hund", "back": "dog"}, headers=ALICE)

    def tearDown(self):
        self.client.__exit__(None, None, None)
        super().tearDown()

    def test_paste_preview_and_commit(self):
        res = self.client.post("/decks/1/import", data={"text": "Hund\tdog\nKatze\tcat\nMaus\tmouse\n"}, headers=ALICE)
        url = res.headers["location"]
        token = url.rsplit("/", 1)[1]
        preview = self.client.get(url, headers=ALICE)
        self.assertIn("2 Karten importieren", preview.text)
        self.assertIn("Katze", preview.text)
        # Ein anderes Konto sieht die Vorschau nicht.
        self.assertEqual(self.client.get(f"/decks/1/import/{token}", headers=BOB).status_code, 404)
        res = self.client.post(url, data={"delimiter": "tab", "header": "no", "front": 0, "back": 1}, headers=ALICE)
        self.assertIn("n=2", res.headers["location"])
        self.assertIn("2 Karten importiert.", self.client.get(res.headers["location"], headers=ALICE).text)
        conn = db.connect(get_settings().db_path)
        self.assertEqual(conn.execute("SELECT COUNT(*) AS n FROM cards").fetchone()["n"], 3)
        conn.close()
        # Die Vorschau ist danach weg.
        self.assertIn("import-expired", self.client.get(url, headers=ALICE).headers["location"])

    def test_file_upload_and_settings(self):
        content = "Frage;Antwort\nWas ist FSRS?;Ein Planer für Wiederholungen\n".encode("utf-8-sig")
        res = self.client.post("/decks/1/import", files={"file": ("liste.csv", content, "text/csv")}, headers=ALICE)
        url = res.headers["location"]
        page = self.client.get(url, headers=ALICE).text
        self.assertIn("1 Karten importieren", page)
        swapped = self.client.get(url + "?delimiter=semicolon&header=yes&front=1&back=0", headers=ALICE).text
        self.assertIn("Ein Planer für Wiederholungen", swapped)

    def test_remnote_paste_preview(self):
        text = "- Was ist BV? >> Bundesverfassung\n- Die {{Leber}} entgiftet\n"
        url = self.client.post("/decks/1/import", data={"text": text}, headers=ALICE).headers["location"]
        page = self.client.get(url, headers=ALICE).text
        self.assertIn('<option value="remnote" selected>', page)
        self.assertNotIn('name="front"', page.split("import-stats")[0])
        self.assertIn("2 Karten importieren", page)
        res = self.client.post(url, data={"delimiter": "remnote", "header": "no", "front": 0, "back": 0}, headers=ALICE)
        self.assertIn("n=2", res.headers["location"])

    def test_empty_and_bad_tokens(self):
        res = self.client.post("/decks/1/import", data={"text": "  "}, headers=ALICE)
        self.assertIn("import-empty", res.headers["location"])
        self.assertIn("import-expired", self.client.get("/decks/1/import/..%2F..%2Fstudy",
                                                        headers=ALICE).headers.get("location", "import-expired"))
        self.assertIn("import-expired", self.client.get("/decks/1/import/" + "a" * 30, headers=ALICE).headers["location"])

    def test_bad_backups_are_rejected(self):
        res = self.client.post("/import", files={"file": ("x.zip", b"keine zip", "application/zip")}, headers=ALICE)
        self.assertEqual(res.status_code, 400)
        self.assertIn("keine ZIP-Datei", res.text)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("deck.json", json.dumps({"format": "anderes"}))
        res = self.client.post("/import", files={"file": ("x.zip", buf.getvalue(), "application/zip")}, headers=ALICE)
        self.assertIn("keine Studynator-Sicherung", res.text)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("deck.json", json.dumps({
                "format": "studynator-deck", "version": 1, "deck": {"name": "X"},
                "pages": [{"key": "p1", "number": 1, "image": "../../evil.jpg"}],
                "cards": [{"type": "basic", "front": "F", "back": "A", "fsrs": "kaputt", "reviews": [{"rating": 9}]}],
            }))
            zf.writestr("../../evil.jpg", b"\xff\xd8boese")
        res = self.client.post("/import", files={"file": ("x.zip", buf.getvalue(), "application/zip")}, headers=ALICE)
        self.assertEqual(res.status_code, 303)
        conn = db.connect(get_settings().db_path)
        card = conn.execute("SELECT * FROM cards WHERE front = 'F'").fetchone()
        self.assertEqual(card["reps"], 0)
        self.assertEqual(conn.execute("SELECT COUNT(*) AS n FROM reviews").fetchone()["n"], 0)
        conn.close()
        self.assertFalse((self.tmp / "evil.jpg").exists())
        self.assertFalse(any(p.name == "evil.jpg" for p in self.tmp.rglob("*.jpg")))


if __name__ == "__main__":
    unittest.main()
