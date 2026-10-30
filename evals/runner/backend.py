"""The backend as a subprocess on a free port, with its own database and secrets."""

from __future__ import annotations

import os
import secrets
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx

from wg.settings import REPO_ROOT, read_env_file

HOST = "127.0.0.1"
BACKEND_DIR = REPO_ROOT / "backend"
STARTUP_TIMEOUT_S = 30.0
EXIT_TIMEOUT_S = 15.0
SECRET_NAMES = ("WG_CODE_HMAC_KEY", "WG_SESSION_SECRET", "WG_CHANNEL_SECRET")


class BackendError(RuntimeError):
    """The backend did not start or stop in time."""


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((HOST, 0))
        return sock.getsockname()[1]


def _setting_names() -> list[str]:
    """Every WG_ name in .env.example, so that the working copy's own .env never fills a gap."""
    example = REPO_ROOT / ".env.example"
    names = [line.partition("=")[0].strip() for line in example.read_text("utf-8").splitlines()]
    return [name for name in names if name.startswith("WG_")]


def backend_environment(
    workdir: Path,
    port: int,
    *,
    env_file: Path | None,
    stub_script: str | None,
    force_stub: bool,
    spend_cap_cny: str | None,
) -> dict[str, str]:
    """Process environment, then the env file, then the settings an episode must control.

    The database, fault marker, port and demo mode always belong to the episode. Secrets that the
    env file leaves empty are generated, so one episode never shares them with another.
    """
    env = dict.fromkeys(_setting_names(), "")
    env.update(os.environ)
    if env_file is not None:
        env.update(read_env_file(env_file))
    env.update(
        {
            "WG_PORT": str(port),
            "WG_DB_PATH": str(workdir / "wg.db"),
            "WG_FAULT_WAIT_FILE": str(workdir / "fault.wait"),
            "WG_DEMO_MODE": "1",
            "PYTHONUNBUFFERED": "1",
        }
    )
    for name in SECRET_NAMES:
        if not env.get(name):
            env[name] = secrets.token_hex(32)
    if force_stub:
        env["WG_LLM_PROVIDER"] = "stub"
        env["WG_LLM_API_KEY"] = ""
        env["WG_OUTBOUND_PROXY"] = ""
    if stub_script:
        env["WG_LLM_STUB_SCRIPT"] = stub_script
    if spend_cap_cny is not None:
        env["WG_LLM_SPEND_CAP_CNY"] = spend_cap_cny
    return env


class Backend:
    """One backend process; `restart` starts another on the same database and secrets."""

    def __init__(self, workdir: Path, env: dict[str, str]) -> None:
        workdir.mkdir(parents=True, exist_ok=True)
        self.workdir = workdir
        self.env = env
        self.port = int(env["WG_PORT"])
        self.log_path = workdir / "server.log"
        self.process: subprocess.Popen[bytes] | None = None
        self.starts = 0

    @property
    def base_url(self) -> str:
        return f"http://{HOST}:{self.port}"

    @property
    def running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def start(self) -> None:
        if self.running:
            raise BackendError("backend already running")
        Path(self.env["WG_FAULT_WAIT_FILE"]).unlink(missing_ok=True)
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
        self._wait_healthy()

    def _wait_healthy(self) -> None:
        deadline = time.monotonic() + STARTUP_TIMEOUT_S
        with httpx.Client(base_url=self.base_url, trust_env=False, timeout=2.0) as http:
            while True:
                if not self.running:
                    code = self.process.returncode if self.process else None
                    raise BackendError(f"backend exited ({code}) during startup\n{self.log_tail()}")
                try:
                    if http.get("/healthz").status_code == 200:
                        return
                except httpx.TransportError:
                    pass
                if time.monotonic() > deadline:
                    raise BackendError(f"backend not healthy after {STARTUP_TIMEOUT_S}s")
                time.sleep(0.05)

    def kill(self) -> None:
        """SIGKILL the process group."""
        if self.process is None or not self.running:
            return
        os.killpg(self.process.pid, signal.SIGKILL)
        self.process.wait(EXIT_TIMEOUT_S)

    def wait_exit(self) -> None:
        """Wait for the process to end by itself, as after POST /sim/crash or a crash fault."""
        if self.process is None:
            raise BackendError("backend was never started")
        try:
            self.process.wait(EXIT_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            raise BackendError(f"backend still running after {EXIT_TIMEOUT_S}s") from None

    def stop(self) -> None:
        """Terminate gracefully so that SQLite checkpoints its log; kill after a timeout."""
        if not self.running or self.process is None:
            return
        os.killpg(self.process.pid, signal.SIGTERM)
        try:
            self.process.wait(EXIT_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            self.kill()

    def log_tail(self, lines: int = 40) -> str:
        if not self.log_path.exists():
            return ""
        text = self.log_path.read_text(encoding="utf-8", errors="replace")
        return "\n".join(text.splitlines()[-lines:])
