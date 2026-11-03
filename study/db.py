"""SQLite: Schema, Verbindungen, Snapshots."""

import sqlite3
import time
from datetime import date, datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    created TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS decks (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    name TEXT NOT NULL,
    created TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    deck_id INTEGER NOT NULL REFERENCES decks(id) ON DELETE CASCADE,
    filename TEXT NOT NULL,
    pages INTEGER NOT NULL DEFAULT 0,
    -- uploaded, rendering, rendered, generating, done, failed
    status TEXT NOT NULL,
    error TEXT,
    estimate_usd REAL,
    cost_usd REAL NOT NULL DEFAULT 0,
    model TEXT,
    -- angefragter Seitenbereich und letzte fertige Seite der Vorschläge
    gen_first INTEGER,
    gen_last INTEGER,
    progress INTEGER NOT NULL DEFAULT 0,
    created TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pages (
    id INTEGER PRIMARY KEY,
    document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    number INTEGER NOT NULL,
    text TEXT NOT NULL,
    has_text INTEGER NOT NULL,
    width INTEGER NOT NULL,
    height INTEGER NOT NULL,
    UNIQUE (document_id, number)
);
CREATE TABLE IF NOT EXISTS suggestions (
    id INTEGER PRIMARY KEY,
    document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    page_id INTEGER REFERENCES pages(id) ON DELETE CASCADE,
    type TEXT NOT NULL,
    front TEXT NOT NULL,
    back TEXT NOT NULL,
    -- open, accepted, rejected
    status TEXT NOT NULL DEFAULT 'open',
    similar_to TEXT,
    created TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cards (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    deck_id INTEGER NOT NULL REFERENCES decks(id) ON DELETE CASCADE,
    page_id INTEGER REFERENCES pages(id) ON DELETE SET NULL,
    type TEXT NOT NULL,
    front TEXT NOT NULL,
    back TEXT NOT NULL,
    fsrs TEXT NOT NULL,
    due TEXT NOT NULL,
    reps INTEGER NOT NULL DEFAULT 0,
    lapses INTEGER NOT NULL DEFAULT 0,
    created TEXT NOT NULL,
    updated TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS cards_due ON cards (user_id, due);
CREATE TABLE IF NOT EXISTS reviews (
    id INTEGER PRIMARY KEY,
    card_id INTEGER NOT NULL REFERENCES cards(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id),
    rating INTEGER NOT NULL,
    was_new INTEGER NOT NULL,
    reviewed_at TEXT NOT NULL,
    duration_ms INTEGER
);
CREATE INDEX IF NOT EXISTS reviews_user ON reviews (user_id, reviewed_at);
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,
    document_id INTEGER REFERENCES documents(id) ON DELETE CASCADE,
    params TEXT NOT NULL DEFAULT '{}',
    -- queued, running, done, failed
    status TEXT NOT NULL DEFAULT 'queued',
    attempts INTEGER NOT NULL DEFAULT 0,
    run_after TEXT NOT NULL,
    error TEXT,
    created TEXT NOT NULL,
    finished TEXT
);
CREATE TABLE IF NOT EXISTS usage (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    document_id INTEGER REFERENCES documents(id) ON DELETE SET NULL,
    model TEXT NOT NULL,
    input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    cost_usd REAL NOT NULL,
    created TEXT NOT NULL
);
"""


# Änderungen am Schema nach dem ersten Stand: Index + 1 ist die Version
# (PRAGMA user_version). Nur anhängen, nie umschreiben.
MIGRATIONS = [
    # 1, 02.11.2026: Anweisungen pro Kurs; Originalvorschlag und Entscheidzeit,
    # damit die KI aus den Korrekturen lernt.
    [
        "ALTER TABLE decks ADD COLUMN instructions TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE suggestions ADD COLUMN orig_type TEXT",
        "ALTER TABLE suggestions ADD COLUMN orig_front TEXT",
        "ALTER TABLE suggestions ADD COLUMN orig_back TEXT",
        "ALTER TABLE suggestions ADD COLUMN decided TEXT",
    ],
    # 2, 03.11.2026: welches Modell einen Vorschlag geschrieben hat (Modellwahl
    # pro Auftrag, Vergleich).
    [
        "ALTER TABLE suggestions ADD COLUMN model TEXT",
    ],
    # 3, 03.11.2026: Karten aussetzen; Lernstand vor jeder Bewertung, damit sie
    # sich zurücknehmen lässt.
    [
        "ALTER TABLE cards ADD COLUMN suspended INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE reviews ADD COLUMN prev TEXT",
    ],
    # 4, 03.11.2026: Farben pro Konto (Akzent als #rrggbb, Hintergrund als
    # Helligkeit 0 = schwarz bis 100 = weiss); NULL heisst Standard.
    [
        "ALTER TABLE users ADD COLUMN accent TEXT",
        "ALTER TABLE users ADD COLUMN background INTEGER",
    ],
    # 5, 03.11.2026: Planung pro Kurs; NULL heisst ohne Prüfung bzw. Standard
    # aus STUDY_NEW_PER_DAY.
    [
        "ALTER TABLE decks ADD COLUMN exam_date TEXT",
        "ALTER TABLE decks ADD COLUMN new_per_day INTEGER",
    ],
]


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=15, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 15000")
    return conn


def init(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(path)
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        for number, statements in enumerate(MIGRATIONS, start=1):
            if number <= version:
                continue
            # Ausdrücklich in einer Transaktion: sqlite3 öffnet vor DDL keine selbst.
            conn.execute("BEGIN")
            try:
                for statement in statements:
                    conn.execute(statement)
                conn.execute(f"PRAGMA user_version = {number}")
                conn.commit()
            except Exception:
                conn.rollback()
                raise
    finally:
        conn.close()


def user_id(conn: sqlite3.Connection, name: str) -> int:
    row = conn.execute("SELECT id FROM users WHERE name = ?", (name,)).fetchone()
    if row:
        return row["id"]
    cur = conn.execute("INSERT INTO users (name, created) VALUES (?, ?)", (name, now()))
    conn.commit()
    return cur.lastrowid


def month_spent(conn: sqlite3.Connection) -> float:
    """Ausgaben im laufenden Kalendermonat (UTC), über alle Konten: das Limit
    gilt für den einen API-Key."""
    start = datetime.now(timezone.utc).strftime("%Y-%m-01")
    row = conn.execute("SELECT COALESCE(SUM(cost_usd), 0) AS s FROM usage WHERE created >= ?", (start,)).fetchone()
    return float(row["s"])


def changed_at(path: Path) -> float:
    """Letzte Änderung der Datenbank; im WAL-Modus landen Schreibzugriffe zuerst
    in der -wal-Datei."""
    times = [path.stat().st_mtime]
    wal = path.with_name(path.name + "-wal")
    if wal.exists():
        times.append(wal.stat().st_mtime)
    return max(times)


def is_wal_file(path: Path) -> bool:
    """SQLite-Kopfzeile: Byte 18 ist 2 im WAL-Modus, 1 sonst."""
    with path.open("rb") as fh:
        header = fh.read(20)
    return len(header) == 20 and header[18] == 2


def remove_sidecars(path: Path) -> None:
    """-wal und -shm einer Datenbankdatei: Eine liegengebliebene -wal-Datei
    würde SQLite sonst auf eine neue Datei gleichen Namens anwenden."""
    for suffix in ("-wal", "-shm", "-journal"):
        path.with_name(path.name + suffix).unlink(missing_ok=True)


def snapshot(path: Path, target_dir: Path, keep: int = 7, refresh: float = 3600) -> Path | None:
    """Tageskopie der Datenbank für den NAS-Spiegel, eine Datei pro Tag; die
    neuesten `keep` bleiben. Die Datei des Tages wird erneuert, wenn sich die
    Datenbank seither geändert hat und die Kopie mindestens `refresh` Sekunden
    alt ist; so ist sie nie mehr als eine Stunde hinter dem Stand. Gibt die
    geschriebene Datei zurück oder None.

    Die Kopie ist eine gewöhnliche SQLite-Datei ohne WAL-Modus: Wer sie öffnet,
    legt keine -wal/-shm daneben an. Kopien im WAL-Modus (bis 92de274) werden
    sofort neu geschrieben."""
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"study-{date.today().isoformat()}.sqlite"
    if target.exists() and not is_wal_file(target):
        made = target.stat().st_mtime
        if time.time() - made < refresh or changed_at(path) <= made:
            return None
    tmp = target.with_suffix(".tmp")
    remove_sidecars(tmp)
    src = connect(path)
    dst = sqlite3.connect(tmp)
    try:
        src.backup(dst)
        # backup() übernimmt den WAL-Modus der Quelle mit der Kopfzeile.
        dst.execute("PRAGMA journal_mode = DELETE")
    finally:
        dst.close()
        src.close()
    remove_sidecars(target)
    tmp.replace(target)
    for old in sorted(target_dir.glob("study-*.sqlite"))[:-keep]:
        old.unlink()
        remove_sidecars(old)
    return target
