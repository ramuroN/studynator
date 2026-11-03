"""Fragt /api/health im Container ab und gibt die Antwort aus; Exit 1 bei Fehler.
Für HEALTHCHECK im Dockerfile und tools/deploy.ps1 (ohne verschachtelte
Anführungszeichen über ssh):

    python -m study.healthcheck
"""

import sys
import urllib.request


def main() -> int:
    try:
        with urllib.request.urlopen("http://127.0.0.1:8000/api/health", timeout=4) as res:
            print(res.read().decode())
        return 0
    except OSError as exc:
        print(f"health: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
