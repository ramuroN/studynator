# Studynator

Selfhosted "presentation-to-flashcards" App. Folien als PDF hochladen, die AI API schlägt pro Folie Karteikarten vor, Vorschläge werden geprüft und mit FSRS gelernt.

Gebaut mit FastAPI, Jinja2, SQLite, PyMuPDF und [py-fsrs](https://github.com/open-spaced-repetition/py-fsrs); wenig JavaScript, kein Build-Schritt.

## Lokal starten

Python 3.13. Unter Windows:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\tools\dev.ps1
```

Öffnet `http://127.0.0.1:8790` als Benutzer `dev` (anderes Konto: `-User`); venv und Daten liegen unter `%LOCALAPPDATA%\study`. Pro Datenordner läuft nur ein Server (Sperrdatei `study.lock`); ein zweiter braucht `-DataDir` und `-Port`. `-SkipInstall` überspringt die Paketprüfung, `-NoWorker` startet ohne Jobs und Tageskopie (z. B. um eine wiederhergestellte Kopie anzusehen). Ohne Schalter erzeugt der Anbieter `fake` Vorschläge aus der ersten Zeile jeder Seite, ohne Netz und ohne Kosten. Mit `-Anthropic` (und `ANTHROPIC_API_KEY` in der Sitzung) gehen die Seiten an die Claude-API. Ein Beispiel-PDF erzeugt `tools/sample_pdf.py`.

Unter Linux oder macOS dasselbe von Hand:

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
STUDY_DATA_DIR=.local-data STUDY_DEV_USER=dev STUDY_AI_PROVIDER=fake .venv/bin/uvicorn study.app:app --port 8790
```

## Tests

```bash
python -m unittest discover -s tests -t tests
```

## Einstellungen

| Variable | Standard | Zweck |
| --- | --- | --- |
| `STUDY_DATA_DIR` | `/data` | SQLite, Seitenbilder, Tageskopien |
| `PROXY_SECRET` | leer | Wenn gesetzt, gilt nur eine Anfrage mit gleichem `X-Proxy-Secret` (ausser `/api/health`) |
| `USER_HEADER` | `X-authentik-username` | Header mit dem Benutzernamen |
| `STUDY_DEV_USER` | leer | Nur lokal: Benutzer, wenn der Header fehlt; wird ignoriert, sobald `PROXY_SECRET` gesetzt ist |
| `STUDY_AI_PROVIDER` | `anthropic` | `anthropic` oder `fake` |
| `ANTHROPIC_API_KEY` | – | Schlüssel für die Claude-API |
| `STUDY_MODEL` | `claude-opus-5-5` | vorausgewähltes Modell; beim Erzeugen wählbar |
| `STUDY_EFFORT` | `medium` | vorausgewählter Denkaufwand; beim Erzeugen wählbar (niedrig, mittel, hoch) |
| `STUDY_MONTHLY_BUDGET_USD` | `10` | Deckel für alle Konten zusammen pro Kalendermonat |
| `STUDY_PAGES_PER_REQUEST` | `8` | Seiten pro API-Anfrage |
| `STUDY_MAX_UPLOAD_MB` / `STUDY_MAX_PAGES` | `100` / `400` | Grenzen pro PDF |
| `STUDY_NEW_PER_DAY` | `20` | neue Karten pro Person und Tag |
| `STUDY_TZ` | `Europe/Zurich` | Beginn des Lerntags |

## Lernen und Prüfen

- **Auf dem Handy:** Im Browser «Zum Home-Bildschirm» ergibt eine eigene App ohne Browserleiste (Manifest `study/static/manifest.webmanifest`, Icons aus `tools/icons.py`). Beim Lernen liegen «Antwort zeigen» und die vier Bewertungen als Leiste unten; Antippen der Karte zeigt die Antwort.
- **Rückgängig** (Taste `Z`): nimmt die letzte Bewertung zurück, mehrmals Schritt für Schritt, solange sie höchstens eine Stunde alt ist. Dafür speichert jede Bewertung den Lernstand davor (`reviews.prev`); ältere und importierte Bewertungen lassen sich nicht zurücknehmen.
- **Aussetzen:** Die Karte behält ihren Lernstand, kommt aber nicht mehr dran, bis sie wieder aufgenommen wird (direkt nach dem Aussetzen oder auf der Kartenseite). In der Sicherung als `suspended`.
- Doppelt abgeschickte Bewertungen (zweimal getippt) zählen nur einmal; «Bearbeiten» beim Lernen führt nach dem Speichern zur selben Karte zurück.
- **Vorschläge prüfen:** Übernehmen und Verwerfen ohne Neuladen. Tasten: `J`/`K` nächster und vorheriger Vorschlag, `A` übernehmen, `V` verwerfen, `E` bearbeiten; im Feld `Ctrl`+`Enter` übernimmt, `Esc` verlässt es.

## Farben

Das Paletten-Symbol in der App-Leiste öffnet den Farbwähler: Akzentfarbe als Punkt auf einem Verlauf (Farbton nach rechts, Helligkeit nach unten, Sättigung fest 65 %), Hintergrund als Punkt auf dem Verlauf von Weiss nach Schwarz. Die Vorschau gilt sofort, gespeichert wird beim Loslassen, pro Konto in `users.accent` (`#rrggbb`) und `users.background` (0 = schwarz bis 100 = weiss); leer heisst Standard bzw. hell/dunkel wie das System. Tastatur: Pfeiltasten, mit Umschalt in grossen Schritten.

Alle übrigen Farben leitet `study/static/theme.js` ab: Flächen, Linien, Text, gedämpfter Text, Akzent als Textfarbe (`--accent-ink`), Schrift auf Akzentflächen und die Signalfarben. Text hat auf Hintergrund und Flächen immer mindestens 4.5:1 Kontrast; bei mittlerem Grau gehen Text und Signalfarben dafür bis Weiss oder Schwarz. Geprüft am 03.11.2026 über 107 712 Kombinationen (Hintergrund 0 bis 100 und Standard, 24 Farbtöne × 22 Helligkeiten, System hell und dunkel).

## Vorschläge steuern

Auf der Kursseite stehen die «Anweisungen an die KI» (bis 2000 Zeichen). Jede Anfrage bekommt sie mit, dazu die jüngsten Entscheide im Kurs: was du vor dem Übernehmen geändert hast (vorher/nachher), was du unverändert übernommen und was du verworfen hast. Die Grundregeln für alle Kurse stehen in `SYSTEM_PROMPT` in `study/ai.py`.

Beim Erzeugen wählst du Modell (Opus 5.5, Sonnet 5.5, Haiku 5.5, Fable 5.1, Liste `MODELS` in `study/ai.py`) und Denkaufwand; die Auswahl zeigt die geschätzten Kosten für alle Seiten. Jeder Vorschlag merkt sich das Modell, das ihn geschrieben hat. Zum Vergleichen dasselbe PDF in zwei Kursen erzeugen, weil die KI im selben Kurs bestehende Karten meidet.

## Import und Export

Auf der Kursseite unter «Import und Export»:

- **Tabelle (CSV):** Strichpunkt, Überschrift, UTF-8 mit BOM; öffnet Excel direkt. Zellen, die mit `=`, `+`, `-` oder `@` beginnen, bekommen ein `'` davor; der eigene Import nimmt es wieder weg.
- **RemNote (Markdown):** RemNote-Textsyntax: `- Frage >> Antwort`, mehrzeilige Antworten als `>>>` mit Unterpunkten, Lücken `{{…}}` (Zusatz als Unterpunkt). In RemNote als Markdown importieren oder einfügen.
- **GoodNotes / Quizlet (TSV):** Frage Tab Antwort, keine Überschrift (so verlangt es GoodNotes für Study Sets), nur Text ohne `**` und Backticks; Lücken werden zu `[…]` vorne und dem Wort hinten. GoodNotes exportiert Study Sets nur im eigenen `.goodnotes`-Format, darum gibt es keinen Import aus GoodNotes.
- **Sicherung (ZIP):** `deck.json` (Format `studynator-deck`, Version 1) mit Anweisungen, Karten samt FSRS-Stand, Status «ausgesetzt» (seit 03.11.2026, fehlt in älteren Dateien) und Bewertungen, dazu die Folienbilder der Karten. Auf der Startseite («Sicherung einlesen») wird daraus ein neuer Kurs; Original-PDFs und offene Vorschläge sind nicht enthalten.
- **Import aus RemNote-Text** (Rems kopieren und einfügen oder RemNote-Markdown hochladen): erkannt werden `>>`, `==`, `::`, `;;` (vorwärts), `<<` (rückwärts), `<>` (beidseitig, zwei Karten), `>-` (ausgeschaltet, übersprungen), `>>>`/`<<<`/`<><` mit Unterpunkten, Lücken `{{…}}` (pro Lücke eine Karte, Hinweise `{({…})}` fallen weg). Quelle: RemNote-Hilfe «How to import flashcards from text», abgerufen 03.11.2026.
- **Import aus CSV, TSV oder eingefügtem Text** (z. B. Quizlet, Anki «Notes in Plain Text»): Vorschau mit Trennzeichen, Überschrift und Spaltenwahl; Doppelte (gleiche Vorderseite im Kurs) werden übersprungen; Anki-Lückentext `{{c1::…}}` wird pro Lücke eine Karte, HTML aus Anki wird Text. Importierte Karten beginnen als neu.

Grenzen: Dateien bis `STUDY_MAX_UPLOAD_MB`, eingefügter Text bis 1 MB, Sicherungen höchstens 20 000 Einträge und 1 GB entpackt; das ZIP wird nur gelesen, nie ins Dateisystem entpackt.

## Betrieb

Studynator hat keine eigene Anmeldung. Davor gehört ein Reverse-Proxy mit Anmeldung, z. B. Nginx Proxy Manager oder Traefik mit Authentik, Authelia oder oauth2-proxy. Der Proxy muss bei jeder Anfrage

- den angemeldeten Benutzernamen im Header `X-authentik-username` mitgeben (anderer Name: `USER_HEADER`) und einen gleichnamigen Header aus dem Browser überschreiben;
- den Header `X-Proxy-Secret` mit dem Wert von `PROXY_SECRET` setzen. Ohne ihn antwortet die App nur auf `/api/health`; so erreicht sie niemand am Proxy vorbei.

Mit Docker Compose:

```bash
cp .env.example .env              # PROXY_SECRET und ANTHROPIC_API_KEY eintragen
openssl rand -hex 32              # ein Wert für PROXY_SECRET
mkdir -p data && sudo chown 1000:1000 data
docker compose -f compose.example.yml up -d --build
```

Der Container läuft als UID 1000 ohne Capabilities und lauscht in diesem Beispiel nur auf `127.0.0.1:8000`; der Proxy leitet dorthin. Der Build führt die Tests aus und bricht ab, wenn einer fehlschlägt.

Alle Daten liegen in `data/`: `study.sqlite`, pro Dokument das Original-PDF und die Seitenbilder, dazu in `data/snapshots/` eine Tageskopie der Datenbank (stündlich erneuert, sieben Tage). Für eine Sicherung genügt dieser Ordner; zum Wiederherstellen den Container stoppen, `data/` zurücklegen und bei Zweifeln an `study.sqlite` die neueste Tageskopie nehmen.

Beim Erzeugen von Vorschlägen gehen die Folienbilder und ihr Text an die Claude-API von Anthropic; die Kosten laufen über den eigenen API-Schlüssel, `STUDY_MONTHLY_BUDGET_USD` setzt einen Deckel. Unterlagen, die nicht in die Cloud dürfen, nicht hochladen.

## Lizenz

MIT, siehe [LICENSE](LICENSE).
