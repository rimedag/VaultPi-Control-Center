from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from flask import has_app_context

from ..db import setting
from .logs import read_log_tail

APP_ROOT = Path(__file__).resolve().parents[2]
GITEA_BACKUP_SCRIPT_PATH = os.getenv("GITEA_BACKUP_SCRIPT_PATH", "/usr/local/bin/gitea-backup.sh").strip()
SCRIPT_BACKUP_DIR = os.getenv("GITEA_SCRIPT_BACKUP_DIR", "/usr/local/bin/script-backups").strip()
LOCAL_BACKUP_DIR = os.getenv("LOCAL_BACKUP_DIR", "/backup/gitea").strip()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class JobSpec:
    key: str
    label: str
    lock_path: str
    log_path: str
    status_path: str
    script_path: str = ""
    run_as_user: str | None = None
    job_type: str = "script"  # script | internal
    run_button_label: str = "Run Now"
    editable_script: bool = False


JOBS: dict[str, JobSpec] = {
    "gitea-backup": JobSpec(
        key="gitea-backup",
        label="Local Gitea Backup",
        script_path=GITEA_BACKUP_SCRIPT_PATH,
        lock_path="/tmp/gitea-backup.lock",
        log_path="/var/log/vaultpi/gitea-backup-run.log",
        status_path="/var/lib/vaultpi/status/gitea-backup.json",
        run_as_user="git",
        job_type="script",
        run_button_label="Run Local Backup Now",
        editable_script=True,
    ),
    "gitea-sync-android": JobSpec(
        key="gitea-sync-android",
        label="Android Sync",
        script_path="/usr/local/bin/gitea-sync-android.sh",
        lock_path="/tmp/gitea-sync-android.lock",
        log_path="/var/log/vaultpi/gitea-sync-android-run.log",
        status_path="/var/lib/vaultpi/status/gitea-sync-android.json",
        run_as_user="git",
        job_type="script",
        run_button_label="Run Android Sync Now",
        editable_script=True,
    ),
    "gitea-backup-verify": JobSpec(
        key="gitea-backup-verify",
        label="Backup Verification",
        lock_path="/tmp/gitea-backup-verify.lock",
        log_path="/var/log/vaultpi/gitea-backup-verify.log",
        status_path="/var/lib/vaultpi/status/gitea-backup-verify.json",
        run_as_user="git",
        job_type="internal",
        run_button_label="Verify Latest Backup",
        editable_script=False,
    ),
    "gitea-healthcheck": JobSpec(
        key="gitea-healthcheck",
        label="Repository Health Check",
        lock_path="/tmp/gitea-healthcheck.lock",
        log_path="/var/log/vaultpi/gitea-healthcheck.log",
        status_path="/var/lib/vaultpi/status/gitea-healthcheck.json",
        run_as_user="git",
        job_type="internal",
        run_button_label="Run Repo Health Check",
        editable_script=False,
    ),
}


def _job_env_overrides(job: JobSpec) -> dict[str, str]:
    if not has_app_context():
        return {}

    env: dict[str, str] = {}

    if job.key == "gitea-backup":
        env["GITEA_CONFIG_PATH"] = setting(
            "gitea_config_path",
            os.getenv("GITEA_CONFIG_PATH", "/etc/gitea/app.ini"),
        ).strip() or "/etc/gitea/app.ini"
        env["BACKUP_DIR"] = os.getenv("LOCAL_BACKUP_DIR", "/backup/gitea").strip()
        env["REPO_DIR"] = os.getenv("LOCAL_REPO_ROOT", "/var/lib/gitea/data/gitea-repositories").strip()

    if job.key == "gitea-sync-android":
        android_host = setting("android_host", "phone.lan").strip()
        android_port = setting("android_termux_ssh_port", "8022").strip() or "8022"
        android_user = setting("android_ssh_user", "git").strip() or "git"
        android_backup_path = setting("android_backup_path", "/data/data/com.termux/files/home/gitea-backups").strip()
        android_mirror_path = setting("android_mirror_path", "/data/data/com.termux/files/home/gitea-mirrors").strip()
        android_gitea_url = setting("android_gitea_url", "http://phone.lan:3000").strip()
        env.update(
            {
                "PHONE_HOST": android_host,
                "PHONE_PORT": android_port,
                "PHONE_USER": android_user,
                "ANDROID_BACKUP_DIR": android_backup_path,
                "ANDROID_MIRROR_DIR": android_mirror_path,
                "ANDROID_GITEA_URL": android_gitea_url,
            }
        )

    if job.key == "gitea-healthcheck":
        env["GITEA_CONFIG_PATH"] = setting(
            "gitea_config_path",
            os.getenv("GITEA_CONFIG_PATH", "/etc/gitea/app.ini"),
        ).strip() or "/etc/gitea/app.ini"

    return {key: value for key, value in env.items() if value}


def allowed_job_keys() -> list[str]:
    return sorted(JOBS.keys())


def get_job(job_key: str) -> JobSpec | None:
    return JOBS.get(job_key)


def script_exists(job: JobSpec) -> bool:
    if job.job_type != "script":
        return True
    return bool(job.script_path) and Path(job.script_path).is_file()


def latest_local_backup_info() -> dict[str, Any]:
    backup_dir = Path(LOCAL_BACKUP_DIR)
    result = {"exists": False, "path": "", "filename": "", "size_bytes": 0, "mtime": ""}
    if not backup_dir.exists() or not backup_dir.is_dir():
        return result

    candidates = [p for p in backup_dir.glob("gitea-*.zip") if p.is_file()]
    if not candidates:
        candidates = [p for p in backup_dir.glob("*.zip") if p.is_file()]
    if not candidates:
        return result

    latest = max(candidates, key=lambda p: p.stat().st_mtime)
    result["exists"] = True
    result["path"] = str(latest)
    result["filename"] = latest.name
    result["size_bytes"] = latest.stat().st_size
    result["mtime"] = datetime.fromtimestamp(latest.stat().st_mtime, tz=timezone.utc).isoformat()
    return result


def _safe_read_json(path: str) -> dict[str, Any]:
    p = Path(path)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_json_atomic(path: str, payload: dict[str, Any]) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=str(p.parent), delete=False) as tmp:
        tmp.write(json.dumps(payload, indent=2, sort_keys=True))
        tmp_path = Path(tmp.name)
    os.replace(tmp_path, p)


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return False


def _default_status(job: JobSpec) -> dict[str, Any]:
    return {
        "job": job.key,
        "running": False,
        "last_started_at": "",
        "last_finished_at": "",
        "last_status": "never",
        "last_exit_code": None,
        "last_message": "Never run",
        "last_duration_seconds": None,
        "last_artifact": "",
    }


def read_status(job: JobSpec) -> dict[str, Any]:
    payload = _default_status(job)
    payload.update(_safe_read_json(job.status_path))
    if payload.get("job") != job.key:
        payload["job"] = job.key
    return payload


def write_status(job: JobSpec, status_payload: dict[str, Any]) -> None:
    payload = _default_status(job)
    payload.update(status_payload)
    payload["job"] = job.key
    _write_json_atomic(job.status_path, payload)


def _lock_payload(job: JobSpec) -> dict[str, Any]:
    payload = _safe_read_json(job.lock_path)
    pid = int(payload.get("pid") or 0)
    if pid > 0 and _pid_alive(pid):
        payload["pid"] = pid
        return payload

    if Path(job.lock_path).exists():
        try:
            Path(job.lock_path).unlink()
        except Exception:
            pass
    return {}


def is_running(job: JobSpec) -> bool:
    lock = _lock_payload(job)
    return bool(lock.get("pid"))


def write_lock(job: JobSpec, pid: int, actor: str = "") -> None:
    _write_json_atomic(
        job.lock_path,
        {"job": job.key, "pid": pid, "started_at": _utc_now(), "actor": actor},
    )


def clear_lock(job: JobSpec) -> None:
    p = Path(job.lock_path)
    if p.exists():
        p.unlink(missing_ok=True)


def _acquire_launch_lock(job: JobSpec, actor: str = "") -> bool:
    lock_path = Path(job.lock_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    for _ in range(2):
        try:
            with lock_path.open("x", encoding="utf-8") as lock_file:
                json.dump(
                    {
                        "job": job.key,
                        "pid": os.getpid(),
                        "started_at": _utc_now(),
                        "actor": actor,
                        "phase": "launching",
                    },
                    lock_file,
                )
            return True
        except FileExistsError:
            if _lock_payload(job):
                return False
            continue
    return False


def build_command(job: JobSpec) -> list[str]:
    if job.job_type != "script":
        return []

    if not job.run_as_user:
        return [job.script_path]

    if os.name != "posix":
        return [job.script_path]

    try:
        import pwd  # Linux-only helper

        target_uid = pwd.getpwnam(job.run_as_user).pw_uid
        if os.geteuid() == target_uid:
            return [job.script_path]
    except Exception:
        pass

    return ["sudo", "-n", "-u", job.run_as_user, "--", job.script_path]


def start_job(job: JobSpec, actor: str) -> tuple[str, str]:
    if not _acquire_launch_lock(job, actor=actor):
        return "already_running", f"{job.label} is already running."

    if job.job_type == "script" and not script_exists(job):
        status = read_status(job)
        status["last_message"] = f"Script not found: {job.script_path}"
        status["last_status"] = "failed"
        status["running"] = False
        write_status(job, status)
        clear_lock(job)
        return "missing_script", f"Script not found: {job.script_path}"

    try:
        Path(job.log_path).parent.mkdir(parents=True, exist_ok=True)
        Path(job.status_path).parent.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        clear_lock(job)
        return "failed_to_start", f"Unable to prepare log/status paths: {exc}"

    started_at = _utc_now()
    status = read_status(job)
    status.update(
        {
            "running": True,
            "last_started_at": started_at,
            "last_message": f"Started by {actor}",
        }
    )
    write_status(job, status)

    runner_cmd = [sys.executable, "-m", "app.services.gitea_job_runner", job.key, actor]
    runner_env = os.environ.copy()
    python_path = runner_env.get("PYTHONPATH", "").strip()
    runner_env["PYTHONPATH"] = str(APP_ROOT) if not python_path else os.pathsep.join([str(APP_ROOT), python_path])
    runner_env.update(_job_env_overrides(job))
    try:
        process = subprocess.Popen(  # noqa: S603
            runner_cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            close_fds=True,
            start_new_session=True,
            cwd=str(APP_ROOT),
            env=runner_env,
        )
        write_lock(job, process.pid, actor=actor)
        return "started", f"{job.label} started."
    except Exception as exc:
        status["running"] = False
        status["last_status"] = "failed"
        status["last_message"] = f"Failed to start: {exc}"
        write_status(job, status)
        clear_lock(job)
        return "failed_to_start", f"Failed to start job: {exc}"


def status_with_runtime(job: JobSpec) -> dict[str, Any]:
    status = read_status(job)
    running = is_running(job)
    if status.get("running") != running:
        status["running"] = running
        if not running and status.get("last_status") == "running":
            status["last_status"] = "failed"
            status["last_message"] = "Detected stale lock; last run did not finish cleanly."
        write_status(job, status)

    status["script_exists"] = script_exists(job)
    status["label"] = job.label
    status["script_path"] = job.script_path
    status["job_type"] = job.job_type
    status["run_button_label"] = job.run_button_label
    status["editable_script"] = job.editable_script
    status["log_path"] = job.log_path
    status["status_path"] = job.status_path
    status["lock_path"] = job.lock_path
    status["tail"] = read_log_tail(job.log_path, lines=16)[-1200:]
    return status


def all_job_statuses() -> dict[str, dict[str, Any]]:
    return {key: status_with_runtime(job) for key, job in JOBS.items()}


def read_script(job: JobSpec) -> tuple[bool, str, str | None]:
    p = Path(job.script_path)
    if not p.exists():
        return False, "", None
    try:
        return True, p.read_text(encoding="utf-8", errors="replace"), None
    except OSError as exc:
        return True, "", f"Unable to read script at {job.script_path}: {exc}"


def list_script_backups(job: JobSpec, limit: int = 8) -> tuple[list[str], str | None]:
    if not job.script_path:
        return [], None
    backup_dir = Path(SCRIPT_BACKUP_DIR)
    if not backup_dir.exists() or not backup_dir.is_dir():
        return [], None

    pattern = f"{Path(job.script_path).name}.*.bak"
    try:
        backups = [p for p in backup_dir.glob(pattern) if p.is_file()]
        backups.sort(key=lambda item: item.stat().st_mtime, reverse=True)
    except OSError as exc:
        return [], f"Unable to list script backups in {SCRIPT_BACKUP_DIR}: {exc}"
    return [str(p) for p in backups[:limit]], None


def save_script(job: JobSpec, content: str) -> tuple[bool, str]:
    p = Path(job.script_path)
    existing_mode = stat.S_IMODE(p.stat().st_mode) if p.exists() else 0o750

    backup_path = ""
    if p.exists():
        backup_dir = Path(SCRIPT_BACKUP_DIR)
        backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
        backup_path = str(backup_dir / f"{p.name}.{stamp}.bak")
        shutil.copy2(p, backup_path)

    p.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=str(p.parent), delete=False) as tmp:
        tmp.write(content)
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp_path = Path(tmp.name)

    os.replace(tmp_path, p)
    os.chmod(p, existing_mode | stat.S_IXUSR)

    if backup_path:
        return True, f"Saved. Previous version backed up to {backup_path}"
    return True, "Saved."


def read_full_log(job: JobSpec, max_bytes: int = 2_000_000) -> tuple[str, bool]:
    p = Path(job.log_path)
    if not p.exists():
        return f"Log file not found: {job.log_path}", False
    raw = p.read_bytes()
    truncated = False
    if len(raw) > max_bytes:
        raw = raw[-max_bytes:]
        truncated = True
    return raw.decode("utf-8", errors="replace"), truncated
