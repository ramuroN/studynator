"""Weboberfläche und Routen."""

import hmac
import json
import logging
import re
import secrets
import shutil
import sqlite3
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import ai, db, exchange, pdf, srs, worker
from . import plan as planning
from .config import Settings, get_settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("study")

HERE = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=HERE / "templates")

# Hängt an CSS und JS, damit Browser nach einem Update nicht den alten Stand nehmen.
STATIC_VERSION = get_settings().commit if get_settings().commit != "dev" else str(int(time.time()))

USERNAME = re.compile(r"^[A-Za-z0-9._@+-]{1,128}$")
CHUNK = 1024 * 1024

MESSAGES = {
    "deck-created": "Kurs angelegt.",
    "deck-renamed": "Kurs umbenannt.",
    "deck-deleted": "Kurs gelöscht.",
    "uploaded": "Hochgeladen; die Seiten werden vorbereitet.",
    "not-pdf": "Das ist kein PDF.",
    "too-large": "Die Datei ist zu gross.",
    "generating": "Vorschläge werden erzeugt.",
    "doc-deleted": "Dokument gelöscht; die Karten bleiben.",
    "card-added": "Karte angelegt.",
    "card-saved": "Karte gespeichert.",
    "card-deleted": "Karte gelöscht.",
    "card-suspended": "Karte ausgesetzt; sie kommt beim Lernen nicht mehr.",
    "card-resumed": "Karte wieder aufgenommen.",
    "undone": "Letzte Bewertung zurückgenommen.",
    "undo-none": "Es gibt keine Bewertung zum Zurücknehmen.",
    "accepted-all": "Alle offenen Vorschläge übernommen.",
    "instructions-saved": "Anweisungen gespeichert; sie gelten ab den nächsten Vorschlägen.",
    "plan-saved": "Planung gespeichert.",
    "plan-invalid": "Planung nicht gespeichert: Datum oder Zahl ungültig.",
    "empty": "Vorder- und Rückseite dürfen nicht leer sein.",
    "imported": "{n} Karten importiert.",
    "import-empty": "Keine Datei und kein Text zum Importieren.",
    "import-expired": "Diese Import-Vorschau gibt es nicht mehr; bitte nochmals hochladen.",
    "backup-imported": "Sicherung eingelesen: {n} Karten.",
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    thread = lock = None
    if settings.worker:
        lock = worker.lock_data_dir(settings.data_dir)
    db.init(settings.db_path)
    settings.files_dir.mkdir(parents=True, exist_ok=True)
    if settings.worker:
        thread = worker.Worker(settings)
        thread.start()
    log.info("Studynator %s, Anbieter %s, Modell %s", settings.commit, settings.ai_provider, settings.model)
    yield
    if thread:
        thread.stop()
    if lock:
        lock.close()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")


@app.middleware("http")
async def guard(request: Request, call_next):
    settings = get_settings()
    if request.url.path != "/api/health":
        # NPM setzt das Secret; wer den Container direkt erreicht, kennt es nicht.
        if settings.proxy_secret and not hmac.compare_digest(
            request.headers.get("X-Proxy-Secret", ""), settings.proxy_secret
        ):
            return Response("Forbidden", status_code=403)
        # Formulare nur von der eigenen Seite (gegen fremde Seiten, die im
        # Browser des Nutzers etwas abschicken).
        if request.method not in ("GET", "HEAD") and request.headers.get("Sec-Fetch-Site", "same-origin") not in (
            "same-origin", "none"
        ):
            return Response("Cross-site request blocked", status_code=403)
    return await call_next(request)


@dataclass
class User:
    id: int
    name: str


def get_conn():
    conn = db.connect(get_settings().db_path)
    try:
        yield conn
    finally:
        conn.close()


def current_user(request: Request, conn: sqlite3.Connection = Depends(get_conn)) -> User:
    settings = get_settings()
    name = request.headers.get(settings.user_header, "").strip()
    if not name and settings.dev_user and not settings.proxy_secret:
        name = settings.dev_user
    if not name:
        raise HTTPException(401, "Keine Anmeldung (Header fehlt).")
    if not USERNAME.match(name):
        raise HTTPException(400, "Ungültiger Benutzername.")
    return User(db.user_id(conn, name), name)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)



def due_label(card: sqlite3.Row) -> str:
    if card["suspended"]:
        return "ausgesetzt"
    if card["reps"] == 0:
        return "neu"
    delta = datetime.fromisoformat(card["due"]) - utcnow()
    return "jetzt" if delta.total_seconds() <= 0 else f"in {srs.human_delta(delta)}"


def with_count(request: Request, message: str | None) -> str | None:
    """Setzt eine Zahl aus ?n= in Meldungen mit {n} ein; nur Ziffern."""
    if message and "{n}" in message:
        n = request.query_params.get("n", "")
        return message.replace("{n}", n if n.isdigit() else "Einige")
    return message


def back(url: str, msg: str | None = None) -> RedirectResponse:
    path, _, anchor = url.partition("#")
    if msg:
        path += f"{'&' if '?' in path else '?'}msg={msg}"
    return RedirectResponse(path + (f"#{anchor}" if anchor else ""), status_code=303)


def learn_url(learn: str, card_id: int | None = None, suspended: int | None = None) -> str:
    """Zurück in die Lernansicht, aus der jemand kam: Kurs-ID oder alle Kurse
    («all» oder leer); `card` zeigt diese Karte zuerst."""
    params = [f"deck={int(learn)}"] if learn.isdigit() else []
    if card_id:
        params.append(f"card={card_id}")
    if suspended:
        params.append(f"suspended={suspended}")
    return "/learn" + ("?" + "&".join(params) if params else "")


def wants_json(request: Request) -> bool:
    """Von app.js abgeschickt: Antwort als JSON statt Umleitung."""
    return "application/json" in request.headers.get("Accept", "")


def page(request: Request, name: str, user: User, conn: sqlite3.Connection, **context) -> Response:
    settings = get_settings()
    status_code = context.pop("status_code", 200)
    context.update(
        user=user,
        message=context.pop("message", None) or with_count(request, MESSAGES.get(request.query_params.get("msg", ""))),
        spent=db.month_spent(conn),
        budget=settings.monthly_budget_usd,
        settings=settings,
        srs=srs,
        due_label=due_label,
        v=STATIC_VERSION,
        # Für die Navigation: aktiver Reiter und Zahl der Karten, die jetzt dran sind.
        nav="learn" if request.url.path.startswith("/learn") else "home",
        header_counts=counts(conn, user),
        theme=conn.execute("SELECT accent, background FROM users WHERE id = ?", (user.id,)).fetchone(),
    )
    return templates.TemplateResponse(request, name, context, status_code=status_code)


def own_deck(conn: sqlite3.Connection, user: User, deck_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM decks WHERE id = ? AND user_id = ?", (deck_id, user.id)).fetchone()
    if row is None:
        raise HTTPException(404, "Kurs nicht gefunden.")
    return row


def own_document(conn: sqlite3.Connection, user: User, document_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM documents WHERE id = ? AND user_id = ?", (document_id, user.id)).fetchone()
    if row is None:
        raise HTTPException(404, "Dokument nicht gefunden.")
    return row


def own_card(conn: sqlite3.Connection, user: User, card_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM cards WHERE id = ? AND user_id = ?", (card_id, user.id)).fetchone()
    if row is None:
        raise HTTPException(404, "Karte nicht gefunden.")
    return row


def own_suggestion(conn: sqlite3.Connection, user: User, suggestion_id: int) -> sqlite3.Row:
    row = conn.execute(
        "SELECT s.*, d.deck_id FROM suggestions s JOIN documents d ON d.id = s.document_id "
        "WHERE s.id = ? AND d.user_id = ?",
        (suggestion_id, user.id),
    ).fetchone()
    if row is None:
        raise HTTPException(404, "Vorschlag nicht gefunden.")
    return row


def deck_plans(conn: sqlite3.Connection, user: User) -> dict[int, planning.DeckPlan]:
    """Neue Karten pro Kurs für heute: eigenes Limit oder Standard, mit
    Prüfungsdatum mindestens das nötige Tempo."""
    settings = get_settings()
    return planning.deck_plans(conn, user.id, settings.new_per_day, settings.tz)


def counts(conn: sqlite3.Connection, user: User, deck_id: int | None = None,
           plans: dict[int, planning.DeckPlan] | None = None) -> dict:
    plans = plans if plans is not None else deck_plans(conn, user)
    where, args = "user_id = ?", [user.id]
    if deck_id is not None:
        where += " AND deck_id = ?"
        args.append(deck_id)
    now = iso(utcnow())
    row = conn.execute(
        f"SELECT COUNT(*) AS total,"
        f" SUM(CASE WHEN suspended = 0 AND reps > 0 AND due <= ? THEN 1 ELSE 0 END) AS due,"
        f" SUM(CASE WHEN suspended = 0 AND reps = 0 THEN 1 ELSE 0 END) AS new,"
        f" SUM(suspended) AS suspended FROM cards WHERE {where}",
        [now, *args],
    ).fetchone()
    new_now = sum(p.left for p in plans.values() if deck_id is None or p.deck_id == deck_id)
    return {"total": row["total"], "due": row["due"] or 0, "new": row["new"] or 0, "new_now": new_now,
            "suspended": row["suspended"] or 0}


def insert_card(conn: sqlite3.Connection, user: User, deck_id: int, kind: str, front: str, back_text: str,
                page_id: int | None) -> int:
    card = srs.new_card()
    cur = conn.execute(
        "INSERT INTO cards (user_id, deck_id, page_id, type, front, back, fsrs, due, created, updated) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (user.id, deck_id, page_id, kind, front, back_text, srs.to_json(card), iso(card.due), db.now(), db.now()),
    )
    return cur.lastrowid


def clean_card(kind: str, front: str, back_text: str) -> tuple[str, str, str] | None:
    kind = kind if kind in ai.CARD_TYPES else "basic"
    front, back_text = front.strip(), back_text.strip()
    if not front or (kind == "basic" and not back_text):
        return None
    return kind, front, back_text


# --- Gesundheit ---------------------------------------------------------------


@app.get("/api/health")
def health():
    settings = get_settings()
    return {"status": "ok", "commit": settings.commit, "provider": settings.ai_provider}


# --- Farben ---------------------------------------------------------------------

HEX_COLOR = re.compile(r"^#[0-9a-f]{6}$")


@app.post("/settings/theme")
def save_theme(request: Request, accent: str = Form(""), background: str = Form(""),
               user: User = Depends(current_user), conn: sqlite3.Connection = Depends(get_conn)):
    """Akzentfarbe und Helligkeit des Hintergrunds pro Konto; leer heisst
    Standard. Die übrigen Farben leitet static/theme.js daraus ab."""
    accent, background = accent.strip().lower(), background.strip()
    if accent and not HEX_COLOR.match(accent):
        raise HTTPException(400, "Akzentfarbe als #rrggbb.")
    if background and not (background.isdigit() and int(background) <= 100):
        raise HTTPException(400, "Hintergrund als Zahl von 0 bis 100.")
    values = (accent or None, int(background) if background else None)
    conn.execute("UPDATE users SET accent = ?, background = ? WHERE id = ?", (*values, user.id))
    conn.commit()
    if wants_json(request):
        return JSONResponse({"ok": True, "accent": values[0], "background": values[1]})
    return back("/")


# --- Kurse ----------------------------------------------------------------------


@app.get("/")
def index(request: Request, user: User = Depends(current_user), conn: sqlite3.Connection = Depends(get_conn)):
    return render_index(request, user, conn)


def render_index(request: Request, user: User, conn: sqlite3.Connection, error: str | None = None) -> Response:
    decks = conn.execute("SELECT * FROM decks WHERE user_id = ? ORDER BY name COLLATE NOCASE", (user.id,)).fetchall()
    open_by_deck = dict(conn.execute(
        "SELECT d.deck_id, COUNT(*) FROM suggestions s JOIN documents d ON d.id = s.document_id "
        "WHERE d.user_id = ? AND s.status = 'open' GROUP BY d.deck_id",
        (user.id,),
    ).fetchall())
    plans = deck_plans(conn, user)
    rows = [{"deck": d, "counts": counts(conn, user, d["id"], plans), "open": open_by_deck.get(d["id"], 0),
             "plan": plans.get(d["id"])} for d in decks]
    return page(request, "index.html", user, conn, decks=rows, all_counts=counts(conn, user, plans=plans),
                stats=planning.stats(conn, user.id, get_settings().tz), message=error,
                status_code=400 if error else 200)


@app.post("/decks")
def create_deck(name: str = Form(...), user: User = Depends(current_user), conn: sqlite3.Connection = Depends(get_conn)):
    name = name.strip()[:120]
    if not name:
        return back("/")
    cur = conn.execute("INSERT INTO decks (user_id, name, created) VALUES (?, ?, ?)", (user.id, name, db.now()))
    conn.commit()
    return back(f"/decks/{cur.lastrowid}", "deck-created")


@app.get("/decks/{deck_id}")
def show_deck(request: Request, deck_id: int, q: str = "", user: User = Depends(current_user),
              conn: sqlite3.Connection = Depends(get_conn)):
    deck = own_deck(conn, user, deck_id)
    documents = conn.execute(
        "SELECT d.*, (SELECT COUNT(*) FROM suggestions s WHERE s.document_id = d.id AND s.status = 'open') AS open "
        "FROM documents d WHERE d.deck_id = ? ORDER BY d.id DESC",
        (deck_id,),
    ).fetchall()
    sql, args = "SELECT * FROM cards WHERE deck_id = ?", [deck_id]
    if q.strip():
        sql += " AND (front LIKE ? OR back LIKE ?)"
        args += [f"%{q.strip()}%"] * 2
    cards = conn.execute(sql + " ORDER BY id DESC LIMIT 500", args).fetchall()
    plans = deck_plans(conn, user)
    return page(request, "deck.html", user, conn, deck=deck, documents=documents, cards=cards,
                counts=counts(conn, user, deck_id, plans), q=q, feedback=feedback_counts(conn, deck_id), ai=ai,
                plan=plans[deck_id], stats=planning.stats(conn, user.id, get_settings().tz, deck_id),
                day_label=planning.day_label, today=planning.local_today(get_settings().tz),
                max_new=planning.MAX_NEW_PER_DAY, review_days=planning.REVIEW_DAYS_BEFORE_EXAM)


@app.post("/decks/{deck_id}/plan")
def save_plan(deck_id: int, exam_date: str = Form(""), new_per_day: str = Form(""),
              user: User = Depends(current_user), conn: sqlite3.Connection = Depends(get_conn)):
    """Prüfungsdatum und eigenes Limit für neue Karten; leer heisst ohne bzw. Standard."""
    own_deck(conn, user, deck_id)
    exam_date, new_per_day = exam_date.strip(), new_per_day.strip()
    exam = planning.parse_exam(exam_date)
    if (exam_date and (exam is None or not 2000 <= exam.year <= 2100)) or (
            new_per_day and not (new_per_day.isdigit() and int(new_per_day) <= planning.MAX_NEW_PER_DAY)):
        return back(f"/decks/{deck_id}#planung", "plan-invalid")
    conn.execute("UPDATE decks SET exam_date = ?, new_per_day = ? WHERE id = ?",
                 (exam.isoformat() if exam else None, int(new_per_day) if new_per_day else None, deck_id))
    conn.commit()
    return back(f"/decks/{deck_id}#planung", "plan-saved")


@app.post("/decks/{deck_id}/rename")
def rename_deck(deck_id: int, name: str = Form(...), user: User = Depends(current_user),
                conn: sqlite3.Connection = Depends(get_conn)):
    own_deck(conn, user, deck_id)
    if name.strip():
        conn.execute("UPDATE decks SET name = ? WHERE id = ?", (name.strip()[:120], deck_id))
        conn.commit()
    return back(f"/decks/{deck_id}", "deck-renamed")


@app.post("/decks/{deck_id}/instructions")
def save_instructions(deck_id: int, instructions: str = Form(""), user: User = Depends(current_user),
                      conn: sqlite3.Connection = Depends(get_conn)):
    own_deck(conn, user, deck_id)
    conn.execute("UPDATE decks SET instructions = ? WHERE id = ?",
                 (instructions.strip()[:ai.MAX_INSTRUCTIONS], deck_id))
    conn.commit()
    return back(f"/decks/{deck_id}#anweisungen", "instructions-saved")


def feedback_counts(conn: sqlite3.Connection, deck_id: int) -> dict:
    """Alle Entscheide des Kurses; in jede Anfrage gehen nur die jüngsten."""
    row = conn.execute(
        "SELECT"
        " SUM(CASE WHEN s.status = 'rejected' THEN 1 ELSE 0 END) AS rejected,"
        " SUM(CASE WHEN s.status = 'accepted' AND (s.type != s.orig_type OR s.front != s.orig_front"
        "  OR s.back != s.orig_back) THEN 1 ELSE 0 END) AS edited,"
        " SUM(CASE WHEN s.status = 'accepted' AND s.type = s.orig_type AND s.front = s.orig_front"
        "  AND s.back = s.orig_back THEN 1 ELSE 0 END) AS kept "
        "FROM suggestions s JOIN documents d ON d.id = s.document_id "
        "WHERE d.deck_id = ? AND s.orig_front IS NOT NULL",
        (deck_id,),
    ).fetchone()
    return {key: row[key] or 0 for key in ("edited", "kept", "rejected")}


@app.post("/decks/{deck_id}/delete")
def delete_deck(deck_id: int, user: User = Depends(current_user), conn: sqlite3.Connection = Depends(get_conn)):
    own_deck(conn, user, deck_id)
    settings = get_settings()
    docs = conn.execute("SELECT id FROM documents WHERE deck_id = ?", (deck_id,)).fetchall()
    conn.execute("DELETE FROM decks WHERE id = ?", (deck_id,))
    conn.commit()
    for doc in docs:
        shutil.rmtree(worker.doc_dir(settings, user.id, doc["id"]), ignore_errors=True)
    return back("/", "deck-deleted")


# --- Import und Export -------------------------------------------------------------

IMPORT_TOKEN = re.compile(r"^[A-Za-z0-9_-]{20,64}$")
IMPORT_MAX_AGE_S = 86400


async def read_limited(file: UploadFile, limit: int) -> bytes | None:
    """Liest eine hochgeladene Datei; None, wenn sie grösser als limit ist."""
    data = bytearray()
    while chunk := await file.read(CHUNK):
        data += chunk
        if len(data) > limit:
            return None
    return bytes(data)


def import_path(user: User, token: str) -> Path | None:
    if not IMPORT_TOKEN.match(token):
        return None
    return get_settings().data_dir / "imports" / str(user.id) / f"{token}.txt"


def download(body: bytes, filename: str, media_type: str) -> Response:
    ascii_name = re.sub(r"[^A-Za-z0-9._-]+", "_", filename).strip("_") or "export"
    return Response(body, media_type=media_type, headers={
        "Content-Disposition": f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename)}",
        "Cache-Control": "no-store",
    })


@app.get("/decks/{deck_id}/export.{fmt}")
def export_deck(deck_id: int, fmt: str, user: User = Depends(current_user),
                conn: sqlite3.Connection = Depends(get_conn)):
    deck = own_deck(conn, user, deck_id)
    settings = get_settings()
    base = deck["name"].strip() or "Kurs"
    if fmt == "csv":
        return download(exchange.export_csv(exchange.deck_cards(conn, deck_id)), f"{base}.csv",
                        "text/csv; charset=utf-8")
    if fmt == "tsv":
        return download(exchange.export_tsv(exchange.deck_cards(conn, deck_id)), f"{base} (Quizlet, GoodNotes).tsv",
                        "text/tab-separated-values; charset=utf-8")
    if fmt == "md":
        return download(exchange.export_remnote(deck["name"], exchange.deck_cards(conn, deck_id)),
                        f"{base} (RemNote).md", "text/markdown; charset=utf-8")
    if fmt == "zip":
        stamp = datetime.now(ZoneInfo(settings.tz)).strftime("%Y-%m-%d")
        return download(exchange.export_backup(conn, deck, settings.files_dir, settings.commit),
                        f"{base} (Studynator {stamp}).zip", "application/zip")
    raise HTTPException(404)


@app.post("/decks/{deck_id}/import")
async def import_table(deck_id: int, file: UploadFile | None = File(None), text: str = Form(""),
                       user: User = Depends(current_user), conn: sqlite3.Connection = Depends(get_conn)):
    own_deck(conn, user, deck_id)
    settings = get_settings()
    raw = None
    if file is not None and file.filename:
        data = await read_limited(file, settings.max_upload_mb * CHUNK)
        if data is None:
            return back(f"/decks/{deck_id}", "too-large")
        try:
            raw = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            # Ältere Excel-Exporte unter Windows.
            raw = data.decode("cp1252", errors="replace")
    elif text.strip():
        raw = text
    if not raw or not raw.strip():
        return back(f"/decks/{deck_id}", "import-empty")
    folder = settings.data_dir / "imports" / str(user.id)
    folder.mkdir(parents=True, exist_ok=True)
    # Liegengebliebene Vorschauen aufräumen.
    for old in folder.glob("*.txt"):
        if time.time() - old.stat().st_mtime > IMPORT_MAX_AGE_S:
            old.unlink(missing_ok=True)
    token = secrets.token_urlsafe(24)
    (folder / f"{token}.txt").write_text(raw, encoding="utf-8")
    return back(f"/decks/{deck_id}/import/{token}")


def parse_import(conn: sqlite3.Connection, deck_id: int, path: Path, delimiter: str, header: str,
                 front: int | None, back_col: int | None):
    parsed = exchange.parse_table(path.read_text(encoding="utf-8"), delimiter, header, front, back_col)
    existing = [r["front"] for r in conn.execute("SELECT front FROM cards WHERE deck_id = ?", (deck_id,))]
    fresh, dupes = exchange.split_duplicates(parsed.cards, existing)
    return parsed, fresh, dupes


@app.get("/decks/{deck_id}/import/{token}")
def import_preview(request: Request, deck_id: int, token: str, delimiter: str = "auto", header: str = "auto",
                   front: int | None = None, back_col: int | None = Query(None, alias="back"),
                   user: User = Depends(current_user),
                   conn: sqlite3.Connection = Depends(get_conn)):
    deck = own_deck(conn, user, deck_id)
    path = import_path(user, token)
    if path is None or not path.exists():
        return back(f"/decks/{deck_id}", "import-expired")
    parsed, fresh, dupes = parse_import(conn, deck_id, path, delimiter, header, front, back_col)
    return page(request, "import.html", user, conn, deck=deck, token=token, parsed=parsed, fresh=fresh,
                preview=fresh[:20], dupes=dupes, delimiters=exchange.DELIMITER_LABELS)


@app.post("/decks/{deck_id}/import/{token}")
def import_commit(deck_id: int, token: str, delimiter: str = Form("auto"), header: str = Form("auto"),
                  front: int = Form(0), back_col: int = Form(1, alias="back"), user: User = Depends(current_user),
                  conn: sqlite3.Connection = Depends(get_conn)):
    own_deck(conn, user, deck_id)
    path = import_path(user, token)
    if path is None or not path.exists():
        return back(f"/decks/{deck_id}", "import-expired")
    _parsed, fresh, _dupes = parse_import(conn, deck_id, path, delimiter, header, front, back_col)
    for card in fresh:
        insert_card(conn, user, deck_id, card.type, card.front, card.back, None)
    conn.commit()
    path.unlink(missing_ok=True)
    return back(f"/decks/{deck_id}?n={len(fresh)}", "imported")


@app.post("/import")
async def import_backup(request: Request, file: UploadFile = File(...), user: User = Depends(current_user),
                        conn: sqlite3.Connection = Depends(get_conn)):
    settings = get_settings()
    raw = await read_limited(file, settings.max_upload_mb * CHUNK)
    if raw is None:
        return render_index(request, user, conn, f"Die Datei ist grösser als {settings.max_upload_mb} MB.")
    try:
        deck_id, count = exchange.import_backup(conn, user.id, settings.files_dir, raw)
    except exchange.ExchangeError as exc:
        return render_index(request, user, conn, str(exc))
    return back(f"/decks/{deck_id}?n={count}", "backup-imported")


# --- Dokumente ------------------------------------------------------------------


@app.post("/decks/{deck_id}/upload")
async def upload(request: Request, deck_id: int, file: UploadFile = File(...), user: User = Depends(current_user),
                 conn: sqlite3.Connection = Depends(get_conn)):
    own_deck(conn, user, deck_id)
    settings = get_settings()
    limit = settings.max_upload_mb * CHUNK
    head = await file.read(1024)
    if not pdf.looks_like_pdf(head):
        return back(f"/decks/{deck_id}", "not-pdf")
    filename = Path(file.filename or "dokument.pdf").name[:200]
    cur = conn.execute(
        "INSERT INTO documents (user_id, deck_id, filename, status, model, created) VALUES (?, ?, ?, 'uploaded', ?, ?)",
        (user.id, deck_id, filename, settings.model, db.now()),
    )
    doc_id = cur.lastrowid
    target = worker.original_pdf(settings, user.id, doc_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    size = len(head)
    with target.open("wb") as out:
        out.write(head)
        while chunk := await file.read(CHUNK):
            size += len(chunk)
            if size > limit:
                break
            out.write(chunk)
    if size > limit:
        conn.rollback()
        shutil.rmtree(target.parent, ignore_errors=True)
        return back(f"/decks/{deck_id}", "too-large")
    worker.enqueue(conn, "render", doc_id)
    conn.commit()
    return back(f"/documents/{doc_id}", "uploaded")


@app.get("/documents/{document_id}")
def show_document(request: Request, document_id: int, user: User = Depends(current_user),
                  conn: sqlite3.Connection = Depends(get_conn)):
    doc = own_document(conn, user, document_id)
    deck = own_deck(conn, user, doc["deck_id"])
    pages = conn.execute("SELECT * FROM pages WHERE document_id = ? ORDER BY number", (document_id,)).fetchall()
    suggestions = conn.execute(
        "SELECT * FROM suggestions WHERE document_id = ? ORDER BY page_id, id", (document_id,)
    ).fetchall()
    by_page: dict[int, list] = {}
    for s in suggestions:
        by_page.setdefault(s["page_id"], []).append(s)
    without = [p["number"] for p in pages if p["id"] not in by_page and doc["progress"] >= p["number"]]
    open_count = sum(1 for s in suggestions if s["status"] == "open")
    settings = get_settings()
    # Kosten aller Seiten je Modell, damit die Auswahl den Preis zeigt.
    inputs = [ai.PageInput(p["number"], p["text"], p["width"], p["height"], Path()) for p in pages]
    extra = len(ai.guidance_text(worker.guidance(conn, deck["id"]))) // 3
    models = [(model, label, ai.estimate_usd(inputs, model, settings.pages_per_request, extra))
              for model, label in ai.models_with(settings.model)]
    return page(request, "document.html", user, conn, doc=doc, deck=deck, pages=pages, by_page=by_page,
                without=without, open_count=open_count, models=models,
                has_original=worker.original_pdf(settings, user.id, document_id).exists(),
                efforts=ai.efforts_with(settings.effort), model_label=ai.model_label)


@app.get("/api/documents/{document_id}/status")
def document_status(document_id: int, user: User = Depends(current_user), conn: sqlite3.Connection = Depends(get_conn)):
    doc = own_document(conn, user, document_id)
    return JSONResponse({"status": doc["status"], "progress": doc["progress"], "pages": doc["pages"]})


@app.post("/documents/{document_id}/generate")
def generate(document_id: int, first: int = Form(1), last: int = Form(0), model: str = Form(""),
             effort: str = Form(""), user: User = Depends(current_user),
             conn: sqlite3.Connection = Depends(get_conn)):
    doc = own_document(conn, user, document_id)
    if doc["status"] in ("uploaded", "rendering", "generating") or doc["pages"] == 0:
        return back(f"/documents/{document_id}")
    settings = get_settings()
    # Nur Werte aus der Liste; alles andere fällt auf den Standard zurück.
    if model not in dict(ai.models_with(settings.model)):
        model = settings.model
    if effort not in dict(ai.efforts_with(settings.effort)):
        effort = settings.effort
    last = last or doc["pages"]
    first, last = max(1, min(first, doc["pages"])), max(1, min(last, doc["pages"]))
    if first > last:
        first, last = last, first
    conn.execute(
        "UPDATE documents SET status = 'generating', error = NULL, gen_first = ?, gen_last = ?, progress = ? "
        "WHERE id = ?",
        (first, last, first - 1, document_id),
    )
    worker.enqueue(conn, "generate", document_id, {"first": first, "last": last, "model": model, "effort": effort})
    conn.commit()
    return back(f"/documents/{document_id}", "generating")


@app.post("/documents/{document_id}/resume")
def resume(document_id: int, user: User = Depends(current_user), conn: sqlite3.Connection = Depends(get_conn)):
    """Nach einem Fehler dort weitermachen, wo die Vorschläge aufgehört haben."""
    doc = own_document(conn, user, document_id)
    if doc["status"] != "failed" or not doc["gen_last"]:
        return back(f"/documents/{document_id}")
    first = max(doc["gen_first"] or 1, doc["progress"] + 1)
    # Mit demselben Modell und Denkaufwand wie der abgebrochene Auftrag.
    last_job = conn.execute(
        "SELECT params FROM jobs WHERE document_id = ? AND kind = 'generate' ORDER BY id DESC LIMIT 1", (document_id,)
    ).fetchone()
    previous = json.loads(last_job["params"]) if last_job else {}
    params = {"first": first, "last": doc["gen_last"]}
    params.update({k: previous[k] for k in ("model", "effort") if k in previous})
    conn.execute("UPDATE documents SET status = 'generating', error = NULL WHERE id = ?", (document_id,))
    worker.enqueue(conn, "generate", document_id, params)
    conn.commit()
    return back(f"/documents/{document_id}", "generating")


@app.post("/documents/{document_id}/delete")
def delete_document(document_id: int, user: User = Depends(current_user), conn: sqlite3.Connection = Depends(get_conn)):
    doc = own_document(conn, user, document_id)
    if doc["status"] in ("rendering", "generating"):
        return back(f"/documents/{document_id}")
    conn.execute("DELETE FROM documents WHERE id = ?", (document_id,))
    conn.commit()
    shutil.rmtree(worker.doc_dir(get_settings(), user.id, document_id), ignore_errors=True)
    return back(f"/decks/{doc['deck_id']}", "doc-deleted")


@app.post("/documents/{document_id}/accept-all")
def accept_all(document_id: int, user: User = Depends(current_user), conn: sqlite3.Connection = Depends(get_conn)):
    doc = own_document(conn, user, document_id)
    rows = conn.execute("SELECT * FROM suggestions WHERE document_id = ? AND status = 'open'", (document_id,)).fetchall()
    for s in rows:
        insert_card(conn, user, doc["deck_id"], s["type"], s["front"], s["back"], s["page_id"])
        conn.execute("UPDATE suggestions SET status = 'accepted', decided = ? WHERE id = ?", (db.now(), s["id"]))
    conn.commit()
    return back(f"/documents/{document_id}", "accepted-all")


def decided(request: Request, conn: sqlite3.Connection, suggestion_id: int) -> Response:
    """Nach einem Entscheid: für app.js der neue Stand als JSON, sonst zurück zur Folie."""
    s = conn.execute("SELECT * FROM suggestions WHERE id = ?", (suggestion_id,)).fetchone()
    if not wants_json(request):
        return back(f"/documents/{s['document_id']}#p{s['page_id']}")
    open_count = conn.execute("SELECT COUNT(*) FROM suggestions WHERE document_id = ? AND status = 'open'",
                              (s["document_id"],)).fetchone()[0]
    return JSONResponse({"ok": True, "status": s["status"], "text": srs.plain(s["type"], s["front"]),
                         "open": open_count})


@app.post("/suggestions/{suggestion_id}/accept")
def accept(request: Request, suggestion_id: int, type: str = Form("basic"), front: str = Form(""),
           back_text: str = Form("", alias="back"), user: User = Depends(current_user),
           conn: sqlite3.Connection = Depends(get_conn)):
    s = own_suggestion(conn, user, suggestion_id)
    if s["status"] != "open":
        return decided(request, conn, suggestion_id)
    cleaned = clean_card(type, front, back_text)
    if cleaned is None:
        if wants_json(request):
            return JSONResponse({"ok": False, "error": MESSAGES["empty"]}, status_code=400)
        return back(f"/documents/{s['document_id']}", "empty")
    insert_card(conn, user, s["deck_id"], *cleaned, s["page_id"])
    # orig_* bleibt: der Unterschied zur Fassung des Nutzers ist das Beispiel für die KI.
    conn.execute(
        "UPDATE suggestions SET status = 'accepted', type = ?, front = ?, back = ?, decided = ? WHERE id = ?",
        (*cleaned, db.now(), s["id"]),
    )
    conn.commit()
    return decided(request, conn, suggestion_id)


@app.post("/suggestions/{suggestion_id}/reject")
def reject(request: Request, suggestion_id: int, user: User = Depends(current_user),
           conn: sqlite3.Connection = Depends(get_conn)):
    s = own_suggestion(conn, user, suggestion_id)
    if s["status"] == "open":
        conn.execute("UPDATE suggestions SET status = 'rejected', decided = ? WHERE id = ?", (db.now(), s["id"]))
        conn.commit()
    return decided(request, conn, suggestion_id)


# --- Dateien --------------------------------------------------------------------


@app.get("/files/pages/{page_id}.jpg")
def page_file(page_id: int, user: User = Depends(current_user), conn: sqlite3.Connection = Depends(get_conn)):
    row = conn.execute(
        "SELECT p.number, d.id AS doc_id FROM pages p JOIN documents d ON d.id = p.document_id "
        "WHERE p.id = ? AND d.user_id = ?",
        (page_id, user.id),
    ).fetchone()
    if row is None:
        raise HTTPException(404)
    path = pdf.page_image(worker.doc_dir(get_settings(), user.id, row["doc_id"]), row["number"])
    if not path.exists():
        raise HTTPException(404)
    # no-cache: der Browser fragt per ETag nach (304). SQLite kann IDs gelöschter
    # Seiten wiederverwenden; ein fester Cache zeigte dann die alte Folie.
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "private, no-cache"})


@app.get("/files/documents/{document_id}.pdf")
def document_file(document_id: int, user: User = Depends(current_user), conn: sqlite3.Connection = Depends(get_conn)):
    doc = own_document(conn, user, document_id)
    path = worker.original_pdf(get_settings(), user.id, document_id)
    if not path.exists():
        raise HTTPException(404)
    return FileResponse(path, media_type="application/pdf", filename=doc["filename"],
                        content_disposition_type="inline", headers={"Cache-Control": "private, no-cache"})


# --- Karten ---------------------------------------------------------------------


@app.post("/decks/{deck_id}/cards")
def add_card(deck_id: int, type: str = Form("basic"), front: str = Form(""), back_text: str = Form("", alias="back"),
             user: User = Depends(current_user), conn: sqlite3.Connection = Depends(get_conn)):
    own_deck(conn, user, deck_id)
    cleaned = clean_card(type, front, back_text)
    if cleaned is None:
        return back(f"/decks/{deck_id}", "empty")
    insert_card(conn, user, deck_id, *cleaned, None)
    conn.commit()
    return back(f"/decks/{deck_id}", "card-added")


@app.get("/cards/{card_id}/edit")
def edit_card(request: Request, card_id: int, learn: str = "", user: User = Depends(current_user),
              conn: sqlite3.Connection = Depends(get_conn)):
    card = own_card(conn, user, card_id)
    deck = own_deck(conn, user, card["deck_id"])
    # Aus der Lernansicht: Speichern und Abbrechen führen dorthin zurück.
    learn = learn if learn == "all" or learn.isdigit() else ""
    cancel = learn_url(learn, card_id) if learn else f"/decks/{deck['id']}"
    return page(request, "card.html", user, conn, card=card, deck=deck, learn=learn, cancel=cancel)


@app.post("/cards/{card_id}")
def save_card(card_id: int, type: str = Form("basic"), front: str = Form(""), back_text: str = Form("", alias="back"),
              learn: str = Form(""), user: User = Depends(current_user), conn: sqlite3.Connection = Depends(get_conn)):
    card = own_card(conn, user, card_id)
    cleaned = clean_card(type, front, back_text)
    if cleaned is None:
        return back(f"/cards/{card_id}/edit" + (f"?learn={learn}" if learn == "all" or learn.isdigit() else ""),
                    "empty")
    conn.execute("UPDATE cards SET type = ?, front = ?, back = ?, updated = ? WHERE id = ?",
                 (*cleaned, db.now(), card_id))
    conn.commit()
    if learn:
        return back(learn_url(learn, card_id), "card-saved")
    return back(f"/decks/{card['deck_id']}", "card-saved")


@app.post("/cards/{card_id}/suspend")
def suspend_card(card_id: int, on: int = Form(1), learn: str = Form(""), user: User = Depends(current_user),
                 conn: sqlite3.Connection = Depends(get_conn)):
    """Aussetzen: Die Karte bleibt mit Lernstand erhalten, kommt aber nicht mehr dran."""
    own_card(conn, user, card_id)
    conn.execute("UPDATE cards SET suspended = ?, updated = ? WHERE id = ?", (int(bool(on)), db.now(), card_id))
    conn.commit()
    if learn:
        # Aus der Lernansicht: weiter zur nächsten Karte, mit «Wieder aufnehmen».
        return back(learn_url(learn, suspended=card_id if on else None), None if on else "card-resumed")
    return back(f"/cards/{card_id}/edit", "card-suspended" if on else "card-resumed")


@app.post("/cards/{card_id}/delete")
def delete_card(card_id: int, user: User = Depends(current_user), conn: sqlite3.Connection = Depends(get_conn)):
    card = own_card(conn, user, card_id)
    conn.execute("DELETE FROM cards WHERE id = ?", (card_id,))
    conn.commit()
    return back(f"/decks/{card['deck_id']}", "card-deleted")


# --- Lernen ---------------------------------------------------------------------


UNDO_WINDOW = timedelta(hours=1)


def last_review(conn: sqlite3.Connection, user: User) -> sqlite3.Row | None:
    """Jüngste Bewertung, wenn sie sich zurücknehmen lässt: mit gespeichertem
    Vorzustand (ältere und importierte haben keinen) und höchstens eine Stunde alt."""
    row = conn.execute(
        "SELECT r.*, c.type, c.front FROM reviews r JOIN cards c ON c.id = r.card_id "
        "WHERE r.user_id = ? ORDER BY r.reviewed_at DESC, r.id DESC LIMIT 1",
        (user.id,),
    ).fetchone()
    if row is None or row["prev"] is None or row["reviewed_at"] < iso(utcnow() - UNDO_WINDOW):
        return None
    return row


def next_card(conn: sqlite3.Connection, user: User, deck_id: int | None) -> sqlite3.Row | None:
    where, args = "user_id = ? AND suspended = 0", [user.id]
    if deck_id is not None:
        where += " AND deck_id = ?"
        args.append(deck_id)
    now = iso(utcnow())
    row = conn.execute(f"SELECT * FROM cards WHERE {where} AND reps > 0 AND due <= ? ORDER BY due LIMIT 1",
                       [*args, now]).fetchone()
    if row is not None:
        return row
    # Neue Karten nur aus Kursen mit Rest für heute, die nächste Prüfung zuerst.
    for p in planning.new_order(deck_plans(conn, user)):
        if deck_id is not None and p.deck_id != deck_id:
            continue
        row = conn.execute(
            "SELECT * FROM cards WHERE user_id = ? AND deck_id = ? AND suspended = 0 AND reps = 0 ORDER BY id LIMIT 1",
            (user.id, p.deck_id),
        ).fetchone()
        if row is not None:
            return row
    return None


@app.get("/learn")
def learn(request: Request, deck: int | None = None, show: int | None = Query(None, alias="card"),
          suspended: int | None = None, user: User = Depends(current_user),
          conn: sqlite3.Connection = Depends(get_conn)):
    deck_row = own_deck(conn, user, deck) if deck is not None else None
    card = None
    if show is not None:
        # Nach Rückgängig oder Bearbeiten: dieselbe Karte nochmals.
        card = conn.execute("SELECT * FROM cards WHERE id = ? AND user_id = ? AND suspended = 0",
                            (show, user.id)).fetchone()
        if card is not None and deck is not None and card["deck_id"] != deck:
            card = None
    card = card or next_card(conn, user, deck)
    paused = None
    if suspended is not None:
        paused = conn.execute("SELECT * FROM cards WHERE id = ? AND user_id = ? AND suspended = 1",
                              (suspended, user.id)).fetchone()
    plans = deck_plans(conn, user)
    context = {"deck": deck_row, "counts": counts(conn, user, deck, plans), "undo": last_review(conn, user),
               "paused": paused, "learn_key": str(deck) if deck is not None else "all",
               "plan": plans.get(deck) if deck is not None else None}
    if card is None:
        where, args = "user_id = ? AND suspended = 0 AND reps > 0 AND due > ?", [user.id, iso(utcnow())]
        if deck is not None:
            where += " AND deck_id = ?"
            args.append(deck)
        upcoming = conn.execute(f"SELECT MIN(due) AS due FROM cards WHERE {where}", args).fetchone()["due"]
        wait = None
        if upcoming:
            delta = datetime.fromisoformat(upcoming) - utcnow()
            if delta < timedelta(hours=1):
                wait = max(1, int(delta.total_seconds()))
        return page(request, "learn.html", user, conn, card=None, upcoming=upcoming, wait=wait,
                    upcoming_text=srs.human_delta(datetime.fromisoformat(upcoming) - utcnow()) if upcoming else None,
                    **context)
    card_deck = deck_row or own_deck(conn, user, card["deck_id"])
    return page(request, "learn.html", user, conn, card=card, card_deck=card_deck,
                preview=srs.preview(srs.from_json(card["fsrs"])), labels=srs.RATING_LABELS, **context)


# Vor /learn/{card_id}, sonst nimmt FastAPI «undo» für eine Karten-ID.
@app.post("/learn/undo")
def undo(deck: str = Form(""), user: User = Depends(current_user), conn: sqlite3.Connection = Depends(get_conn)):
    """Letzte Bewertung zurücknehmen: alter Lernstand zurück, Bewertung weg.
    Mehrmals hintereinander geht Schritt für Schritt zurück."""
    review = last_review(conn, user)
    if review is None:
        return back(learn_url(deck), "undo-none")
    prev = json.loads(review["prev"])
    conn.execute("UPDATE cards SET fsrs = ?, due = ?, reps = ?, lapses = ?, updated = ? WHERE id = ?",
                 (prev["fsrs"], prev["due"], prev["reps"], prev["lapses"], db.now(), review["card_id"]))
    conn.execute("DELETE FROM reviews WHERE id = ?", (review["id"],))
    conn.commit()
    return back(learn_url(deck, review["card_id"]), "undone")


@app.post("/learn/{card_id}")
def rate(card_id: int, rating: int = Form(...), duration_ms: int = Form(0), deck: str = Form(""),
         reps: int | None = Form(None), user: User = Depends(current_user),
         conn: sqlite3.Connection = Depends(get_conn)):
    card = own_card(conn, user, card_id)
    if rating not in srs.RATINGS:
        raise HTTPException(400, "Ungültige Bewertung.")
    # Doppelt abgeschickt (zweimal getippt, Zurück-Taste): schon bewertet, nichts tun.
    if (reps is not None and reps != card["reps"]) or card["suspended"]:
        return back(learn_url(deck))
    before = srs.from_json(card["fsrs"])
    was_review = before.state.name == "Review"
    after = srs.review(before, rating)
    prev = json.dumps({"fsrs": card["fsrs"], "due": card["due"], "reps": card["reps"], "lapses": card["lapses"]})
    conn.execute(
        "UPDATE cards SET fsrs = ?, due = ?, reps = reps + 1, lapses = lapses + ?, updated = ? WHERE id = ?",
        (srs.to_json(after), iso(after.due), int(was_review and rating == 1), db.now(), card_id),
    )
    conn.execute(
        "INSERT INTO reviews (card_id, user_id, rating, was_new, reviewed_at, duration_ms, prev) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (card_id, user.id, rating, int(card["reps"] == 0), db.now(), max(0, min(duration_ms, 3_600_000)), prev),
    )
    conn.commit()
    return back(learn_url(deck))
