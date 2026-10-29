"""One tiny live call to the configured model: `uv run python -m wg.agent.smoke`.

Prints the requested and returned model, latency, token usage and the recorded spend.
It prints no key and no message text.
"""

from __future__ import annotations

import json
import sys

from wg.agent.llm import LlmError, ProviderError, build_adapter
from wg.api.app import resolve_build_id
from wg.settings import SettingsError, load_settings

SMOKE_PROMPT_VERSION = "smoke"
SMOKE_MESSAGES = [{"role": "user", "content": "只回复一个字：好"}]


def main() -> int:
    try:
        settings = load_settings()
    except SettingsError as error:
        print(f"settings: {error}")
        return 2
    if settings.llm_provider != "deepseek":
        print(f"WG_LLM_PROVIDER is {settings.llm_provider}; the smoke call needs deepseek")
        return 2
    try:
        adapter = build_adapter(settings, build_id=resolve_build_id())
        adapter.complete(SMOKE_MESSAGES, [], prompt_version=SMOKE_PROMPT_VERSION)
    except ProviderError as error:
        print(f"provider error: {error.reason}")
        if error.body:
            print(f"body: {error.body}")
        return 1
    except LlmError as error:
        print(f"model error: {error.reason}")
        return 1
    last = json.loads(adapter.log_path.read_text(encoding="utf-8").splitlines()[-1])
    print(f"requested model: {last['requested_model']}")
    print(f"returned model:  {last['returned_model']}")
    print(f"thinking:        {last['thinking']}")
    print(f"latency:         {last['latency_ms']} ms")
    print(f"usage:           {json.dumps(last['usage'])}")
    print(f"call cost:       CNY {last['cost_cny']}")
    print(f"spend so far:    CNY {adapter.recorded_spend()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
