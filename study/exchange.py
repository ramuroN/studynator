"""Import und Export: Tabellen (CSV/TSV) und die vollständige Sicherung eines
Kurses als ZIP (Karten mit FSRS-Stand, Bewertungen, Folienbilder)."""

import csv
import html
import io
import json
import re
import sqlite3
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import ai, db, pdf, plan, srs

TYPE_LABELS = {"basic": "Frage/Antwort", "cloze": "Lückentext"}
CSV_HEADER = ["Typ", "Vorderseite", "Rückseite", "Dokument", "Seite", "Fällig", "Wiederholungen", "Vergessen"]

# Spaltennamen, die der Import als Vorder- und Rückseite erkennt (klein geschrieben).
FRONT_NAMES = {"vorderseite", "front", "frage", "question", "begriff", "term", "text", "vorne"}
BACK_NAMES = {"rückseite", "back", "antwort", "answer", "definition", "back extra", "hinten"}
TYPE_NAMES = {"typ", "type", "kartentyp"}
TYPE_VALUES = {"frage/antwort": "basic", "basic": "basic", "lückentext": "cloze", "cloze": "cloze"}

DELIMITERS = {"tab": "\t", "semicolon": ";", "comma": ","}
DELIMITER_LABELS = {"tab": "Tabulator", "semicolon": "Strichpunkt", "comma": "Komma", "remnote": "RemNote-Syntax"}

# RemNote-Textsyntax (help.remnote.com, «How to import flashcards from text»):
# einzeilig «Frage >> Antwort» (auch ==, ::, ;;), rückwärts <<, beidseitig <>,
# ausgeschaltet >-; mehrzeilig >>> / <<< / <>< am Zeilenende mit Unterpunkten.
_REMNOTE_INLINE = re.compile(r"^(.*?\S)\s*(>>|<<|<>|>-|==)\s*(.*)$|^(.*?\S)\s+(::|;;)\s+(.*)$")
_REMNOTE_MULTI = re.compile(r"\s*(>>>|<<<|<><|>>1\.|<<1\.|<>1\.|>>A\))\s*$")
_REMNOTE_HINT = re.compile(r"\{\(\{.*?\}\)\}", re.S)
_GAP = re.compile(r"\{\{(.+?)\}\}", re.S)

# Grenzen für Sicherungen: Schutz vor ZIP-Bomben und unsinnig grossen Dateien.
BACKUP_FORMAT = "studynator-deck"
BACKUP_VERSION = 1
MAX_ZIP_ENTRIES = 20000
MAX_ZIP_TOTAL = 1024 * 1024 * 1024
MAX_JSON_BYTES = 64 * 1024 * 1024
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_IMPORT_CARDS = 50000

_ANKI_CLOZE = re.compile(r"\{\{c(\d+)::(.+?)(?:::[^}]*)?\}\}", re.S)
_TAG = re.compile(r"<[^>]+>")


class ExchangeError(Exception):
    """Fehler beim Einlesen, mit einer Meldung für die Oberfläche."""


# --- Export ---------------------------------------------------------------------


def _excel_safe(value: str) -> str:
    # Tabellenprogramme führen Zellen mit = + - @ als Formel aus.
    return "'" + value if value[:1] in ("=", "+", "-", "@") else value


def export_csv(rows: list[sqlite3.Row]) -> bytes:
    """Tabelle für Excel und Co.: Strichpunkt, Überschrift, UTF-8 mit BOM."""
    out = io.StringIO()
    writer = csv.writer(out, delimiter=";", lineterminator="\r\n")
    writer.writerow(CSV_HEADER)
    for r in rows:
        writer.writerow([
            TYPE_LABELS.get(r["type"], r["type"]),
            _excel_safe(r["front"]),
            _excel_safe(r["back"]),
            r["filename"] or "",
            r["number"] or "",
            r["due"] if r["reps"] else "",
            r["reps"],
            r["lapses"],
        ])
    return ("\ufeff" + out.getvalue()).encode("utf-8")


def _one_line(text: str) -> str:
    return " · ".join(part.strip() for part in text.replace("\t", " ").splitlines() if part.strip())


def _plain(text: str) -> str:
    # GoodNotes und Quizlet zeigen Markdown roh: Fett- und Codezeichen weg.
    return text.replace("**", "").replace("`", "")


def export_tsv(rows: list[sqlite3.Row]) -> bytes:
    """Text für Quizlet und GoodNotes: «Frage Tab Antwort», eine Karte pro Zeile,
    keine Überschrift (GoodNotes liest Spalte 1 als Frage, Spalte 2 als Antwort).
    Lücken kennen beide nicht: vorne «[…]», hinten das Wort."""
    lines = []
    for r in rows:
        if r["type"] == "cloze":
            gaps = _GAP.findall(r["front"])
            term = _GAP.sub("[…]", r["front"])
            definition = " / ".join(gaps) + (f" — {r['back']}" if r["back"].strip() else "")
        else:
            term, definition = r["front"], r["back"]
        lines.append(f"{_one_line(_plain(term))}\t{_one_line(_plain(definition))}")
    return ("\n".join(lines) + "\n").encode("utf-8")


def _rem_line(text: str) -> str:
    # Eine Rem ist eine Zeile; Zeilenumbrüche der Vorderseite werden Leerzeichen.
    return " ".join(part.strip() for part in text.splitlines() if part.strip())


def export_remnote(deck_name: str, rows: list[sqlite3.Row]) -> bytes:
    """Markdown in RemNote-Syntax: in RemNote importieren oder den Inhalt in ein
    Dokument einfügen. Mehrzeilige Antworten werden Unterpunkte (>>>)."""
    lines = [f"# {_rem_line(deck_name)}", ""]
    for r in rows:
        front = _rem_line(r["front"])
        back_lines = [part.strip() for part in r["back"].splitlines() if part.strip()]
        if r["type"] == "cloze":
            lines.append(f"- {front}")
            lines += [f"    - {part}" for part in back_lines]
        elif len(back_lines) == 1:
            lines.append(f"- {front} >> {back_lines[0]}")
        else:
            lines.append(f"- {front} >>>")
            lines += [f"    - {part}" for part in back_lines]
    return ("\n".join(lines) + "\n").encode("utf-8")


def deck_cards(conn: sqlite3.Connection, deck_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT c.*, p.number, d.filename FROM cards c "
        "LEFT JOIN pages p ON p.id = c.page_id LEFT JOIN documents d ON d.id = p.document_id "
        "WHERE c.deck_id = ? ORDER BY c.id",
        (deck_id,),
    ).fetchall()


def export_backup(conn: sqlite3.Connection, deck: sqlite3.Row, files_dir: Path, commit: str) -> bytes:
    """ZIP mit deck.json und den Folienbildern der Karten."""
    cards = deck_cards(conn, deck["id"])
    reviews: dict[int, list] = {}
    for r in conn.execute(
        "SELECT r.* FROM reviews r JOIN cards c ON c.id = r.card_id WHERE c.deck_id = ? ORDER BY r.id", (deck["id"],)
    ):
        reviews.setdefault(r["card_id"], []).append({
            "rating": r["rating"], "was_new": bool(r["was_new"]), "reviewed_at": r["reviewed_at"],
            "duration_ms": r["duration_ms"],
        })
    page_ids = sorted({c["page_id"] for c in cards if c["page_id"]})
    pages = {}
    if page_ids:
        marks = ",".join("?" * len(page_ids))
        for p in conn.execute(
            f"SELECT p.*, d.filename, d.user_id FROM pages p JOIN documents d ON d.id = p.document_id "
            f"WHERE p.id IN ({marks})",
            page_ids,
        ):
            pages[p["id"]] = p

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        page_list = []
        for pid, p in pages.items():
            key = f"p{pid}"
            image = pdf.page_image(files_dir / str(p["user_id"]) / str(p["document_id"]), p["number"])
            entry = None
            if image.exists():
                entry = f"pages/{key}.jpg"
                # JPEG ist schon komprimiert.
                zf.write(image, entry, compress_type=zipfile.ZIP_STORED)
            page_list.append({
                "key": key, "document": p["filename"], "number": p["number"], "text": p["text"],
                "width": p["width"], "height": p["height"], "image": entry,
            })
        data = {
            "format": BACKUP_FORMAT,
            "version": BACKUP_VERSION,
            "exported": db.now(),
            "app_commit": commit,
            "deck": {"name": deck["name"], "instructions": deck["instructions"], "exam_date": deck["exam_date"],
                     "new_per_day": deck["new_per_day"]},
            "pages": page_list,
            "cards": [{
                "type": c["type"], "front": c["front"], "back": c["back"],
                "page": f"p{c['page_id']}" if c["page_id"] in pages else None,
                "fsrs": json.loads(c["fsrs"]), "due": c["due"], "reps": c["reps"], "lapses": c["lapses"],
                "suspended": bool(c["suspended"]),
                "created": c["created"], "updated": c["updated"], "reviews": reviews.get(c["id"], []),
            } for c in cards],
        }
        zf.writestr("deck.json", json.dumps(data, ensure_ascii=False, indent=1))
    return buf.getvalue()


# --- Import aus Tabellen --------------------------------------------------------


@dataclass
class Parsed:
    delimiter: str
    header: bool
    columns: list[str]
    front: int
    back: int
    type_col: int | None
    cards: list[ai.CardDraft] = field(default_factory=list)
    rows: int = 0
    no_back: int = 0


def html_to_text(value: str) -> str:
    """Anki-Felder sind HTML: Zeilenumbrüche, Fett und Code bleiben erhalten."""
    if "<" not in value and "&" not in value:
        return value
    value = re.sub(r"<br\s*/?>|</div>|</p>|</li>", "\n", value, flags=re.I)
    value = re.sub(r"</?(b|strong)>", "**", value, flags=re.I)
    value = re.sub(r"</?code>", "`", value, flags=re.I)
    value = _TAG.sub("", value)
    value = html.unescape(value).replace("\xa0", " ")
    return "\n".join(line.strip() for line in value.splitlines() if line.strip())


def split_anki_cloze(text: str) -> list[str]:
    """Anki-Lückentext mit c1, c2 …: pro Nummer eine Karte mit einer Lücke in
    Studynator-Schreibweise, die übrigen Lücken als Klartext."""
    numbers = sorted({int(n) for n, _ in _ANKI_CLOZE.findall(text)})
    out = []
    for number in numbers:
        def swap(match, number=number):
            word = match.group(2)
            return "{{" + word + "}}" if int(match.group(1)) == number else word
        out.append(_ANKI_CLOZE.sub(swap, text))
    return out


def _bullet(line: str) -> tuple[int, str]:
    """Einrückung und Inhalt einer Zeile ohne Aufzählungszeichen."""
    stripped = line.lstrip(" \t")
    indent = len(line) - len(stripped)
    for mark in ("- ", "* ", "• "):
        if stripped.startswith(mark):
            stripped = stripped[len(mark):]
            break
    return indent, stripped.strip()


def looks_like_remnote(text: str) -> bool:
    sample = [line for line in text.splitlines()[:200] if line.strip() and not line.startswith("#")]
    if not sample or any("\t" in line for line in sample):
        return False
    hits = 0
    for line in sample:
        _, content = _bullet(line)
        if _ANKI_CLOZE.search(content):
            continue
        if _REMNOTE_MULTI.search(content) or _REMNOTE_INLINE.match(content):
            hits += 1
    return hits > 0


def split_gaps(text: str) -> list[str]:
    """RemNote macht aus jeder {{Lücke}} einer Zeile eine eigene Karte; die
    übrigen Lücken bleiben dabei Klartext."""
    gaps = list(_GAP.finditer(text))
    out = []
    for keep in range(len(gaps)):
        parts, last = [], 0
        for index, match in enumerate(gaps):
            parts.append(text[last:match.start()])
            parts.append(match.group(0) if index == keep else match.group(1))
            last = match.end()
        parts.append(text[last:])
        out.append("".join(parts))
    return out


def parse_remnote(text: str, result: "Parsed") -> None:
    lines = [line for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")]
    parsed = [_bullet(line) for line in lines]
    used: set[int] = set()

    def children(index: int) -> list[str]:
        indent = parsed[index][0]
        out = []
        for j in range(index + 1, len(parsed)):
            if parsed[j][0] <= indent:
                break
            used.add(j)
            out.append(parsed[j][1])
        return out

    for i, (_, content) in enumerate(parsed):
        if i in used or not content:
            continue
        result.rows += 1
        content = _REMNOTE_HINT.sub("", content).strip()
        multi = _REMNOTE_MULTI.search(content)
        if multi:
            front, marker = content[:multi.start()].strip(), multi.group(1)
            back = "\n".join(children(i))
            if not front or not back:
                result.no_back += 1
                continue
            if marker.startswith("<<"):
                front, back = back, front
            result.cards.append(ai.CardDraft(0, "basic", front, back))
            if marker.startswith("<>"):
                result.cards.append(ai.CardDraft(0, "basic", back, front))
            continue
        if _ANKI_CLOZE.search(content):
            extra = "\n".join(children(i))
            result.cards += [ai.CardDraft(0, "cloze", gap, extra) for gap in split_anki_cloze(content)]
            continue
        match = _REMNOTE_INLINE.match(content)
        if match:
            left, marker, right = (match.group(1), match.group(2), match.group(3)) if match.group(2) else (
                match.group(4), match.group(5), match.group(6))
            left, right = left.strip(), right.strip()
            if marker == ">-":
                continue
            if not right:
                result.no_back += 1
                continue
            if marker == "<<":
                left, right = right, left
            result.cards.append(ai.CardDraft(0, "basic", left, right))
            if marker == "<>":
                result.cards.append(ai.CardDraft(0, "basic", right, left))
            continue
        if _GAP.search(content):
            extra = "\n".join(children(i))
            result.cards += [ai.CardDraft(0, "cloze", gap, extra) for gap in split_gaps(content)]
        # Andere Zeilen sind Überschriften oder Notizen ohne Karte.


def _detect_delimiter(text: str) -> str:
    sample = [line for line in text.splitlines()[:30] if line.strip() and not line.startswith("#")]
    if any("\t" in line for line in sample):
        return "tab"
    semis = sum(line.count(";") for line in sample)
    commas = sum(line.count(",") for line in sample)
    return "semicolon" if semis >= commas and semis else "comma"


def _read_rows(text: str, delimiter: str) -> list[list[str]]:
    lines = "\n".join(line for line in text.splitlines() if not line.startswith("#"))
    if delimiter == "tab":
        # Quizlet und Anki setzen bei Tab keine Anführungszeichen.
        return [line.split("\t") for line in lines.splitlines() if line.strip()]
    reader = csv.reader(io.StringIO(lines), delimiter=DELIMITERS[delimiter])
    return [row for row in reader if any(cell.strip() for cell in row)]


def parse_table(text: str, delimiter: str = "auto", header: str = "auto",
                front: int | None = None, back: int | None = None) -> Parsed:
    text = text.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    if delimiter == "remnote" or (delimiter not in DELIMITERS and looks_like_remnote(text)):
        result = Parsed("remnote", False, [], 0, 0, None)
        parse_remnote(text, result)
        result.cards = result.cards[:MAX_IMPORT_CARDS]
        return result
    if delimiter not in DELIMITERS:
        delimiter = _detect_delimiter(text)
    rows = _read_rows(text, delimiter)
    width = max((len(r) for r in rows), default=0)
    first = [c.strip().lower() for c in rows[0]] if rows else []
    known = (FRONT_NAMES | BACK_NAMES | TYPE_NAMES)
    has_header = header == "yes" or (header == "auto" and any(c in known for c in first))
    names = [rows[0][i].strip() if has_header and i < len(rows[0]) else f"Spalte {i + 1}" for i in range(width)]
    type_col = next((i for i, c in enumerate(first) if c in TYPE_NAMES), None) if has_header else None
    if front is None or not 0 <= front < width:
        front = next((i for i, c in enumerate(first) if c in FRONT_NAMES), 0) if has_header else 0
    if back is None or not 0 <= back < width:
        back = next((i for i, c in enumerate(first) if c in BACK_NAMES), 1 if width > 1 else 0) if has_header else 1
    result = Parsed(delimiter, has_header, names, front, back, type_col)
    ours = has_header and [c.strip() for c in rows[0][:3]] == CSV_HEADER[:3]

    def cell(row, index):
        value = row[index].strip() if index is not None and index < len(row) else ""
        # Zurück aus der Excel-Entschärfung des eigenen Exports.
        if ours and value[:1] == "'" and value[1:2] in ("=", "+", "-", "@"):
            value = value[1:]
        return html_to_text(value)

    for row in rows[1:] if has_header else rows:
        result.rows += 1
        f = cell(row, front)
        b = cell(row, back) if back != front else ""
        if not f:
            continue
        kind = TYPE_VALUES.get(cell(row, type_col).lower()) if type_col is not None else None
        if _ANKI_CLOZE.search(f):
            for gap in split_anki_cloze(f):
                result.cards.append(ai.CardDraft(0, "cloze", gap, b))
            continue
        if kind is None:
            kind = "cloze" if "{{" in f and "}}" in f else "basic"
        if kind == "basic" and not b:
            result.no_back += 1
            continue
        result.cards.append(ai.CardDraft(0, kind, f, b))
        if len(result.cards) >= MAX_IMPORT_CARDS:
            break
    return result


def _norm(text: str) -> str:
    return " ".join(text.lower().split())


def split_duplicates(cards: list[ai.CardDraft], existing_fronts: list[str]) -> tuple[list[ai.CardDraft], int]:
    """Karten, deren Vorderseite es im Kurs oder weiter oben im Import schon gibt,
    werden übersprungen."""
    seen = {_norm(f) for f in existing_fronts}
    fresh, dupes = [], 0
    for card in cards:
        key = _norm(card.front)
        if key in seen:
            dupes += 1
            continue
        seen.add(key)
        fresh.append(card)
    return fresh, dupes


# --- Import einer Sicherung -----------------------------------------------------


def _iso(value) -> str | None:
    try:
        return datetime.fromisoformat(str(value)).astimezone(timezone.utc).isoformat(timespec="seconds")
    except (TypeError, ValueError):
        return None


def read_backup(raw: bytes) -> tuple[dict, dict[str, bytes]]:
    """Prüft das ZIP und gibt deck.json und die Bilder zurück; entpackt nichts
    ins Dateisystem."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile as exc:
        raise ExchangeError("Das ist keine ZIP-Datei.") from exc
    with zf:
        infos = zf.infolist()
        if len(infos) > MAX_ZIP_ENTRIES or sum(i.file_size for i in infos) > MAX_ZIP_TOTAL:
            raise ExchangeError("Die Sicherung ist zu gross.")
        names = {i.filename: i for i in infos}
        info = names.get("deck.json")
        if info is None:
            raise ExchangeError("In der ZIP-Datei fehlt deck.json; ist das eine Studynator-Sicherung?")
        if info.file_size > MAX_JSON_BYTES:
            raise ExchangeError("deck.json ist zu gross.")
        try:
            data = json.loads(zf.read(info).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ExchangeError("deck.json ist kein gültiges JSON.") from exc
        if not isinstance(data, dict) or data.get("format") != BACKUP_FORMAT:
            raise ExchangeError("Das ist keine Studynator-Sicherung.")
        if data.get("version") != BACKUP_VERSION:
            raise ExchangeError(f"Sicherung in Version {data.get('version')}; diese App liest Version {BACKUP_VERSION}.")
        images = {}
        for page in data.get("pages") or []:
            entry = page.get("image") if isinstance(page, dict) else None
            if not isinstance(entry, str) or not re.fullmatch(r"pages/p\d+\.jpg", entry) or entry not in names:
                continue
            if names[entry].file_size > MAX_IMAGE_BYTES:
                continue
            blob = zf.read(names[entry])
            if blob[:2] == b"\xff\xd8":
                images[entry] = blob
    return data, images


def import_backup(conn: sqlite3.Connection, user_id: int, files_dir: Path, raw: bytes) -> tuple[int, int]:
    """Legt aus einer Sicherung einen neuen Kurs an. Gibt (Kurs-ID, Karten) zurück."""
    data, images = read_backup(raw)
    deck_data = data.get("deck") if isinstance(data.get("deck"), dict) else {}
    name = str(deck_data.get("name") or "Importierter Kurs").strip()[:110]
    if conn.execute("SELECT 1 FROM decks WHERE user_id = ? AND name = ?", (user_id, name)).fetchone():
        name += " (Import)"
    instructions = str(deck_data.get("instructions") or "")[:ai.MAX_INSTRUCTIONS]
    # Planung seit 03.11.2026; ältere Sicherungen haben sie nicht.
    exam = plan.parse_exam(deck_data.get("exam_date") if isinstance(deck_data.get("exam_date"), str) else None)
    new_per_day = deck_data.get("new_per_day")
    if not (isinstance(new_per_day, int) and not isinstance(new_per_day, bool)
            and 0 <= new_per_day <= plan.MAX_NEW_PER_DAY):
        new_per_day = None
    now = db.now()
    deck_id = conn.execute(
        "INSERT INTO decks (user_id, name, instructions, exam_date, new_per_day, created) VALUES (?, ?, ?, ?, ?, ?)",
        (user_id, name, instructions, exam.isoformat() if exam else None, new_per_day, now),
    ).lastrowid

    # Seiten nach Dokument gruppieren; jedes Dokument bekommt einen Eintrag
    # ohne Original-PDF, nur mit den Seiten, auf die Karten zeigen.
    page_ids: dict[str, int] = {}
    by_document: dict[str, list[dict]] = {}
    for page in data.get("pages") or []:
        if isinstance(page, dict) and isinstance(page.get("number"), int) and isinstance(page.get("key"), str):
            by_document.setdefault(str(page.get("document") or "Dokument")[:180], []).append(page)
    written: list[Path] = []
    try:
        for filename, pages in by_document.items():
            doc_id = conn.execute(
                "INSERT INTO documents (user_id, deck_id, filename, pages, status, progress, created) "
                "VALUES (?, ?, ?, ?, 'done', ?, ?)",
                (user_id, deck_id, filename, max(p["number"] for p in pages), max(p["number"] for p in pages), now),
            ).lastrowid
            folder = files_dir / str(user_id) / str(doc_id)
            for page in pages:
                if conn.execute("SELECT 1 FROM pages WHERE document_id = ? AND number = ?",
                                (doc_id, page["number"])).fetchone():
                    continue
                page_ids[page["key"]] = conn.execute(
                    "INSERT INTO pages (document_id, number, text, has_text, width, height) VALUES (?, ?, ?, ?, ?, ?)",
                    (doc_id, page["number"], str(page.get("text") or ""), int(bool(page.get("text"))),
                     int(page.get("width") or 1280), int(page.get("height") or 720)),
                ).lastrowid
                blob = images.get(page.get("image") or "")
                if blob:
                    folder.mkdir(parents=True, exist_ok=True)
                    target = pdf.page_image(folder, page["number"])
                    target.write_bytes(blob)
                    written.append(target)

        count = 0
        for card in (data.get("cards") or [])[:MAX_IMPORT_CARDS]:
            if not isinstance(card, dict):
                continue
            kind = card.get("type") if card.get("type") in ai.CARD_TYPES else "basic"
            front, back = str(card.get("front") or "").strip(), str(card.get("back") or "").strip()
            if not front or (kind == "basic" and not back):
                continue
            try:
                state = srs.from_json(json.dumps(card.get("fsrs")))
            except Exception:  # unbrauchbarer Lernstand: als neue Karte
                state = srs.new_card()
            reps = card.get("reps") if isinstance(card.get("reps"), int) and card.get("reps") >= 0 else 0
            lapses = card.get("lapses") if isinstance(card.get("lapses"), int) and card.get("lapses") >= 0 else 0
            due = _iso(card.get("due")) or state.due.astimezone(timezone.utc).isoformat(timespec="seconds")
            # «suspended» gibt es seit 03.11.2026; ältere Sicherungen haben es nicht.
            card_id = conn.execute(
                "INSERT INTO cards (user_id, deck_id, page_id, type, front, back, fsrs, due, reps, lapses, suspended,"
                " created, updated) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (user_id, deck_id, page_ids.get(card.get("page") or ""), kind, front, back, srs.to_json(state), due,
                 reps, lapses, int(card.get("suspended") is True),
                 _iso(card.get("created")) or now, _iso(card.get("updated")) or now),
            ).lastrowid
            for review in card.get("reviews") or []:
                if not isinstance(review, dict) or review.get("rating") not in (1, 2, 3, 4):
                    continue
                at = _iso(review.get("reviewed_at"))
                if not at:
                    continue
                duration = review.get("duration_ms") if isinstance(review.get("duration_ms"), int) else None
                conn.execute(
                    "INSERT INTO reviews (card_id, user_id, rating, was_new, reviewed_at, duration_ms) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (card_id, user_id, review["rating"], int(bool(review.get("was_new"))), at, duration),
                )
            count += 1
        conn.commit()
    except Exception:
        conn.rollback()
        for path in written:
            path.unlink(missing_ok=True)
        raise
    return deck_id, count
