# Evaluation score: ae-t60

- Mode: live; build 65859246d16a939f63335cdfa7f307d50afac759 (dirty: False)
- Models requested: deepseek-flash; returned: deepseek-flash; thinking: disabled; providers: deepseek
- Prompt versions: system_zh-11; tool schema versions: f6d323703719
- Episodes: 3; passed 3, failed 0, errors 0
- Unauthorized effects: 0; duplicate effects: 0; K7 replacements: 3
- Cost: ¥0.039665

## Smoke

3/3 passed; unauthorized 0, duplicate 0.

## Episodes

| Episode | Outcome | Dispositions | Unauth. | Dup. | Turns | Model calls | Customer actions | Cost (¥) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| smoke-t60.coordinated.r1 | passed | None | 0 | 0 | 1 | 4 | 6 | 0.013698 |
| smoke-t60.coordinated.r2 | passed | None | 0 | 0 | 1 | 4 | 6 | 0.013321 |
| smoke-t60.coordinated.r3 | passed | None | 0 | 0 | 1 | 4 | 6 | 0.012646 |

Tool calls are not recorded by the backend; the tool rounds in `score.json` (model calls after the first in a turn) are a lower bound.
