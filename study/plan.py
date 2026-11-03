"""Planung pro Kurs (neue Karten pro Tag, Prüfungsdatum) und Statistik.

Alle Tage sind Kalendertage in STUDY_TZ; in der Datenbank stehen Zeiten als UTC."""

import json
import math
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

MAX_NEW_PER_DAY = 500
# Die letzten Tage vor der Prüfung bleiben für Wiederholungen frei.
REVIEW_DAYS_BEFORE_EXAM = 3
WEEKDAYS = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]
# fsrs.State.Review
REVIEW_STATE = 2


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def local_today(tz: str, now: datetime | None = None) -> date:
    return (now or utcnow()).astimezone(ZoneInfo(tz)).date()


def day_start(day: date, tz: str) -> str:
    """Mitternacht des Tages in tz, als UTC-ISO wie in der Datenbank."""
    return datetime.combine(day, time(), ZoneInfo(tz)).astimezone(timezone.utc).isoformat(timespec="seconds")


def local_day(iso: str, tz: str) -> date:
    return datetime.fromisoformat(iso).astimezone(ZoneInfo(tz)).date()


def day_label(day: date, today: date | None = None) -> str:
    """«Heute», «Morgen» oder «Mi 05.11.»."""
    if today is not None and day == today:
        return "Heute"
    if today is not None and day == today + timedelta(days=1):
        return "Morgen"
    return f"{WEEKDAYS[day.weekday()]} {day:%d.%m.}"


def parse_exam(value: str | None) -> date | None:
    try:
        return date.fromisoformat(value) if value else None
    except ValueError:
        return None


@dataclass
class DeckPlan:
    deck_id: int
    new: int  # neue Karten, die noch nie dran waren (ohne ausgesetzte)
    introduced: int  # heute zum ersten Mal gelernt
    base: int  # eigenes Limit des Kurses oder der Standard
    custom: bool  # eigenes Limit gesetzt
    exam: date | None
    days_left: int | None  # bis zur Prüfung; negativ, wenn sie vorbei ist
    pace: int  # nötig pro Tag bis zur Prüfung, 0 ohne Prüfung
    limit: int  # gilt heute: das höhere von base und pace
    left: int  # heute noch, höchstens so viele wie neue da sind
    finish: date | None  # bei diesem Tempo: letzter Tag mit neuen Karten
    target: date | None  # bis dann sollen alle neuen dran gewesen sein

    @property
    def exam_ahead(self) -> bool:
        return self.days_left is not None and self.days_left >= 1


def make_plan(deck_id: int, new: int, introduced: int, own_limit: int | None, default: int,
              exam: date | None, today: date) -> DeckPlan:
    base = own_limit if own_limit is not None else default
    days_left = (exam - today).days if exam else None
    # Neue Karten zu Tagesbeginn: was heute schon dran war, zählt zum Tag.
    start = new + introduced
    pace, target = 0, None
    if days_left is not None and days_left >= 1:
        days = max(1, days_left - REVIEW_DAYS_BEFORE_EXAM)
        target = today + timedelta(days=days - 1)
        pace = math.ceil(start / days)
    limit = min(MAX_NEW_PER_DAY, max(base, pace))
    left = max(0, min(new, limit - introduced))
    finish = today + timedelta(days=math.ceil(start / limit) - 1) if start and limit else None
    return DeckPlan(deck_id, new, introduced, base, own_limit is not None, exam, days_left, pace, limit, left,
                    finish, target)


def deck_plans(conn: sqlite3.Connection, user_id: int, default: int, tz: str,
               now: datetime | None = None) -> dict[int, DeckPlan]:
    today = local_today(tz, now)
    new = dict(conn.execute(
        "SELECT deck_id, COUNT(*) FROM cards WHERE user_id = ? AND suspended = 0 AND reps = 0 GROUP BY deck_id",
        (user_id,),
    ).fetchall())
    introduced = dict(conn.execute(
        "SELECT c.deck_id, COUNT(*) FROM reviews r JOIN cards c ON c.id = r.card_id "
        "WHERE r.user_id = ? AND r.was_new = 1 AND r.reviewed_at >= ? GROUP BY c.deck_id",
        (user_id, day_start(today, tz)),
    ).fetchall())
    plans = {}
    for deck in conn.execute("SELECT id, exam_date, new_per_day FROM decks WHERE user_id = ?", (user_id,)):
        plans[deck["id"]] = make_plan(deck["id"], new.get(deck["id"], 0), introduced.get(deck["id"], 0),
                                      deck["new_per_day"], default, parse_exam(deck["exam_date"]), today)
    return plans


def new_order(plans: dict[int, DeckPlan]) -> list[DeckPlan]:
    """Kurse mit neuen Karten für heute, die nächste Prüfung zuerst."""
    ready = [p for p in plans.values() if p.left > 0]
    return sorted(ready, key=lambda p: (not p.exam_ahead, p.days_left if p.exam_ahead else 0, p.deck_id))


# --- Statistik --------------------------------------------------------------------


def _deck_filter(deck_id: int | None, column: str = "deck_id") -> tuple[str, list]:
    return (f" AND {column} = ?", [deck_id]) if deck_id is not None else ("", [])


def forecast(conn: sqlite3.Connection, user_id: int, tz: str, deck_id: int | None = None, days: int = 7,
             now: datetime | None = None) -> list[tuple[date, int]]:
    """Fällige Wiederholungen pro Tag; Überfälliges zählt zu heute."""
    today = local_today(tz, now)
    extra, args = _deck_filter(deck_id)
    rows = conn.execute(
        "SELECT due FROM cards WHERE user_id = ? AND suspended = 0 AND reps > 0 AND due < ?" + extra,
        [user_id, day_start(today + timedelta(days=days), tz), *args],
    ).fetchall()
    counts = {today + timedelta(days=i): 0 for i in range(days)}
    for row in rows:
        counts[max(local_day(row["due"], tz), today)] += 1
    return list(counts.items())


def retention(conn: sqlite3.Connection, user_id: int, deck_id: int | None = None, days: int = 30,
              now: datetime | None = None) -> tuple[int, int]:
    """(gewusst, alle) bei Wiederholungen der letzten Tage: nur Karten, die schon
    im Wiederholungsmodus waren, nicht die ersten Lernschritte. Ohne
    gespeicherten Vorzustand (alte, importierte) zählt jede nicht neue."""
    extra, args = _deck_filter(deck_id, "c.deck_id")
    rows = conn.execute(
        "SELECT r.rating, r.was_new, r.prev FROM reviews r JOIN cards c ON c.id = r.card_id "
        "WHERE r.user_id = ? AND r.reviewed_at >= ?" + extra,
        [user_id, ((now or utcnow()) - timedelta(days=days)).isoformat(timespec="seconds"), *args],
    ).fetchall()
    known = total = 0
    for row in rows:
        if row["prev"]:
            try:
                if json.loads(json.loads(row["prev"])["fsrs"]).get("state") != REVIEW_STATE:
                    continue
            except (ValueError, KeyError, TypeError):
                continue
        elif row["was_new"]:
            continue
        total += 1
        known += row["rating"] > 1
    return known, total


def streak(conn: sqlite3.Connection, user_id: int, tz: str, now: datetime | None = None) -> int:
    """Tage in Folge mit mindestens einer Bewertung, bis heute; heute noch ohne
    zählt die Serie bis gestern."""
    today = local_today(tz, now)
    rows = conn.execute(
        "SELECT reviewed_at FROM reviews WHERE user_id = ? AND reviewed_at >= ?",
        (user_id, day_start(today - timedelta(days=400), tz)),
    ).fetchall()
    days = {local_day(row["reviewed_at"], tz) for row in rows}
    day = today if today in days else today - timedelta(days=1)
    count = 0
    while day in days:
        count += 1
        day -= timedelta(days=1)
    return count


def today_done(conn: sqlite3.Connection, user_id: int, tz: str, deck_id: int | None = None,
               now: datetime | None = None) -> tuple[int, int]:
    """(Bewertungen, Minuten) heute."""
    extra, args = _deck_filter(deck_id, "c.deck_id")
    row = conn.execute(
        "SELECT COUNT(*) AS n, COALESCE(SUM(r.duration_ms), 0) AS ms FROM reviews r JOIN cards c ON c.id = r.card_id "
        "WHERE r.user_id = ? AND r.reviewed_at >= ?" + extra,
        [user_id, day_start(local_today(tz, now), tz), *args],
    ).fetchone()
    return row["n"], round(row["ms"] / 60000)


@dataclass
class ForecastDay:
    day: date
    count: int
    label: str  # «Heute», «Morgen», «Mi 05.11.»
    short: str  # unter der Säule: «Heute», «Mi»
    labeled: bool  # Zahl direkt an der Säule: heute und der erste höchste Tag
    height: int  # Prozent der höchsten Säule


@dataclass
class Stats:
    forecast: list[ForecastDay]
    peak: int
    known: int
    reviewed: int
    streak: int
    done_today: int
    minutes_today: int

    @property
    def rate(self) -> int | None:
        """Trefferquote in Prozent; erst ab 10 Wiederholungen aussagekräftig."""
        return round(100 * self.known / self.reviewed) if self.reviewed >= 10 else None


def stats(conn: sqlite3.Connection, user_id: int, tz: str, deck_id: int | None = None,
          now: datetime | None = None) -> Stats:
    today = local_today(tz, now)
    raw = forecast(conn, user_id, tz, deck_id, now=now)
    peak = max(n for _, n in raw)
    peak_day = next((day for day, n in raw if n == peak), None) if peak else None
    days = [ForecastDay(day, n, day_label(day, today), "Heute" if day == today else WEEKDAYS[day.weekday()],
                        day == today or day == peak_day, round(100 * n / peak) if peak else 0)
            for day, n in raw]
    known, reviewed = retention(conn, user_id, deck_id, now=now)
    done, minutes = today_done(conn, user_id, tz, deck_id, now=now)
    return Stats(days, peak, known, reviewed, streak(conn, user_id, tz, now), done, minutes)
