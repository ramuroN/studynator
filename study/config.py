"""Einstellungen aus der Umgebung. Werte werden bei jedem Aufruf frisch gelesen,
damit Tests sie pro Fall setzen können."""

import os
from dataclasses import dataclass
from pathlib import Path


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    proxy_secret: str
    user_header: str
    dev_user: str
    ai_provider: str
    model: str
    effort: str
    monthly_budget_usd: float
    max_upload_mb: int
    max_pages: int
    pages_per_request: int
    new_per_day: int
    worker: bool
    tz: str
    commit: str

    @property
    def db_path(self) -> Path:
        return self.data_dir / "study.sqlite"

    @property
    def files_dir(self) -> Path:
        return self.data_dir / "files"

    @property
    def snapshots_dir(self) -> Path:
        return self.data_dir / "snapshots"


def _commit() -> str:
    # deploy.ps1 schreibt den ausgelieferten Commit nach src/COMMIT.
    try:
        return (Path(__file__).resolve().parent.parent / "COMMIT").read_text(encoding="utf-8").strip() or "dev"
    except OSError:
        return "dev"


def get_settings() -> Settings:
    return Settings(
        data_dir=Path(os.environ.get("STUDY_DATA_DIR", "/data")),
        proxy_secret=os.environ.get("PROXY_SECRET", ""),
        user_header=os.environ.get("USER_HEADER", "X-authentik-username"),
        dev_user=os.environ.get("STUDY_DEV_USER", ""),
        ai_provider=os.environ.get("STUDY_AI_PROVIDER", "anthropic"),
        model=os.environ.get("STUDY_MODEL", "claude-opus-5-5"),
        effort=os.environ.get("STUDY_EFFORT", "medium"),
        monthly_budget_usd=_float("STUDY_MONTHLY_BUDGET_USD", 10.0),
        max_upload_mb=_int("STUDY_MAX_UPLOAD_MB", 100),
        max_pages=_int("STUDY_MAX_PAGES", 400),
        pages_per_request=max(1, _int("STUDY_PAGES_PER_REQUEST", 8)),
        new_per_day=max(0, _int("STUDY_NEW_PER_DAY", 20)),
        worker=os.environ.get("STUDY_WORKER", "1") != "0",
        tz=os.environ.get("STUDY_TZ", "Europe/Zurich"),
        commit=_commit(),
    )
