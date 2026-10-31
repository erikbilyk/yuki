"""The system prompt keeps the text that was tested live and only adds the direct-debit section."""

from __future__ import annotations

import hashlib

import pytest

from wg.agent.prompt import PROMPT_DIR, load_system_prompt
from wg.contracts.tools import TOOLS_BY_NAME

pytestmark = pytest.mark.layer("U")

SECTION = "\n订阅与自动扣款\n"
# SHA-256 of the prompt body that the live scenarios were run against (version line excluded).
D7_BODY_SHA256 = "8762aab48bc4c7fd6ccd0e69e666c98374a8e49912e4d1bd4d53e2b935d1637b"
SUBSCRIPTION_TOOLS = (
    "list_mandates get_debit_requests get_subscription_spend "
    "draft_mandate_change draft_debit_decision"
).split()


def body_and_version() -> tuple[str, str]:
    raw = (PROMPT_DIR / "system_zh.md").read_text(encoding="utf-8")
    first, _, body = raw.partition("\n")
    return body, first


def test_the_text_before_the_added_section_is_the_one_tested_live():
    body, _ = body_and_version()
    before, found, _ = body.partition(SECTION)
    assert found, "the direct-debit section is missing"
    assert hashlib.sha256(before.encode("utf-8")).hexdigest() == D7_BODY_SHA256


def test_the_version_changed_with_the_added_section():
    _, first = body_and_version()
    assert first == "version: system_zh-11"
    assert load_system_prompt().version == "system_zh-11"


def test_the_section_is_last_and_short():
    body, _ = body_and_version()
    section = body.partition(SECTION)[2]
    lines = [line for line in section.splitlines() if line.strip()]
    assert lines and all(line.startswith("- ") for line in lines) and len(lines) <= 8
    assert "\n\n" not in section.strip()


def test_the_section_names_every_tool_once_and_the_intent():
    section = load_system_prompt().text.partition(SECTION.strip())[2]
    for name in SUBSCRIPTION_TOOLS:
        assert name in section and name in TOOLS_BY_NAME
    assert "subscriptions.manage" in section


def test_the_section_keeps_agreements_apart_from_the_service_contract():
    section = load_system_prompt().text.partition(SECTION.strip())[2]
    assert "关闭自动扣款" in section and "已保存，正在核实" in section
    assert "不说“取消订阅”" in section
    assert "description_raw" in section and "decision_quote" in section


def test_a_new_agreement_is_sent_to_the_app_and_has_no_tool():
    section = load_system_prompt().text.partition(SECTION.strip())[2]
    assert "应用里" in section and "不能起草" in section
    assert not [name for name in TOOLS_BY_NAME if name.startswith("draft_new_mandate")]
    assert not [name for name in TOOLS_BY_NAME if "mandate_create" in name]
