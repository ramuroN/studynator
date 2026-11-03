"""PNG-Icons für den Home-Bildschirm aus study/static/icon.svg.

Mit der venv aus tools/dev.ps1 aus dem Hauptordner:

    python tools/icons.py

Die Icons bekommen den hellen Hintergrund der App, weil iOS Transparenz schwarz
füllt; die «maskable»-Variante ist kleiner, damit Android sie rund oder eckig
zuschneiden kann, ohne das Motiv anzuschneiden.
"""

import re
from pathlib import Path

import pymupdf

STATIC = Path(__file__).resolve().parent.parent / "study" / "static"
BACKGROUND = "#f6f5f1"

# (Datei, Kantenlänge, Massstab des Motivs)
ICONS = [
    ("icon-192.png", 192, 1.0),
    ("icon-512.png", 512, 1.0),
    ("icon-maskable-512.png", 512, 0.78),
    ("apple-touch-icon.png", 180, 0.92),
]


def main() -> None:
    source = (STATIC / "icon.svg").read_text(encoding="utf-8")
    inner = re.search(r"<svg[^>]*>(.*)</svg>", source, re.S).group(1)
    for name, size, scale in ICONS:
        svg = (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{size}" height="{size}" viewBox="0 0 64 64">'
            f'<rect width="64" height="64" fill="{BACKGROUND}"/>'
            f'<g transform="translate(32 32) scale({scale}) translate(-33 -32)">{inner}</g></svg>'
        )
        doc = pymupdf.open(stream=svg.encode(), filetype="svg")
        pix = doc[0].get_pixmap(alpha=False)
        pix.save(STATIC / name)
        doc.close()
        print(name, pix.width, "x", pix.height)


if __name__ == "__main__":
    main()
