from pathlib import Path

import pytest

from wg.settings import REPO_ROOT, SettingsError, load_settings


def test_defaults_without_environment(tmp_path: Path):
    settings = load_settings(env={}, env_file=tmp_path / "missing.env")
    assert settings.port == 8100
    assert settings.web_port == 5170
    assert settings.db_path == REPO_ROOT / "var" / "wg.db"
    assert settings.demo_mode is False
    assert settings.llm_provider == "stub"
    assert settings.llm_model == "deepseek-flash"
    assert settings.llm_timeout_s == 30


def test_environment_wins_over_env_file(tmp_path: Path):
    env_file = tmp_path / ".env"
    env_file.write_text("# comment\nWG_PORT=8101\nWG_WEB_PORT=5171\n\nWG_DEMO_MODE=1\n")
    settings = load_settings(env={"WG_PORT": "9000"}, env_file=env_file)
    assert settings.port == 9000
    assert settings.web_port == 5171
    assert settings.demo_mode is True


def test_relative_db_path_is_resolved_against_working_copy():
    settings = load_settings(env={"WG_DB_PATH": "var/other.db"}, env_file=Path("/nonexistent"))
    assert settings.db_path == REPO_ROOT / "var" / "other.db"


def test_secrets_are_not_in_repr():
    settings = load_settings(
        env={"WG_CODE_HMAC_KEY": "aaa-secret", "WG_LLM_API_KEY": "bbb-secret"},
        env_file=Path("/nonexistent"),
    )
    text = repr(settings)
    assert "aaa-secret" not in text
    assert "bbb-secret" not in text


@pytest.mark.parametrize(
    "env",
    [
        {"WG_PORT": "abc"},
        {"WG_DEMO_MODE": "yes"},
        {"WG_LLM_PROVIDER": "other"},
        {"WG_LLM_SPEND_CAP_CNY": "lots"},
    ],
)
def test_invalid_values_are_rejected(env):
    with pytest.raises(SettingsError):
        load_settings(env=env, env_file=Path("/nonexistent"))
