from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass
class CommandResult:
    exit_code: int
    stdout: str
    stderr: str


def run_command(command: str, timeout_sec: int = 30, working_directory: str = "") -> CommandResult:
    cwd = working_directory.strip() or None
    if cwd and not Path(cwd).exists():
        return CommandResult(2, "", f"Working directory does not exist: {cwd}")

    try:
        process = subprocess.run(
            command,
            shell=True,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout_sec,
            check=False,
        )
        return CommandResult(process.returncode, process.stdout[-4000:], process.stderr[-4000:])
    except subprocess.TimeoutExpired:
        return CommandResult(124, "", f"Command timed out after {timeout_sec}s")
    except Exception as exc:
        return CommandResult(1, "", str(exc))
