"""Runtime settings read from the environment and the working copy's .env file."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

LLM_PROVIDERS = ("deepseek", "stub")


class SettingsError(ValueError):
    """A setting is missing, empty where a value is required, or malformed."""


@dataclass(frozen=True)
class Settings:
    port: int = 8100
    web_port: int = 5170
    db_path: Path = REPO_ROOT / "var" / "wg.db"
    demo_mode: bool = False
    llm_provider: str = "stub"
    llm_base_url: str = "https://api.deepseek.com"
    llm_model: str = "deepseek-flash"
    llm_thinking: str = "disabled"
    llm_timeout_s: float = 30.0
    llm_price_table: str = "0.04/2/8"
    llm_spend_cap_cny: Decimal = Decimal("50")
    llm_stub_script: str = ""
    fault_wait_file: Path = REPO_ROOT / "var" / "fault.wait"
    backend_url: str = "http://127.0.0.1:8100"
    web_base_url: str = "http://127.0.0.1:8100"
    feishu_app_id: str = ""
    code_hmac_key: str = field(default="", repr=False)
    session_secret: str = field(default="", repr=False)
    llm_api_key: str = field(default="", repr=False)
    outbound_proxy: str = field(default="", repr=False)
    channel_secret: str = field(default="", repr=False)
    feishu_app_secret: str = field(default="", repr=False)

    def require_secrets(self) -> None:
        """Refuse to start with an empty code key or session secret."""
        missing = [
            name
            for name, value in (
                ("WG_CODE_HMAC_KEY", self.code_hmac_key),
                ("WG_SESSION_SECRET", self.session_secret),
            )
            if not value
        ]
        if missing:
            raise SettingsError(f"required secret is empty: {', '.join(missing)}")


def read_env_file(path: Path) -> dict[str, str]:
    """Parse NAME=value lines; blank lines and # comments are skipped."""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        values[name.strip()] = value.strip().strip("'\"")
    return values


def load_settings(env: Mapping[str, str] | None = None, env_file: Path | None = None) -> Settings:
    """Build settings; values in `env` (default os.environ) win over the .env file."""
    merged = read_env_file(REPO_ROOT / ".env" if env_file is None else env_file)
    merged.update(os.environ if env is None else env)

    def text(name: str, default: str) -> str:
        return merged.get(name) or default

    def number(name: str, default: int) -> int:
        raw = merged.get(name) or str(default)
        try:
            return int(raw)
        except ValueError:
            raise SettingsError(f"{name} must be an integer") from None

    def path(name: str, default: Path) -> Path:
        raw = merged.get(name)
        if not raw:
            return default
        return Path(raw) if Path(raw).is_absolute() else REPO_ROOT / raw

    provider = text("WG_LLM_PROVIDER", "stub")
    if provider not in LLM_PROVIDERS:
        raise SettingsError(f"WG_LLM_PROVIDER must be one of {', '.join(LLM_PROVIDERS)}")
    demo = merged.get("WG_DEMO_MODE", "0") or "0"
    if demo not in ("0", "1"):
        raise SettingsError("WG_DEMO_MODE must be 0 or 1")
    try:
        timeout = float(text("WG_LLM_TIMEOUT_S", "30"))
        spend_cap = Decimal(text("WG_LLM_SPEND_CAP_CNY", "50"))
    except (ValueError, InvalidOperation):
        raise SettingsError("WG_LLM_TIMEOUT_S and WG_LLM_SPEND_CAP_CNY must be numbers") from None

    return Settings(
        port=number("WG_PORT", 8100),
        web_port=number("WG_WEB_PORT", 5170),
        db_path=path("WG_DB_PATH", REPO_ROOT / "var" / "wg.db"),
        demo_mode=demo == "1",
        llm_provider=provider,
        llm_base_url=text("WG_LLM_BASE_URL", "https://api.deepseek.com"),
        llm_model=text("WG_LLM_MODEL", "deepseek-flash"),
        llm_thinking=text("WG_LLM_THINKING", "disabled"),
        llm_timeout_s=timeout,
        llm_price_table=text("WG_LLM_PRICE_TABLE", "0.04/2/8"),
        llm_spend_cap_cny=spend_cap,
        llm_stub_script=merged.get("WG_LLM_STUB_SCRIPT", ""),
        fault_wait_file=path("WG_FAULT_WAIT_FILE", REPO_ROOT / "var" / "fault.wait"),
        backend_url=text("WG_BACKEND_URL", "http://127.0.0.1:8100"),
        web_base_url=text("WG_WEB_BASE_URL", "http://127.0.0.1:8100"),
        feishu_app_id=merged.get("WG_FEISHU_APP_ID", ""),
        code_hmac_key=merged.get("WG_CODE_HMAC_KEY", ""),
        session_secret=merged.get("WG_SESSION_SECRET", ""),
        llm_api_key=merged.get("WG_LLM_API_KEY", ""),
        outbound_proxy=merged.get("WG_OUTBOUND_PROXY", ""),
        channel_secret=merged.get("WG_CHANNEL_SECRET", ""),
        feishu_app_secret=merged.get("WG_FEISHU_APP_SECRET", ""),
    )
