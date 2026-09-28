from __future__ import annotations

import os
from pathlib import Path

APP_DIR_NAME = ".ducky"


def app_dir() -> Path:
    custom = os.environ.get("DUCKY_HOME")
    if custom:
        return Path(custom).expanduser().resolve()
    return Path.home() / APP_DIR_NAME


def ensure_app_dirs(base: Path | None = None) -> dict[str, Path]:
    base = base or app_dir()
    paths = {
        "base": base,
        "logs": base / "logs",
        "reports": base / "reports",
        "dataset": base / "dataset",
        "outbox": base / "outbox",
        "packs": base / "packs",
        "server_packs": base / "server_packs",
        "teacher": base / "teacher",
        "student_packs": base / "packs",
        "workspace": base / "workspace",
    }
    for path in paths.values():
        path.mkdir(parents=True, exist_ok=True)
    return paths
