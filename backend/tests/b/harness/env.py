"""Isolated settings for backends started by the harness: own database, demo mode, stub model."""

from __future__ import annotations

import os
import secrets
from pathlib import Path

from wg.settings import REPO_ROOT, Settings, load_settings

BACKEND_DIR = REPO_ROOT / "backend"
FIXTURE_PATH = BACKEND_DIR / "tests" / "fixtures" / "fixture_v1.yaml"

# Names known when the harness was written; .env.example adds any later ones.
_KNOWN_SETTINGS = (
    "WG_PORT",
    "WG_WEB_PORT",
    "WG_DB_PATH",
    "WG_FAULT_WAIT_FILE",
    "WG_DEMO_MODE",
    "WG_CODE_HMAC_KEY",
    "WG_SESSION_SECRET",
    "WG_CHANNEL_SECRET",
    "WG_LLM_PROVIDER",
    "WG_LLM_API_KEY",
    "WG_LLM_BASE_URL",
    "WG_LLM_MODEL",
    "WG_LLM_THINKING",
    "WG_LLM_TIMEOUT_S",
    "WG_LLM_STUB_SCRIPT",
    "WG_LLM_PRICE_TABLE",
    "WG_LLM_SPEND_CAP_CNY",
    "WG_OUTBOUND_PROXY",
    "WG_FEISHU_APP_ID",
    "WG_FEISHU_APP_SECRET",
    "WG_BACKEND_URL",
    "WG_WEB_BASE_URL",
)


def setting_names() -> tuple[str, ...]:
    """Every WG_ setting name, so that each one can be set explicitly."""
    names: dict[str, None] = dict.fromkeys(_KNOWN_SETTINGS)
    example = REPO_ROOT / ".env.example"
    if example.is_file():
        for line in example.read_text(encoding="utf-8").splitlines():
            name = line.partition("=")[0].strip()
            if name.startswith("WG_"):
                names.setdefault(name)
    return tuple(names)


def harness_settings_env(workdir: Path, port: int = 8100) -> dict[str, str]:
    """WG_ values for one backend; every name is present so the working copy's .env never applies.

    Empty values fall back to the built-in defaults; the model provider is the offline stub.
    """
    values = dict.fromkeys(setting_names(), "")
    values.update(
        {
            "WG_PORT": str(port),
            "WG_DB_PATH": str(workdir / "wg.db"),
            "WG_FAULT_WAIT_FILE": str(workdir / "fault.wait"),
            "WG_DEMO_MODE": "1",
            "WG_LLM_PROVIDER": "stub",
            "WG_CODE_HMAC_KEY": secrets.token_hex(32),
            "WG_SESSION_SECRET": secrets.token_hex(32),
            "WG_CHANNEL_SECRET": secrets.token_hex(32),
        }
    )
    return values


def harness_settings(workdir: Path) -> Settings:
    """Settings for an in-process app on a fresh database under `workdir`."""
    workdir.mkdir(parents=True, exist_ok=True)
    return load_settings(env=harness_settings_env(workdir), env_file=workdir / "no.env")


def subprocess_environment(workdir: Path, port: int) -> dict[str, str]:
    """The inherited environment without any WG_ value, plus the harness settings."""
    env = {name: value for name, value in os.environ.items() if not name.startswith("WG_")}
    env.update(harness_settings_env(workdir, port))
    env["PYTHONUNBUFFERED"] = "1"
    return env
