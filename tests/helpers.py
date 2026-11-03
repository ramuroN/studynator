"""Gemeinsame Hilfen: Umgebung pro Test, Beispiel-PDFs."""

import os
import shutil
import tempfile
import unittest
from pathlib import Path

import pymupdf

ENV_KEYS = [
    "STUDY_DATA_DIR", "PROXY_SECRET", "USER_HEADER", "STUDY_DEV_USER", "STUDY_AI_PROVIDER", "STUDY_MODEL",
    "STUDY_EFFORT", "STUDY_MONTHLY_BUDGET_USD", "STUDY_MAX_UPLOAD_MB", "STUDY_MAX_PAGES",
    "STUDY_PAGES_PER_REQUEST", "STUDY_NEW_PER_DAY", "STUDY_WORKER",
]


def make_pdf(path: Path, pages: list[str]) -> Path:
    doc = pymupdf.open()
    for text in pages:
        page = doc.new_page(width=960, height=540)
        page.insert_text((60, 80), text, fontsize=24)
    doc.save(path)
    doc.close()
    return path


class TempEnv(unittest.TestCase):
    """Eigener Datenordner und feste Einstellungen pro Test; nie echte Daten."""

    env: dict[str, str] = {}

    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in ENV_KEYS}
        self.tmp = Path(tempfile.mkdtemp(prefix="study-test-"))
        os.environ.update({
            "STUDY_DATA_DIR": str(self.tmp / "data"),
            "PROXY_SECRET": "",
            "STUDY_DEV_USER": "",
            "STUDY_AI_PROVIDER": "fake",
            "STUDY_WORKER": "0",
            "STUDY_PAGES_PER_REQUEST": "2",
            "STUDY_NEW_PER_DAY": "20",
            "STUDY_MONTHLY_BUDGET_USD": "10",
            **self.env,
        })

    def tearDown(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        shutil.rmtree(self.tmp, ignore_errors=True)
