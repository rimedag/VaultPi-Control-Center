from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from flask import current_app

from ..db import get_db


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def log_event(event_type: str, project_id: int | None, actor: str | None, message: str, details: str = "") -> None:
    db = get_db()
    db.execute(
        "INSERT INTO activity_log(event_type, project_id, actor, message, details, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (event_type, project_id, actor, message, details, utc_now()),
    )
    db.commit()


def trim_activity(max_rows: int = 1000) -> None:
    db = get_db()
    db.execute(
        "DELETE FROM activity_log WHERE id NOT IN (SELECT id FROM activity_log ORDER BY created_at DESC LIMIT ?)",
        (max_rows,),
    )
    db.commit()


def is_command_execution_enabled() -> bool:
    row = get_db().execute("SELECT value FROM settings WHERE key = 'command_execution_enabled'").fetchone()
    return bool(row and row["value"] == "1")


def app_logger() -> Any:
    return current_app.logger
