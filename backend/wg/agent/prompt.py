"""The system prompt and its version string, which every model call record carries."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

PROMPT_DIR = Path(__file__).parent / "prompts"
VERSION_PREFIX = "version:"


@dataclass(frozen=True)
class SystemPrompt:
    version: str
    text: str


def load_system_prompt(name: str = "system_zh") -> SystemPrompt:
    """Read `prompts/<name>.md`; its first line is `version: <id>`, the rest is the prompt."""
    raw = (PROMPT_DIR / f"{name}.md").read_text(encoding="utf-8")
    first, _, body = raw.partition("\n")
    if not first.startswith(VERSION_PREFIX):
        raise ValueError(f"{name}.md must start with a '{VERSION_PREFIX}' line")
    return SystemPrompt(version=first.removeprefix(VERSION_PREFIX).strip(), text=body.strip())
