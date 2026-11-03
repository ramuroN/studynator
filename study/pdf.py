"""PDF: Seiten als JPEG rendern und die Textebene auslesen."""

from dataclasses import dataclass
from pathlib import Path

import pymupdf

# Lange Kante der Seitenbilder. 1280 px reicht für Folien und hält ein Bild bei
# etwa 1200 Tokens.
TARGET_LONG_EDGE = 1280
JPEG_QUALITY = 80
# Weniger Zeichen gelten als «keine Textebene» (Scan, reine Grafik).
MIN_TEXT_CHARS = 20


class PdfError(Exception):
    pass


@dataclass
class RenderedPage:
    number: int
    text: str
    has_text: bool
    width: int
    height: int


def _private_use(ch: str) -> bool:
    # Symbolschriften (Wingdings, Symbol) liefern Aufzählungszeichen aus dem
    # privaten Unicode-Bereich; dafür hat keine normale Schrift ein Zeichen.
    return 0xE000 <= ord(ch) <= 0xF8FF


def clean_text(text: str) -> str:
    """Zeilen ohne überzählige Leerzeichen, Aufzählungszeichen als «•». Ein
    Aufzählungszeichen auf eigener Zeile kommt vor die nächste Zeile."""
    lines: list[str] = []
    bullet = False
    for raw in text.splitlines():
        line = "".join("• " if _private_use(ch) else ch for ch in raw)
        line = " ".join(line.split())
        if not line:
            continue
        if line == "•":
            bullet = True
            continue
        if bullet and not line.startswith("•"):
            line = "• " + line
        bullet = False
        lines.append(line)
    return "\n".join(lines)


def page_image(doc_dir: Path, number: int) -> Path:
    return doc_dir / f"page-{number:04d}.jpg"


def looks_like_pdf(head: bytes) -> bool:
    return head.lstrip()[:5] == b"%PDF-"


def render(pdf_path: Path, out_dir: Path, max_pages: int) -> list[RenderedPage]:
    try:
        doc = pymupdf.open(pdf_path)
    except Exception as exc:  # PyMuPDF wirft je nach Fehler verschiedene Typen
        raise PdfError("Die Datei lässt sich nicht als PDF öffnen.") from exc
    try:
        if doc.needs_pass:
            raise PdfError("Das PDF ist passwortgeschützt.")
        if doc.page_count == 0:
            raise PdfError("Das PDF hat keine Seiten.")
        if doc.page_count > max_pages:
            raise PdfError(f"Das PDF hat {doc.page_count} Seiten, erlaubt sind {max_pages}.")
        out_dir.mkdir(parents=True, exist_ok=True)
        pages = []
        for index, page in enumerate(doc, start=1):
            rect = page.rect
            long_edge = max(rect.width, rect.height) or 1
            zoom = TARGET_LONG_EDGE / long_edge
            pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
            pix.save(page_image(out_dir, index), jpg_quality=JPEG_QUALITY)
            text = clean_text(page.get_text("text"))
            pages.append(RenderedPage(index, text, len(text) >= MIN_TEXT_CHARS, pix.width, pix.height))
        return pages
    finally:
        doc.close()
