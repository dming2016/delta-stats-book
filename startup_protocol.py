"""One-shot startup readiness handshake shared by the app and launcher."""

from __future__ import annotations

import os
from pathlib import Path


STARTUP_READY_FILE_ENV = "DELTA_STATS_STARTUP_READY_FILE"
STARTUP_READY_VALUE = "ready"


def mark_startup_ready() -> bool:
    raw_path = os.environ.pop(STARTUP_READY_FILE_ENV, "").strip()
    if not raw_path:
        return False
    with Path(raw_path).open("x", encoding="utf-8") as handle:
        handle.write(STARTUP_READY_VALUE)
    return True


def is_startup_ready(path: Path) -> bool:
    try:
        return path.read_text(encoding="utf-8") == STARTUP_READY_VALUE
    except (OSError, UnicodeError):
        return False
