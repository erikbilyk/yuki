"""The backend as a real subprocess: start, health wait, SIGKILL, restart on the same database."""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from types import TracebackType

import httpx

from .env import BACKEND_DIR, subprocess_environment

HOST = "127.0.0.1"
STARTUP_TIMEOUT_S = 30.0
EXIT_TIMEOUT_S = 15.0
MARKER_TIMEOUT_S = 15.0


class ServerError(RuntimeError):
    """The backend did not start, stop or reach a fault point in time."""


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((HOST, 0))
        return sock.getsockname()[1]


class BackendServer:
    """One backend process on its own port, database, fault marker and secrets.

    `restart()` starts a new process with the same environment, so the database, the code
    key and the session secret survive the restart exactly as in a real deployment.
    """

    def __init__(self, workdir: Path) -> None:
        workdir.mkdir(parents=True, exist_ok=True)
        self.workdir = workdir
        self.port = free_port()
        self.env = subprocess_environment(workdir, self.port)
        self.db_path = Path(self.env["WG_DB_PATH"])
        self.fault_wait_file = Path(self.env["WG_FAULT_WAIT_FILE"])
        self.log_path = workdir / "server.log"
        self.process: subprocess.Popen[bytes] | None = None
        self.starts = 0

    @property
    def base_url(self) -> str:
        return f"http://{HOST}:{self.port}"

    @property
    def running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def start(self, timeout: float = STARTUP_TIMEOUT_S) -> None:
        if self.running:
            raise ServerError("backend already running")
        self.fault_wait_file.unlink(missing_ok=True)
        with self.log_path.open("ab") as log:
            log.write(f"--- start {self.starts + 1}\n".encode())
            log.flush()
            self.process = subprocess.Popen(
                [sys.executable, "-m", "wg.api.serve"],
                cwd=BACKEND_DIR,
                env=self.env,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        self.starts += 1
        self.wait_healthy(timeout)

    def wait_healthy(self, timeout: float = STARTUP_TIMEOUT_S) -> None:
        deadline = time.monotonic() + timeout
        with httpx.Client(base_url=self.base_url, trust_env=False, timeout=2.0) as http:
            while True:
                if not self.running:
                    code = self.process.returncode if self.process else None
                    raise ServerError(f"backend exited ({code}) during startup\n{self.log_tail()}")
                try:
                    if http.get("/healthz").status_code == 200:
                        return
                except httpx.TransportError:
                    pass
                if time.monotonic() > deadline:
                    raise ServerError(f"backend not healthy after {timeout}s\n{self.log_tail()}")
                time.sleep(0.05)

    def kill(self) -> int:
        """SIGKILL the process group and return the exit status."""
        if self.process is None:
            raise ServerError("backend was never started")
        if self.running:
            os.killpg(self.process.pid, signal.SIGKILL)
        return self.process.wait(EXIT_TIMEOUT_S)

    def stop(self) -> None:
        if not self.running or self.process is None:
            return
        os.killpg(self.process.pid, signal.SIGTERM)
        try:
            self.process.wait(EXIT_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            self.kill()

    def restart(self, timeout: float = STARTUP_TIMEOUT_S) -> None:
        """Start again on the same database; a still-running process is stopped first."""
        self.stop()
        self.start(timeout)

    def wait_exit(self, timeout: float = EXIT_TIMEOUT_S) -> int:
        """Wait for the process to end by itself (a crash fault) and return its status."""
        if self.process is None:
            raise ServerError("backend was never started")
        try:
            return self.process.wait(timeout)
        except subprocess.TimeoutExpired:
            raise ServerError(f"backend still running after {timeout}s") from None

    def wait_for_fault_marker(self, timeout: float = MARKER_TIMEOUT_S) -> None:
        """Wait until a pause fault has written WG_FAULT_WAIT_FILE and blocked."""
        deadline = time.monotonic() + timeout
        while not self.fault_wait_file.exists():
            if not self.running:
                raise ServerError(f"backend exited before the fault marker\n{self.log_tail()}")
            if time.monotonic() > deadline:
                raise ServerError(f"no fault marker after {timeout}s\n{self.log_tail()}")
            time.sleep(0.02)

    def log_text(self) -> str:
        if not self.log_path.exists():
            return ""
        return self.log_path.read_text(encoding="utf-8", errors="replace")

    def log_tail(self, lines: int = 40) -> str:
        return "\n".join(self.log_text().splitlines()[-lines:])

    def __enter__(self) -> BackendServer:
        self.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.stop()
