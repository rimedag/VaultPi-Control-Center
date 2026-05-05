from __future__ import annotations

import os
import pty
import re
import select
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Any


MAX_BUFFER_CHARS = 120000
IDLE_TIMEOUT_SECONDS = 1800
ANSI_ESCAPE_RE = re.compile(
    r"""
    \x1B\[[0-?]*[ -/]*[@-~] |   # CSI sequences
    \x1B\][^\x07\x1B]*(?:\x07|\x1B\\) |  # OSC sequences
    \x1B[@-_]                   # Single-character escape sequences
    """,
    re.VERBOSE,
)


@dataclass
class TerminalSnapshot:
    output: str
    cursor: int
    alive: bool
    reset: bool


class TerminalSession:
    def __init__(self, shell: str = "/bin/bash") -> None:
        self.shell = shell
        self.master_fd: int | None = None
        self.pid: int | None = None
        self.created_at = time.time()
        self.last_active = self.created_at
        self.buffer = ""
        self.base_offset = 0
        self._lock = threading.Lock()
        self._start_shell()

    def _start_shell(self) -> None:
        pid, master_fd = pty.fork()
        if pid == 0:
            env = os.environ.copy()
            env.setdefault("TERM", "xterm-256color")
            env.setdefault("COLORTERM", "truecolor")
            env["PROMPT_COMMAND"] = ""
            env["INPUTRC"] = "/dev/null"
            env["PS1"] = "pi@vaultpi:\\w $ "
            os.execvpe(self.shell, [self.shell, "--noprofile", "--norc", "-i"], env)
        os.set_blocking(master_fd, False)
        self.pid = pid
        self.master_fd = master_fd
        time.sleep(0.15)
        self.write("bind 'set enable-bracketed-paste off'\n")
        self._drain_output()

    def is_alive(self) -> bool:
        if self.pid is None:
            return False
        try:
            result = os.waitpid(self.pid, os.WNOHANG)
            return result == (0, 0)
        except ChildProcessError:
            return False

    def touch(self) -> None:
        self.last_active = time.time()

    def _append_output(self, text: str) -> None:
        if not text:
            return
        text = ANSI_ESCAPE_RE.sub("", text).replace("\r\n", "\n").replace("\r", "\n")
        self.buffer += text
        if len(self.buffer) > MAX_BUFFER_CHARS:
            extra = len(self.buffer) - MAX_BUFFER_CHARS
            self.buffer = self.buffer[extra:]
            self.base_offset += extra

    def _drain_output(self) -> None:
        if self.master_fd is None:
            return
        chunks: list[str] = []
        while True:
            readable, _, _ = select.select([self.master_fd], [], [], 0)
            if not readable:
                break
            try:
                data = os.read(self.master_fd, 4096)
            except BlockingIOError:
                break
            except OSError:
                break
            if not data:
                break
            chunks.append(data.decode("utf-8", errors="replace"))
        if chunks:
            with self._lock:
                self._append_output("".join(chunks))

    def snapshot(self, cursor: int = 0) -> TerminalSnapshot:
        self.touch()
        self._drain_output()
        alive = self.is_alive()
        with self._lock:
            if cursor < self.base_offset:
                return TerminalSnapshot(self.buffer, self.base_offset + len(self.buffer), alive, True)
            relative = max(0, cursor - self.base_offset)
            output = self.buffer[relative:]
            return TerminalSnapshot(output, self.base_offset + len(self.buffer), alive, False)

    def write(self, data: str) -> bool:
        self.touch()
        if self.master_fd is None or not self.is_alive():
            return False
        if not data:
            return True
        os.write(self.master_fd, data.encode("utf-8", errors="ignore"))
        time.sleep(0.03)
        self._drain_output()
        return True

    def terminate(self) -> None:
        if self.pid is not None and self.is_alive():
            try:
                os.kill(self.pid, signal.SIGTERM)
            except OSError:
                pass
            time.sleep(0.1)
            if self.is_alive():
                try:
                    os.kill(self.pid, signal.SIGKILL)
                except OSError:
                    pass
        if self.master_fd is not None:
            try:
                os.close(self.master_fd)
            except OSError:
                pass
        self.master_fd = None
        self.pid = None


class TerminalManager:
    def __init__(self) -> None:
        self._sessions: dict[str, TerminalSession] = {}
        self._lock = threading.Lock()

    def _cleanup_idle_locked(self) -> None:
        now = time.time()
        expired = [key for key, sess in self._sessions.items() if (not sess.is_alive()) or (now - sess.last_active > IDLE_TIMEOUT_SECONDS)]
        for key in expired:
            self._sessions.pop(key).terminate()

    def ensure(self, key: str) -> TerminalSession:
        with self._lock:
            self._cleanup_idle_locked()
            session = self._sessions.get(key)
            if session and session.is_alive():
                return session
            if session:
                session.terminate()
            session = TerminalSession()
            self._sessions[key] = session
            return session

    def reset(self, key: str) -> TerminalSession:
        with self._lock:
            old = self._sessions.pop(key, None)
            if old:
                old.terminate()
            session = TerminalSession()
            self._sessions[key] = session
            return session

    def read(self, key: str, cursor: int = 0) -> TerminalSnapshot:
        session = self.ensure(key)
        return session.snapshot(cursor)

    def write(self, key: str, data: str) -> bool:
        session = self.ensure(key)
        return session.write(data)

    def close(self, key: str) -> None:
        with self._lock:
            session = self._sessions.pop(key, None)
        if session:
            session.terminate()


terminal_manager = TerminalManager()
