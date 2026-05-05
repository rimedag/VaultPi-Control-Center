from __future__ import annotations

import threading
import time
from datetime import datetime, timezone

from flask import Flask

from ..db import get_db
from .activity import log_event, trim_activity
from .checks import check_http


class HealthChecker:
    def __init__(self, app: Flask) -> None:
        self.app = app
        self.lock = threading.Lock()
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        if self.thread and self.thread.is_alive():
            return
        self.thread = threading.Thread(target=self._loop, daemon=True, name="vaultpi-health-checker")
        self.thread.start()

    def _loop(self) -> None:
        while True:
            try:
                if self._interval() <= 0:
                    time.sleep(60)
                    continue
                self.run_once()
            except Exception as exc:
                with self.app.app_context():
                    log_event("checker_error", None, "system", "Health checker failed", str(exc))
            time.sleep(self._interval())

    def _interval(self) -> int:
        with self.app.app_context():
            row = get_db().execute("SELECT value FROM settings WHERE key='monitor_interval'").fetchone()
            try:
                value = int(row["value"]) if row else 60
            except Exception:
                value = 60
        if value <= 0:
            return 0
        return max(15, min(600, value))

    def run_once(self) -> None:
        if not self.lock.acquire(blocking=False):
            return
        try:
            with self.app.app_context():
                db = get_db()
                projects = db.execute(
                    """
                    SELECT id, name, healthcheck_url, local_url, remote_url
                    FROM projects
                    WHERE enabled = 1 AND archived = 0 AND monitoring_enabled = 1
                    ORDER BY display_order ASC, name ASC
                    """
                ).fetchall()

                for project in projects:
                    target = (project["healthcheck_url"] or project["local_url"] or project["remote_url"] or "").strip()
                    if not target:
                        continue

                    result = check_http(target)
                    now = datetime.now(timezone.utc).isoformat()
                    db.execute(
                        """
                        INSERT INTO service_checks(project_id, checked_at, status, status_code, response_ms, error_text, checker)
                        VALUES (?, ?, ?, ?, ?, ?, 'http')
                        """,
                        (
                            project["id"],
                            now,
                            result.status,
                            result.status_code,
                            result.response_ms,
                            result.error_text,
                        ),
                    )
                    if result.status == "down":
                        log_event(
                            "healthcheck_failure",
                            project["id"],
                            "system",
                            f"Health check failed for {project['name']}",
                            result.error_text or f"status={result.status_code}",
                        )

                retention_row = db.execute("SELECT value FROM settings WHERE key='history_retention'").fetchone()
                retention = int(retention_row["value"]) if retention_row else 500
                retention = max(50, min(2000, retention))

                db.execute(
                    """
                    DELETE FROM service_checks
                    WHERE id NOT IN (
                        SELECT id FROM service_checks ORDER BY checked_at DESC LIMIT ?
                    )
                    """,
                    (retention,),
                )
                db.commit()
                trim_activity(1200)
        finally:
            self.lock.release()
