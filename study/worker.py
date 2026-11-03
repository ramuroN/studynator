"""Hintergrundjobs: PDF rendern, Vorschläge erzeugen, Tageskopie der Datenbank.
Ein Thread im selben Prozess; die Jobs stehen in der Tabelle `jobs`."""

import difflib
import json
import logging
import os
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import ai, db, pdf
from .config import Settings

log = logging.getLogger("study.worker")

MAX_ATTEMPTS = 3
SIMILAR = 0.85


class BudgetError(Exception):
    pass


def doc_dir(settings: Settings, user_id: int, document_id: int) -> Path:
    return settings.files_dir / str(user_id) / str(document_id)


def original_pdf(settings: Settings, user_id: int, document_id: int) -> Path:
    return doc_dir(settings, user_id, document_id) / "original.pdf"


def enqueue(conn: sqlite3.Connection, kind: str, document_id: int, params: dict | None = None,
            delay_s: int = 0) -> None:
    run_after = (datetime.now(timezone.utc) + timedelta(seconds=delay_s)).isoformat(timespec="seconds")
    conn.execute(
        "INSERT INTO jobs (kind, document_id, params, run_after, created) VALUES (?, ?, ?, ?, ?)",
        (kind, document_id, json.dumps(params or {}), run_after, db.now()),
    )


def reset_running(conn: sqlite3.Connection) -> None:
    """Nach einem Neustart: abgebrochene Jobs wieder einreihen."""
    conn.execute("UPDATE jobs SET status = 'queued' WHERE status = 'running'")
    conn.commit()


def _claim(conn: sqlite3.Connection) -> sqlite3.Row | None:
    row = conn.execute(
        "UPDATE jobs SET status = 'running', attempts = attempts + 1 WHERE id = ("
        " SELECT id FROM jobs WHERE status = 'queued' AND run_after <= ? ORDER BY id LIMIT 1"
        ") RETURNING *",
        (db.now(),),
    ).fetchone()
    conn.commit()
    return row


def _finish(conn: sqlite3.Connection, job_id: int, status: str, error: str | None = None) -> None:
    conn.execute("UPDATE jobs SET status = ?, error = ?, finished = ? WHERE id = ?", (status, error, db.now(), job_id))
    conn.commit()


def _fail_document(conn: sqlite3.Connection, document_id: int, message: str) -> None:
    conn.execute("UPDATE documents SET status = 'failed', error = ? WHERE id = ?", (message, document_id))
    conn.commit()


def process_one(settings: Settings, provider=None) -> bool:
    """Arbeitet höchstens einen Job ab. True, wenn es einen gab."""
    conn = db.connect(settings.db_path)
    try:
        job = _claim(conn)
        if job is None:
            return False
        try:
            if job["kind"] == "render":
                run_render(conn, settings, job)
            elif job["kind"] == "generate":
                if provider is None:
                    # Modell und Denkaufwand wählt der Nutzer pro Auftrag.
                    params = json.loads(job["params"])
                    provider = ai.get_provider(settings.ai_provider, params.get("model", settings.model),
                                               params.get("effort", settings.effort))
                run_generate(conn, settings, job, provider)
            else:
                raise RuntimeError(f"unbekannter Job {job['kind']}")
            _finish(conn, job["id"], "done")
        except ai.AIError as exc:
            if exc.retry and job["attempts"] < MAX_ATTEMPTS:
                log.warning("Job %s: %s, neuer Versuch", job["id"], exc)
                params = json.loads(job["params"])
                progress = conn.execute("SELECT progress FROM documents WHERE id = ?", (job["document_id"],)).fetchone()
                params["first"] = max(params.get("first", 1), (progress["progress"] if progress else 0) + 1)
                conn.execute(
                    "UPDATE jobs SET status = 'queued', params = ?, run_after = ?, error = ? WHERE id = ?",
                    (json.dumps(params),
                     (datetime.now(timezone.utc) + timedelta(seconds=30 * job["attempts"])).isoformat(timespec="seconds"),
                     str(exc), job["id"]),
                )
                conn.commit()
            else:
                _fail_document(conn, job["document_id"], str(exc))
                _finish(conn, job["id"], "failed", str(exc))
        except (pdf.PdfError, BudgetError) as exc:
            log.warning("Job %s: %s", job["id"], exc)
            _fail_document(conn, job["document_id"], str(exc))
            _finish(conn, job["id"], "failed", str(exc))
        except Exception:  # Job darf den Worker nicht beenden
            log.exception("Job %s fehlgeschlagen", job["id"])
            message = "Interner Fehler, siehe Log."
            _fail_document(conn, job["document_id"], message)
            _finish(conn, job["id"], "failed", message)
        return True
    finally:
        conn.close()


def run_render(conn: sqlite3.Connection, settings: Settings, job: sqlite3.Row) -> None:
    doc = conn.execute("SELECT * FROM documents WHERE id = ?", (job["document_id"],)).fetchone()
    if doc is None:
        return
    conn.execute("UPDATE documents SET status = 'rendering', error = NULL WHERE id = ?", (doc["id"],))
    conn.commit()
    out = doc_dir(settings, doc["user_id"], doc["id"])
    pages = pdf.render(original_pdf(settings, doc["user_id"], doc["id"]), out, settings.max_pages)
    conn.execute("DELETE FROM pages WHERE document_id = ?", (doc["id"],))
    conn.executemany(
        "INSERT INTO pages (document_id, number, text, has_text, width, height) VALUES (?, ?, ?, ?, ?, ?)",
        [(doc["id"], p.number, p.text, int(p.has_text), p.width, p.height) for p in pages],
    )
    inputs = [ai.PageInput(p.number, p.text, p.width, p.height, Path()) for p in pages]
    extra = len(ai.guidance_text(guidance(conn, doc["deck_id"]))) // 3
    estimate = ai.estimate_usd(inputs, settings.model, settings.pages_per_request, extra)
    conn.execute(
        "UPDATE documents SET status = 'rendered', pages = ?, estimate_usd = ? WHERE id = ?",
        (len(pages), estimate, doc["id"]),
    )
    conn.commit()


def _existing_fronts(conn: sqlite3.Connection, deck_id: int) -> list[str]:
    rows = conn.execute(
        "SELECT front FROM cards WHERE deck_id = ? UNION ALL "
        "SELECT s.front FROM suggestions s JOIN documents d ON d.id = s.document_id "
        "WHERE d.deck_id = ? AND s.status != 'rejected'",
        (deck_id, deck_id),
    ).fetchall()
    return [r["front"] for r in rows]


def _normalize(text: str) -> str:
    return " ".join(text.lower().split())


def guidance(conn: sqlite3.Connection, deck_id: int) -> ai.Guidance:
    """Anweisungen des Kurses und die jüngsten Entscheide zu Vorschlägen."""
    deck = conn.execute("SELECT instructions FROM decks WHERE id = ?", (deck_id,)).fetchone()
    result = ai.Guidance(instructions=deck["instructions"] if deck else "")
    rows = conn.execute(
        "SELECT s.status, s.type, s.front, s.back, s.orig_type, s.orig_front, s.orig_back "
        "FROM suggestions s JOIN documents d ON d.id = s.document_id "
        "WHERE d.deck_id = ? AND s.status IN ('accepted', 'rejected') AND s.orig_front IS NOT NULL "
        "ORDER BY s.decided DESC, s.id DESC LIMIT 300",
        (deck_id,),
    ).fetchall()
    for r in rows:
        before = ai.Example(r["orig_type"], r["orig_front"], r["orig_back"])
        if r["status"] == "rejected":
            if len(result.rejected) < ai.MAX_REJECTED:
                result.rejected.append(before)
            continue
        after = ai.Example(r["type"], r["front"], r["back"])
        changed = (before.type, _normalize(before.front), _normalize(before.back)) != (
            after.type, _normalize(after.front), _normalize(after.back))
        if changed and len(result.edited) < ai.MAX_EDITED:
            result.edited.append((before, after))
        elif not changed and len(result.kept) < ai.MAX_KEPT:
            result.kept.append(after)
    return result


def similar_front(front: str, existing: list[str]) -> str | None:
    target = _normalize(front)
    for other in existing:
        if difflib.SequenceMatcher(None, target, _normalize(other)).ratio() >= SIMILAR:
            return other
    return None


def run_generate(conn: sqlite3.Connection, settings: Settings, job: sqlite3.Row, provider) -> None:
    doc = conn.execute(
        "SELECT d.*, k.name AS deck_name FROM documents d JOIN decks k ON k.id = d.deck_id WHERE d.id = ?",
        (job["document_id"],),
    ).fetchone()
    if doc is None:
        return
    params = json.loads(job["params"])
    first, last = params.get("first", 1), params.get("last", doc["pages"])
    # Nach einem Neustart mitten im Job (reset_running) nicht von vorn: fertige
    # Abschnitte sind schon bezahlt und ihre Vorschläge gespeichert. Ein neuer
    # Auftrag setzt progress auf first - 1, dort ändert das nichts.
    first = max(first, doc["progress"] + 1)
    conn.execute(
        "UPDATE documents SET status = 'generating', error = NULL, model = ? WHERE id = ?",
        (getattr(provider, "model", settings.model), doc["id"]),
    )
    conn.commit()
    rows = conn.execute(
        "SELECT * FROM pages WHERE document_id = ? AND number BETWEEN ? AND ? ORDER BY number",
        (doc["id"], first, last),
    ).fetchall()
    folder = doc_dir(settings, doc["user_id"], doc["id"])
    inputs = [ai.PageInput(r["number"], r["text"], r["width"], r["height"], pdf.page_image(folder, r["number"]))
              for r in rows]
    page_ids = {r["number"]: r["id"] for r in rows}
    existing = _existing_fronts(conn, doc["deck_id"])

    size = settings.pages_per_request
    for start in range(0, len(inputs), size):
        chunk = inputs[start:start + size]
        # Pro Abschnitt neu: Entscheide, die der Nutzer währenddessen trifft, zählen schon.
        ctx = ai.Context(doc["deck_name"], doc["pages"], existing, guidance(conn, doc["deck_id"]))
        if provider.name != "fake":
            spent = db.month_spent(conn)
            needed = ai.estimate_usd(chunk, provider.model, size, len(ai.guidance_text(ctx.guidance)) // 3)
            if spent + needed > settings.monthly_budget_usd:
                raise BudgetError(
                    f"Monatsbudget erreicht: {spent:.2f} von {settings.monthly_budget_usd:.2f} USD verbraucht. "
                    f"Fertig bis Seite {chunk[0].number - 1}."
                )
        try:
            result = provider.generate(chunk, ctx)
        except ai.AIError as exc:
            if exc.result is not None:
                _record_usage(conn, doc, exc.result)
                conn.commit()
            raise
        _record_usage(conn, doc, result)
        for card in result.cards:
            similar = similar_front(card.front, existing)
            conn.execute(
                "INSERT INTO suggestions (document_id, page_id, type, front, back, orig_type, orig_front, orig_back, "
                "similar_to, model, created) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (doc["id"], page_ids[card.page], card.type, card.front, card.back, card.type, card.front, card.back,
                 similar[:200] if similar else None, result.model or provider.model, db.now()),
            )
            existing.append(card.front)
        conn.execute("UPDATE documents SET progress = ? WHERE id = ?", (chunk[-1].number, doc["id"]))
        conn.commit()

    conn.execute("UPDATE documents SET status = 'done' WHERE id = ?", (doc["id"],))
    conn.commit()


def _record_usage(conn: sqlite3.Connection, doc: sqlite3.Row, result: ai.Result) -> None:
    if not (result.input_tokens or result.output_tokens):
        return
    conn.execute(
        "INSERT INTO usage (user_id, document_id, model, input_tokens, output_tokens, cost_usd, created) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (doc["user_id"], doc["id"], result.model, result.input_tokens, result.output_tokens, result.cost_usd, db.now()),
    )
    conn.execute("UPDATE documents SET cost_usd = cost_usd + ? WHERE id = ?", (result.cost_usd, doc["id"]))


class AlreadyRunning(RuntimeError):
    pass


def lock_data_dir(data_dir: Path):
    """Ein Prozess mit Worker pro Datenordner: Ein zweiter würde Jobs mit
    seinem eigenen Anbieter abarbeiten (z. B. «fake» statt der API). Die Sperre
    hält, solange die zurückgegebene Datei offen ist."""
    data_dir.mkdir(parents=True, exist_ok=True)
    handle = open(data_dir / "study.lock", "a+")
    try:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        handle.close()
        raise AlreadyRunning(f"Ein anderer Studynator-Prozess nutzt bereits {data_dir}.") from exc
    return handle


class Worker(threading.Thread):
    def __init__(self, settings: Settings):
        super().__init__(name="study-worker", daemon=True)
        self.settings = settings
        self.stop_event = threading.Event()

    def run(self) -> None:
        conn = db.connect(self.settings.db_path)
        try:
            reset_running(conn)
        finally:
            conn.close()
        while not self.stop_event.is_set():
            try:
                if db.snapshot(self.settings.db_path, self.settings.snapshots_dir):
                    log.info("Tageskopie der Datenbank geschrieben")
                if process_one(self.settings):
                    continue
            except Exception:
                log.exception("Worker-Durchlauf fehlgeschlagen")
            self.stop_event.wait(1.0)

    def stop(self) -> None:
        self.stop_event.set()
