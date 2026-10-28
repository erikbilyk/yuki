import secrets
from pathlib import Path

import pytest

SETTING_NAMES = (
    "WG_PORT",
    "WG_WEB_PORT",
    "WG_DB_PATH",
    "WG_DEMO_MODE",
    "WG_CODE_HMAC_KEY",
    "WG_SESSION_SECRET",
    "WG_LLM_PROVIDER",
    "WG_LLM_API_KEY",
    "WG_LLM_BASE_URL",
    "WG_LLM_MODEL",
    "WG_LLM_THINKING",
    "WG_LLM_TIMEOUT_S",
    "WG_LLM_PRICE_TABLE",
    "WG_LLM_SPEND_CAP_CNY",
    "WG_LLM_STUB_SCRIPT",
    "WG_OUTBOUND_PROXY",
    "WG_FAULT_WAIT_FILE",
    "WG_CHANNEL_SECRET",
    "WG_FEISHU_APP_ID",
    "WG_FEISHU_APP_SECRET",
    "WG_BACKEND_URL",
    "WG_WEB_BASE_URL",
)


@pytest.fixture(scope="session", autouse=True)
def session_environment(tmp_path_factory: pytest.TempPathFactory):
    """Give the test session its own database path, demo mode, stub model and secrets."""
    work = tmp_path_factory.mktemp("wg")
    overrides = {
        "WG_DB_PATH": str(work / "wg.db"),
        "WG_FAULT_WAIT_FILE": str(work / "fault.wait"),
        "WG_DEMO_MODE": "1",
        "WG_LLM_PROVIDER": "stub",
        "WG_CODE_HMAC_KEY": secrets.token_hex(32),
        "WG_SESSION_SECRET": secrets.token_hex(32),
        "WG_CHANNEL_SECRET": secrets.token_hex(32),
    }
    mp = pytest.MonkeyPatch()
    for name in SETTING_NAMES:
        mp.delenv(name, raising=False)
    for name, value in overrides.items():
        mp.setenv(name, value)
    yield Path(overrides["WG_DB_PATH"])
    mp.undo()
