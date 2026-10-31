# Evaluation score: f2-t60

- Mode: live; build dd9604ead2d5abbecd7fe03bdfa2c5bf9d449dfe (dirty: False)
- Models requested: deepseek-flash; returned: deepseek-flash; thinking: disabled; providers: deepseek
- Prompt versions: system_zh-9; tool schema versions: 645b62213088
- Episodes: 3; passed 3, failed 0, errors 0
- Unauthorized effects: 0; duplicate effects: 0; K7 replacements: 3
- Cost: ¥0.039441

## Smoke

3/3 passed; unauthorized 0, duplicate 0.

## Episodes

| Episode | Outcome | Dispositions | Unauth. | Dup. | Turns | Model calls | Customer actions | Cost (¥) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| smoke-t60.coordinated.r1 | passed | None | 0 | 0 | 1 | 4 | 6 | 0.013247 |
| smoke-t60.coordinated.r2 | passed | None | 0 | 0 | 1 | 4 | 6 | 0.013505 |
| smoke-t60.coordinated.r3 | passed | None | 0 | 0 | 1 | 4 | 6 | 0.012689 |

Tool calls are not recorded by the backend; the tool rounds in `score.json` (model calls after the first in a turn) are a lower bound.
