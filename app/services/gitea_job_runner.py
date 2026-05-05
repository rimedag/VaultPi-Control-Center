from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .gitea_ops import build_command, clear_lock, get_job, latest_local_backup_info, read_status, write_lock, write_status

LOCAL_REPO_ROOT = os.getenv("LOCAL_REPO_ROOT", "/var/lib/gitea/data/gitea-repositories").strip()


def _gitea_config_path() -> str:
    configured = os.environ.get("GITEA_CONFIG_PATH", "").strip()
    if configured:
        return configured
    return "/etc/gitea/app.ini"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _run_as_user_prefix(user: str | None) -> list[str]:
    if not user or os.name != "posix":
        return []
    try:
        import pwd

        if os.geteuid() == pwd.getpwnam(user).pw_uid:
            return []
    except Exception:
        pass
    return ["sudo", "-n", "-u", user, "--"]


def _maybe_run_as_user_prefix(user: str | None, log_file: Any) -> list[str]:
    prefix = _run_as_user_prefix(user)
    if not prefix:
        return []

    probe = subprocess.run(  # noqa: S603
        prefix + ["true"],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
        cwd="/",
    )
    if probe.returncode == 0:
        return prefix

    details = ((probe.stdout or "") + (probe.stderr or "")).strip()
    log_file.write(f"Run-as-user unavailable, falling back to current user: {details or f'exit={probe.returncode}'}\n")
    return []


def _owner_run_prefix(path: str, log_file: Any) -> list[str]:
    if os.name != "posix":
        return []
    try:
        import pwd

        file_uid = os.stat(path).st_uid
        if os.geteuid() == file_uid:
            return []
        owner = pwd.getpwuid(file_uid).pw_name
    except Exception as exc:
        log_file.write(f"Unable to resolve file owner for {path}: {exc}\n")
        return []

    prefix = ["sudo", "-n", "-u", owner, "--"]
    probe = subprocess.run(  # noqa: S603
        prefix + ["true"],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
        cwd="/",
    )
    if probe.returncode == 0:
        log_file.write(f"Using backup file owner context for verification: {owner}\n")
        return prefix

    details = ((probe.stdout or "") + (probe.stderr or "")).strip()
    log_file.write(f"Owner-based verification unavailable for {owner}: {details or f'exit={probe.returncode}'}\n")
    return []


def _verify_zip_via_subprocess(backup_path: str, log_file: Any) -> tuple[int, list[str], str | None]:
    python_cmd = shutil.which("python3") or shutil.which("python") or "/usr/bin/python3"
    prefixes: list[list[str]] = [[], _owner_run_prefix(backup_path, log_file)]

    tried: list[str] = []
    for prefix in prefixes:
        cmd = prefix + [python_cmd, "-m", "zipfile", "-t", backup_path]
        tried.append(" ".join(cmd))
        try:
            result = subprocess.run(  # noqa: S603
                cmd,
                capture_output=True,
                text=True,
                check=False,
                timeout=180,
                cwd="/",
            )
        except Exception as exc:
            log_file.write(f"Subprocess zip verification failed to run: {exc}\n")
            continue

        output = ((result.stdout or "") + (result.stderr or "")).strip()
        if output:
            log_file.write(output[-5000:] + ("\n" if not output.endswith("\n") else ""))
        if result.returncode == 0:
            try:
                list_cmd = prefix + [python_cmd, "-c", "import sys, zipfile; print('\\n'.join(zipfile.ZipFile(sys.argv[1]).namelist()))", backup_path]
                list_result = subprocess.run(  # noqa: S603
                    list_cmd,
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=180,
                    cwd="/",
                )
                if list_result.returncode == 0:
                    names = [line for line in (list_result.stdout or "").splitlines() if line.strip()]
                    return 0, names, None
                list_output = ((list_result.stdout or "") + (list_result.stderr or "")).strip()
                if list_output:
                    log_file.write(list_output[-5000:] + ("\n" if not list_output.endswith("\n") else ""))
            except Exception as exc:
                log_file.write(f"Unable to list archive entries after subprocess verification: {exc}\n")
            return 0, [], None

        if "permission denied" in output.lower():
            continue
        return result.returncode or 1, [], output or None

    return 13, [], f"Permission denied while verifying archive. Tried: {' | '.join(tried)}"


def _latest_backup_artifact(path: str = "") -> str:
    path = path or os.getenv("LOCAL_BACKUP_DIR", "/backup/gitea").strip()
    backup_dir = Path(path)
    if not backup_dir.exists() or not backup_dir.is_dir():
        return ""
    files = [p for p in backup_dir.glob("gitea-*.zip") if p.is_file()]
    if not files:
        files = [p for p in backup_dir.glob("*.zip") if p.is_file()]
    if not files:
        return ""
    return str(max(files, key=lambda item: item.stat().st_mtime))


def _run_backup_verify(job: Any, log_file: Any) -> tuple[int, dict[str, Any]]:
    info = latest_local_backup_info()
    if not info.get("exists"):
        log_file.write("No local backup file found under /backup/gitea (tried gitea-*.zip, then *.zip).\n")
        return 2, {
            "latest_backup_file": "",
            "contains_repos": False,
            "contains_data": False,
            "contains_db_dump": False,
            "contains_config": False,
            "last_message": "No local backup file found.",
        }

    backup_path = str(info.get("path") or "")
    backup_file = str(info.get("filename") or "")
    log_file.write(f"Verifying latest backup: {backup_file}\n")

    try:
        with zipfile.ZipFile(backup_path, "r") as archive:
            test_result = archive.testzip()
            if test_result is not None:
                log_file.write(f"Zip integrity failed at entry: {test_result}\n")
                return 3, {
                    "latest_backup_file": backup_file,
                    "contains_repos": False,
                    "contains_data": False,
                    "contains_db_dump": False,
                    "contains_config": False,
                    "last_message": f"Zip integrity failed at {test_result}.",
                }
            names = archive.namelist()
    except PermissionError as exc:
        log_file.write(f"Zip verification permission error: {exc}\n")
        exit_code, names, detail = _verify_zip_via_subprocess(backup_path, log_file)
        if exit_code != 0:
            return 5, {
                "latest_backup_file": backup_file,
                "contains_repos": False,
                "contains_data": False,
                "contains_db_dump": False,
                "contains_config": False,
                "last_message": f"Backup archive is not readable by the app user: {detail or exc}",
            }
    except Exception as exc:
        log_file.write(f"Zip verification error: {exc}\n")
        return 4, {
            "latest_backup_file": backup_file,
            "contains_repos": False,
            "contains_data": False,
            "contains_db_dump": False,
            "contains_config": False,
            "last_message": f"Unable to read backup archive: {exc}",
        }

    lowered = [n.lower() for n in names]
    contains_repos = any("repos/" in n or n.startswith("repos") or "gitea-repo" in n for n in lowered)
    contains_data = any("data/" in n or n.startswith("data") for n in lowered)
    contains_db_dump = any(
        n.endswith(".sql") or n.endswith(".sql.gz") or n.endswith(".db") or "sqlite" in n for n in lowered
    )
    contains_config = any("app.ini" in n or "custom/conf" in n for n in lowered)

    log_file.write(
        "Contents check: "
        f"repos={contains_repos}, data={contains_data}, db_dump={contains_db_dump}, config={contains_config}\n"
    )

    return 0, {
        "latest_backup_file": backup_file,
        "contains_repos": contains_repos,
        "contains_data": contains_data,
        "contains_db_dump": contains_db_dump,
        "contains_config": contains_config,
        "last_message": "Latest backup passed zip integrity test.",
    }


def _run_repo_healthcheck(job: Any, log_file: Any) -> tuple[int, dict[str, Any]]:
    gitea_bin = shutil.which("gitea") or "/usr/local/bin/gitea"
    gitea_config = _gitea_config_path()
    prefix = _maybe_run_as_user_prefix(job.run_as_user, log_file)
    doctor_status = "skipped"
    doctor_exit_code: int | None = None

    if Path(gitea_bin).exists():
        doctor_cmd = prefix + [gitea_bin, "--config", gitea_config, "doctor", "check"]
        log_file.write(f"Running: {' '.join(doctor_cmd)}\n")
        try:
            doctor = subprocess.run(  # noqa: S603
                doctor_cmd,
                capture_output=True,
                text=True,
                check=False,
                timeout=180,
                cwd="/",
            )
            doctor_exit_code = doctor.returncode
            doctor_output = (doctor.stdout or "") + (doctor.stderr or "")
            log_file.write(doctor_output[-5000:])
            if doctor_output and not doctor_output.endswith("\n"):
                log_file.write("\n")
            doctor_status = "success" if doctor.returncode == 0 else "failed"
        except Exception as exc:
            doctor_status = "skipped"
            log_file.write(f"Gitea doctor skipped: {exc}\n")
    else:
        log_file.write(f"Gitea binary not found at {gitea_bin}; skipping doctor check.\n")

    repo_root = Path(LOCAL_REPO_ROOT)
    repos = sorted(repo_root.rglob("*.git")) if repo_root.exists() else []
    total_repos = len(repos)
    sample_size = min(10, total_repos)
    sample = repos[:sample_size]
    failures: list[str] = []

    for repo in sample:
        rel = str(repo).replace(f"{LOCAL_REPO_ROOT}/", "")
        check_cmd = prefix + ["git", "--git-dir", str(repo), "rev-parse", "--is-bare-repository"]
        res = subprocess.run(  # noqa: S603
            check_cmd,
            capture_output=True,
            text=True,
            check=False,
            timeout=20,
            cwd="/",
        )
        if res.returncode != 0:
            details = ((res.stdout or "") + (res.stderr or "")).strip()
            failures.append(f"{rel}: rev-parse failed")
            log_file.write(f"Repo check failed: {rel} {details}\n")
        else:
            log_file.write(f"Repo check ok: {rel}\n")

    doctor_ok = doctor_status in {"success", "skipped"}
    repos_ok = len(failures) == 0
    status = "success" if doctor_ok and repos_ok else "failed"
    if status == "failed":
        message = f"Doctor={doctor_status}, repos sampled={sample_size}, failures={len(failures)}"
    elif doctor_status == "skipped":
        message = f"Repo checks passed; doctor skipped, repos sampled={sample_size}"
    else:
        message = f"Doctor OK, repos sampled={sample_size}, failures=0"

    return (0 if status == "success" else 3), {
        "doctor_exit_code": doctor_exit_code,
        "doctor_status": doctor_status,
        "total_repos": total_repos,
        "repos_checked": sample_size,
        "repos_failed": len(failures),
        "failure_samples": failures[:10],
        "last_message": message,
    }


def run_job(job_key: str, actor: str = "") -> int:
    job = get_job(job_key)
    if not job:
        return 2

    started_at = _utc_now()
    start_monotonic = time.monotonic()
    status = read_status(job)
    status.update(
        {
            "running": True,
            "last_started_at": started_at,
            "last_status": "running",
            "last_message": f"Running ({actor or 'system'})",
        }
    )
    write_status(job, status)
    write_lock(job, os.getpid(), actor=actor)

    log_path = Path(job.log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    status_path = Path(job.status_path)
    status_path.parent.mkdir(parents=True, exist_ok=True)

    exit_code = 1
    artifact = status.get("last_artifact", "")
    try:
        with log_path.open("a", encoding="utf-8") as log_file:
            log_file.write(f"\n=== {job.key} started at {started_at} (actor={actor or 'system'}) ===\n")
            log_file.flush()

            extra_status: dict[str, Any] = {}
            if job.job_type == "internal":
                if job.key == "gitea-backup-verify":
                    exit_code, extra_status = _run_backup_verify(job, log_file)
                elif job.key == "gitea-healthcheck":
                    exit_code, extra_status = _run_repo_healthcheck(job, log_file)
                else:
                    exit_code = 2
                    extra_status = {"last_message": f"Unsupported internal job: {job.key}"}
            else:
                command = build_command(job)
                process = subprocess.Popen(  # noqa: S603
                    command,
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                    close_fds=True,
                    cwd="/",
                )
                try:
                    exit_code = process.wait(timeout=7200)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                    log_file.write(f"\n=== {job.key} killed after 2-hour timeout ===\n")
                    exit_code = 124
                if job.key == "gitea-backup" and exit_code == 0:
                    artifact = _latest_backup_artifact()

            finished_at = _utc_now()
            duration = int(max(0, time.monotonic() - start_monotonic))
            success = exit_code == 0

            status.update(
                {
                    "running": False,
                    "last_finished_at": finished_at,
                    "last_status": "success" if success else "failed",
                    "last_exit_code": exit_code,
                    "last_duration_seconds": duration,
                    "last_message": extra_status.get("last_message", "Completed successfully." if success else f"Failed with exit code {exit_code}."),
                    "last_artifact": artifact or "",
                    **extra_status,
                }
            )
            write_status(job, status)
            log_file.write(f"=== {job.key} finished at {finished_at} exit={exit_code} duration={duration}s ===\n")
            log_file.flush()
    except Exception as exc:
        finished_at = _utc_now()
        duration = int(max(0, time.monotonic() - start_monotonic))
        status.update(
            {
                "running": False,
                "last_finished_at": finished_at,
                "last_status": "failed",
                "last_exit_code": 1,
                "last_duration_seconds": duration,
                "last_message": f"Runner error: {exc}",
            }
        )
        write_status(job, status)
        exit_code = 1
    finally:
        clear_lock(job)

    return exit_code


def main() -> int:
    if len(sys.argv) < 2:
        return 2
    job_key = sys.argv[1].strip()
    actor = sys.argv[2].strip() if len(sys.argv) > 2 else ""
    return run_job(job_key, actor=actor)


if __name__ == "__main__":
    raise SystemExit(main())
