import json
import os
import sqlite3
import time
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from helpers import TempEnv, make_pdf

from study import ai, db, pdf, srs, worker


class PdfTests(TempEnv):
    def test_render_pages_and_text(self):
        source = make_pdf(self.tmp / "a.pdf", ["Zellatmung\nfindet in den Mitochondrien statt", ""])
        pages = pdf.render(source, self.tmp / "out", max_pages=10)
        self.assertEqual([p.number for p in pages], [1, 2])
        self.assertIn("Mitochondrien", pages[0].text)
        self.assertTrue(pages[0].has_text)
        self.assertFalse(pages[1].has_text)
        self.assertEqual(max(pages[0].width, pages[0].height), pdf.TARGET_LONG_EDGE)
        self.assertTrue(pdf.page_image(self.tmp / "out", 2).exists())

    def test_too_many_pages(self):
        source = make_pdf(self.tmp / "a.pdf", ["x"] * 3)
        with self.assertRaises(pdf.PdfError):
            pdf.render(source, self.tmp / "out", max_pages=2)

    def test_clean_text_bullets(self):
        raw = "Vier Theorien\n\uf0a7Subordinationstheorie\n\uf0a7\nInteressentheorie\n  doppelt   leer  \n"
        self.assertEqual(pdf.clean_text(raw),
                         "Vier Theorien\n• Subordinationstheorie\n• Interessentheorie\ndoppelt leer")

    def test_not_a_pdf(self):
        broken = self.tmp / "b.pdf"
        broken.write_bytes(b"%PDF-1.7 kaputt")
        with self.assertRaises(pdf.PdfError):
            pdf.render(broken, self.tmp / "out", max_pages=10)
        self.assertFalse(pdf.looks_like_pdf(b"PK\x03\x04"))
        self.assertTrue(pdf.looks_like_pdf(b"%PDF-1.4"))


class SrsTests(unittest.TestCase):
    def test_review_moves_due(self):
        card = srs.new_card()
        start = datetime.now(timezone.utc)
        good = srs.review(card, 3, start)
        self.assertGreater(good.due, start)
        again = srs.review(card, 1, start)
        self.assertLessEqual(again.due, good.due)
        restored = srs.from_json(srs.to_json(good))
        self.assertEqual(restored.due, good.due)

    def test_preview_has_all_ratings(self):
        preview = srs.preview(srs.new_card())
        self.assertEqual(sorted(preview), [1, 2, 3, 4])
        self.assertEqual(preview[1], "1 min")

    def test_render_escapes_and_cloze(self):
        self.assertEqual(srs.render_front("basic", "<b>x</b>"), "&lt;b&gt;x&lt;/b&gt;")
        self.assertIn("[…]", srs.render_front("cloze", "Ort der {{Zellatmung}}"))
        self.assertNotIn("Zellatmung", srs.render_front("cloze", "Ort der {{Zellatmung}}"))
        self.assertIn("<mark class=\"cloze\">Zellatmung</mark>", srs.render_back("cloze", "Ort der {{Zellatmung}}", ""))
        self.assertIn("<strong>fett</strong>", srs.render_back("basic", "", "**fett**"))
        self.assertEqual(srs.plain("cloze", "a {{b}} c"), "a [b] c")
        self.assertEqual(srs.render_back("basic", "", "Mit `display: flex` und `<div>`"),
                         "Mit <code>display: flex</code> und <code>&lt;div&gt;</code>")
        self.assertIn("<code>", srs.render_front("cloze", "Die Eigenschaft `{{gap}}` setzt Abstände"))

    def test_instruction_templates_fit(self):
        names = [name for name, _ in ai.INSTRUCTION_TEMPLATES]
        self.assertEqual(len(names), len(set(names)))
        for name, text in ai.INSTRUCTION_TEMPLATES:
            self.assertLess(len(text), ai.MAX_INSTRUCTIONS, name)
            self.assertNotIn('"', text, name)

    def test_human_delta(self):
        self.assertEqual(srs.human_delta(timedelta(minutes=10)), "10 min")
        self.assertEqual(srs.human_delta(timedelta(days=3)), "3 T")


class AiTests(TempEnv):
    def page(self, number, text="Titel\nInhalt"):
        image = self.tmp / f"p{number}.jpg"
        image.write_bytes(b"\xff\xd8jpeg")
        return ai.PageInput(number, text, 1280, 720, image)

    def test_parse_cards_filters_invalid(self):
        raw = json.dumps({"cards": [
            {"page": 1, "type": "basic", "front": "Was?", "back": "Das."},
            {"page": 9, "type": "basic", "front": "Falsche Seite", "back": "x"},
            {"page": 1, "type": "basic", "front": "Ohne Antwort", "back": ""},
            {"page": 2, "type": "cloze", "front": "Ohne Lücke", "back": ""},
            {"page": 2, "type": "cloze", "front": "Die {{Lücke}}", "back": ""},
            {"page": 2, "type": "other", "front": "Typ", "back": "x"},
        ]})
        cards = ai.parse_cards(raw, {1, 2})
        self.assertEqual([(c.page, c.type) for c in cards], [(1, "basic"), (2, "cloze")])
        with self.assertRaises(ai.AIError):
            ai.parse_cards("kein json", {1})

    def test_estimate_and_cost(self):
        pages = [self.page(n) for n in range(1, 51)]
        estimate = ai.estimate_usd(pages, "claude-opus-5-5", 8)
        self.assertGreater(estimate, 0.2)
        self.assertLess(estimate, 2.0)
        self.assertLess(ai.estimate_usd(pages, "claude-haiku-5-5", 8), estimate / 10)
        self.assertAlmostEqual(ai.cost_usd("claude-opus-5-5", 1_000_000, 0), 4.0)
        self.assertEqual(ai.price("unbekannt"), max(ai.PRICES.values()))

    def test_request_shape(self):
        provider = ai.AnthropicProvider("claude-opus-5-5", "medium", client=object())
        params = provider.request([self.page(1), self.page(2)], ai.Context("Bio", 10, ["Alte Frage"]))
        self.assertEqual(params["fallbacks"], "default")
        self.assertEqual(params["betas"], [ai.FALLBACK_BETA])
        self.assertEqual(params["output_config"]["effort"], "medium")
        self.assertEqual(params["output_config"]["format"]["type"], "json_schema")
        content = params["messages"][0]["content"]
        self.assertEqual(sum(1 for b in content if b["type"] == "image"), 2)
        self.assertIn("Alte Frage", content[-1]["text"])
        self.assertNotIn("fallbacks", ai.AnthropicProvider("claude-haiku-5-5", "low", client=object())
                         .request([self.page(1)], ai.Context("Bio", 1)))
        # Ohne Anweisungen und Rückmeldungen kein leerer Block.
        self.assertEqual(sum(1 for b in content if b["type"] == "text"), 4)

    def test_guidance_in_prompt(self):
        guidance = ai.Guidance(
            instructions="Artikel immer mit Gesetz nennen.",
            edited=[(ai.Example("basic", "Worum geht es?", "Alles"), ai.Example("cloze", "Art. {{29}} BV", ""))],
            kept=[ai.Example("basic", "Was ist Privatrecht?", "Recht zwischen Privaten")],
            rejected=[ai.Example("basic", "Was steht auf der Titelfolie?", "x" * 500)],
        )
        content = ai.build_content([self.page(1)], ai.Context("Recht", 3, [], guidance))
        text = content[1]["text"]
        self.assertIn("Artikel immer mit Gesetz nennen.", text)
        self.assertIn("vorher: [Frage/Antwort] Vorderseite: Worum geht es?", text)
        self.assertIn("nachher: [Lückentext] Vorderseite: Art. {{29}} BV | Rückseite: (leer)", text)
        self.assertIn("Unverändert übernommen:", text)
        self.assertIn("Verworfen:", text)
        self.assertLess(len(text), 2000)
        self.assertFalse(ai.Guidance())

    def test_chosen_types_reach_schema_prompt_and_parser(self):
        provider = ai.AnthropicProvider("claude-opus-5-5", "medium", client=object())
        ctx = ai.Context("Bio", 1, types=("cloze", "twoway"))
        params = provider.request([self.page(1)], ctx)
        enum = params["output_config"]["format"]["schema"]["properties"]["cards"]["items"]["properties"]["type"]["enum"]
        self.assertEqual(enum, ["cloze", "twoway"])
        self.assertIn('Erlaubte Kartentypen: "cloze" (Lückentext), "twoway" (Zwei-Wege).',
                      params["messages"][0]["content"][0]["text"])
        self.assertIn('Typ "twoway"', params["system"])
        raw = json.dumps({"cards": [
            {"page": 1, "type": "basic", "front": "Nicht gewählt", "back": "x"},
            {"page": 1, "type": "twoway", "front": "Mitochondrium", "back": ""},
            {"page": 1, "type": "twoway", "front": "Mitochondrium", "back": "Kraftwerk der Zelle"},
        ]})
        cards = ai.parse_cards(raw, {1}, ctx.types)
        self.assertEqual([(c.type, c.back) for c in cards], [("twoway", "Kraftwerk der Zelle")])
        self.assertEqual(ai.clean_types(["twoway", "x", "basic", ""]), ("basic", "twoway"))
        self.assertEqual(ai.clean_types([]), ())

    def test_fake_provider_follows_types(self):
        page = self.page(1, "Glykolyse\nzerlegt Glukose")
        cloze = ai.FakeProvider().generate([page], ai.Context("K", 1, types=("cloze",))).cards[0]
        self.assertEqual((cloze.type, cloze.front, cloze.back), ("cloze", "Thema der Folie: {{Glykolyse}}", ""))
        twoway = ai.FakeProvider().generate([page], ai.Context("K", 1, types=("twoway",))).cards[0]
        self.assertEqual((twoway.type, twoway.front, twoway.back), ("twoway", "Glykolyse", "zerlegt Glukose"))

    def test_fake_provider_keeps_lines(self):
        result = ai.FakeProvider().generate([self.page(1, "Titel\n\uf0a7 Erstens\n\uf0a7 Zweitens")],
                                            ai.Context("K", 1))
        self.assertEqual(result.cards[0].back, "• Erstens\n• Zweitens")

    def test_generate_with_stub_client(self):
        message = SimpleNamespace(
            model="claude-opus-5-5",
            stop_reason="end_turn",
            usage=SimpleNamespace(input_tokens=1000, output_tokens=500, cache_creation_input_tokens=0,
                                  cache_read_input_tokens=0),
            content=[SimpleNamespace(type="thinking", thinking=""),
                     SimpleNamespace(type="text", text=json.dumps(
                         {"cards": [{"page": 1, "type": "basic", "front": "F", "back": "A"}]}))],
        )
        provider = ai.AnthropicProvider("claude-opus-5-5", "medium", client=StubClient(message))
        result = provider.generate([self.page(1)], ai.Context("Bio", 1))
        self.assertEqual(len(result.cards), 1)
        self.assertAlmostEqual(result.cost_usd, (1000 * 4 + 500 * 20) / 1_000_000)

    def test_refusal_keeps_cost(self):
        message = SimpleNamespace(
            model="claude-opus-5-5", stop_reason="refusal",
            usage=SimpleNamespace(input_tokens=100, output_tokens=0), content=[],
        )
        provider = ai.AnthropicProvider("claude-opus-5-5", "medium", client=StubClient(message))
        with self.assertRaises(ai.AIError) as ctx:
            provider.generate([self.page(1)], ai.Context("Bio", 1))
        self.assertIsNotNone(ctx.exception.result)
        self.assertEqual(ctx.exception.result.input_tokens, 100)


class StubClient:
    def __init__(self, message):
        self.beta = SimpleNamespace(messages=SimpleNamespace(stream=lambda **params: StubStream(message)))


class StubStream:
    def __init__(self, message):
        self.message = message

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get_final_message(self):
        return self.message


class LockTests(TempEnv):
    def test_second_process_on_same_data_dir_is_refused(self):
        first = worker.lock_data_dir(self.tmp / "data")
        try:
            with self.assertRaises(worker.AlreadyRunning):
                worker.lock_data_dir(self.tmp / "data")
            other = worker.lock_data_dir(self.tmp / "andere-daten")
            other.close()
        finally:
            first.close()
        worker.lock_data_dir(self.tmp / "data").close()


class DbTests(TempEnv):
    def test_migration_from_first_schema(self):
        path = self.tmp / "old.sqlite"
        conn = sqlite3.connect(path)
        conn.executescript(db.SCHEMA)
        conn.execute("INSERT INTO users (name, created) VALUES ('a', 'x')")
        conn.execute("INSERT INTO decks (user_id, name, created) VALUES (1, 'Alt', 'x')")
        conn.commit()
        conn.close()
        db.init(path)
        db.init(path)  # zweimal: nichts doppelt
        conn = db.connect(path)
        self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], len(db.MIGRATIONS))
        self.assertIn("model", {r["name"] for r in conn.execute("PRAGMA table_info(suggestions)")})
        self.assertEqual(conn.execute("SELECT instructions FROM decks").fetchone()["instructions"], "")
        columns = {r["name"] for r in conn.execute("PRAGMA table_info(suggestions)")}
        self.assertTrue({"orig_type", "orig_front", "orig_back", "decided"} <= columns)
        self.assertIn("suspended", {r["name"] for r in conn.execute("PRAGMA table_info(cards)")})
        self.assertIn("prev", {r["name"] for r in conn.execute("PRAGMA table_info(reviews)")})
        self.assertTrue({"accent", "background"} <= {r["name"] for r in conn.execute("PRAGMA table_info(users)")})
        self.assertTrue({"exam_date", "new_per_day"} <= {r["name"] for r in conn.execute("PRAGMA table_info(decks)")})
        conn.close()

    def test_snapshot_once_per_day(self):
        path = Path(self.tmp / "data" / "study.sqlite")
        db.init(path)
        target = db.snapshot(path, self.tmp / "snap")
        self.assertTrue(target.exists())
        self.assertIsNone(db.snapshot(path, self.tmp / "snap"))
        conn = sqlite3.connect(target)
        self.assertIn(("decks",), conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall())
        conn.close()

    def test_snapshot_refreshes_after_changes(self):
        path = Path(self.tmp / "data" / "study.sqlite")
        db.init(path)
        target = db.snapshot(path, self.tmp / "snap")
        hour_ago = time.time() - 3700
        # ohne Änderung bleibt die alte Kopie, auch wenn sie alt ist
        os.utime(target, (hour_ago, hour_ago))
        os.utime(path, (hour_ago - 10, hour_ago - 10))
        self.assertIsNone(db.snapshot(path, self.tmp / "snap"))
        conn = db.connect(path)
        conn.execute("INSERT INTO users (name, created) VALUES ('anna', 'x')")
        conn.commit()
        conn.close()
        # frisch geschrieben: noch keine Stunde alt
        os.utime(target, None)
        self.assertIsNone(db.snapshot(path, self.tmp / "snap"))
        os.utime(target, (hour_ago, hour_ago))
        self.assertEqual(db.snapshot(path, self.tmp / "snap"), target)
        copy = sqlite3.connect(target)
        self.assertEqual(copy.execute("SELECT name FROM users").fetchall(), [("anna",)])
        copy.close()

    def test_snapshot_is_plain_file_without_sidecars(self):
        path = Path(self.tmp / "data" / "study.sqlite")
        db.init(path)
        self.assertTrue(db.is_wal_file(path))
        target = db.snapshot(path, self.tmp / "snap")
        self.assertFalse(db.is_wal_file(target))
        # nur lesend öffnen legt nichts daneben an
        copy = sqlite3.connect(f"file:{target}?mode=ro", uri=True)
        self.assertEqual(copy.execute("PRAGMA journal_mode").fetchone()[0], "delete")
        copy.close()
        self.assertEqual(sorted(p.name for p in (self.tmp / "snap").iterdir()), [target.name])

    def test_old_wal_snapshot_is_rewritten_and_sidecars_removed(self):
        path = Path(self.tmp / "data" / "study.sqlite")
        db.init(path)
        target = self.tmp / "snap" / f"study-{date.today().isoformat()}.sqlite"
        target.parent.mkdir()
        # wie bis 92de274: Kopie mit WAL-Kopfzeile, daneben eine fremde -wal
        src, dst = db.connect(path), sqlite3.connect(target)
        src.backup(dst)
        dst.close()
        src.close()
        self.assertTrue(db.is_wal_file(target))
        Path(f"{target}-wal").write_bytes(b"alt")
        Path(f"{target}-shm").write_bytes(b"alt")
        self.assertEqual(db.snapshot(path, self.tmp / "snap"), target)
        self.assertFalse(db.is_wal_file(target))
        self.assertEqual(sorted(p.name for p in target.parent.iterdir()), [target.name])
        # danach gilt wieder die Stunde
        self.assertIsNone(db.snapshot(path, self.tmp / "snap"))


if __name__ == "__main__":
    unittest.main()
