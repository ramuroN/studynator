"""Erzeugt ein kleines Vorlesungs-PDF zum Ausprobieren.

    python tools/sample_pdf.py beispiel.pdf
"""

import sys

import pymupdf

SLIDES = [
    ("Zellbiologie I", "Vorlesung 3: Energiestoffwechsel"),
    ("Mitochondrien", "Ort der Zellatmung\nEigene DNA (mtDNA), mütterlich vererbt\nDoppelmembran: äussere und innere Membran mit Cristae"),
    ("Glykolyse", "Findet im Zytosol statt\nGlukose -> 2 Pyruvat\nNetto-Gewinn: 2 ATP und 2 NADH pro Glukose"),
    ("Michaelis-Menten-Gleichung", "v = Vmax * [S] / (Km + [S])\nKm: Substratkonzentration bei halbmaximaler Geschwindigkeit"),
    ("Literatur", "Alberts et al., Molecular Biology of the Cell"),
]


def main(target: str) -> None:
    doc = pymupdf.open()
    for title, body in SLIDES:
        page = doc.new_page(width=960, height=540)
        page.insert_text((60, 90), title, fontsize=34)
        page.draw_line((60, 110), (900, 110), color=(0.2, 0.42, 0.31), width=2)
        page.insert_text((60, 170), body, fontsize=22, lineheight=1.6)
    doc.save(target)
    print(f"{target}: {len(SLIDES)} Seiten")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "beispiel.pdf")
