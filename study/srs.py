"""Planung mit FSRS (Paket `fsrs`) und Anzeige der Karten."""

import html
import re
from datetime import datetime, timedelta, timezone

from fsrs import Card, Rating, Scheduler

RATINGS = {1: Rating.Again, 2: Rating.Hard, 3: Rating.Good, 4: Rating.Easy}
RATING_LABELS = {1: "Nochmal", 2: "Schwer", 3: "Gut", 4: "Einfach"}

_scheduler = Scheduler(desired_retention=0.9, enable_fuzzing=True)
# Ohne Zufallsstreuung, damit die Vorschau auf den Knöpfen beim Neuladen gleich bleibt.
_preview = Scheduler(desired_retention=0.9, enable_fuzzing=False)


def new_card() -> Card:
    return Card()


def to_json(card: Card) -> str:
    return card.to_json()


def from_json(raw: str) -> Card:
    return Card.from_json(raw)


def review(card: Card, rating: int, when: datetime | None = None) -> Card:
    updated, _log = _scheduler.review_card(card, RATINGS[rating], review_datetime=when or datetime.now(timezone.utc))
    return updated


def preview(card: Card, when: datetime | None = None) -> dict[int, str]:
    """Abstand bis zur nächsten Wiederholung je Bewertung, für die Knöpfe."""
    when = when or datetime.now(timezone.utc)
    out = {}
    for value, rating in RATINGS.items():
        updated, _log = _preview.review_card(Card.from_json(card.to_json()), rating, review_datetime=when)
        out[value] = human_delta(updated.due - when)
    return out


def human_delta(delta: timedelta) -> str:
    seconds = max(0, int(delta.total_seconds()))
    if seconds < 3600:
        return f"{max(1, round(seconds / 60))} min"
    if seconds < 86400:
        return f"{round(seconds / 3600)} h"
    days = seconds / 86400
    if days < 30:
        return f"{round(days)} T"
    if days < 365:
        return f"{round(days / 30)} Mt"
    return f"{days / 365:.1f} J"


_CLOZE = re.compile(r"\{\{(.+?)\}\}", re.S)
_BOLD = re.compile(r"\*\*(.+?)\*\*", re.S)
_CODE = re.compile(r"`([^`\n]+)`")


def _format(text: str) -> str:
    out = html.escape(text, quote=False)
    out = _CODE.sub(r"<code>\1</code>", out)
    out = _BOLD.sub(r"<strong>\1</strong>", out)
    return out.replace("\n", "<br>")


def render_front(kind: str, front: str) -> str:
    if kind == "cloze":
        return _CLOZE.sub('<span class="cloze">[…]</span>', _format(front))
    return _format(front)


def render_back(kind: str, front: str, back: str) -> str:
    if kind == "cloze":
        full = _CLOZE.sub(r'<mark class="cloze">\1</mark>', _format(front))
        return full + (f'<div class="extra">{_format(back)}</div>' if back.strip() else "")
    return _format(back)


def plain(kind: str, front: str) -> str:
    """Kurzform für Listen."""
    return _CLOZE.sub(r"[\1]", front) if kind == "cloze" else front
