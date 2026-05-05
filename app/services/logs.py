from __future__ import annotations

from collections import deque
from pathlib import Path


def read_log_tail(path: str, lines: int = 120) -> str:
    if not path:
        return "No log path configured."
    p = Path(path)
    if not p.exists() or not p.is_file():
        return f"Log file not found: {path}"

    try:
        with p.open("r", encoding="utf-8", errors="replace") as f:
            tail = deque(f, maxlen=lines)
        return "".join(tail)
    except Exception as exc:
        return f"Unable to read log: {exc}"
