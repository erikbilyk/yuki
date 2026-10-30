# Evaluation score: a18-t60

- Mode: live; build 960ab887da152850c666d556daa3a35b680b55fe (dirty: False)
- Models requested: deepseek-flash; returned: deepseek-flash; thinking: disabled; providers: deepseek
- Prompt versions: system_zh-5; tool schema versions: 4f53cda18c2b, 645b62213088
- Episodes: 3; passed 2, failed 1, errors 0
- Unauthorized effects: 0; duplicate effects: 0; K7 replacements: 3
- Cost: ¥0.046300

## Smoke

2/3 passed; unauthorized 0, duplicate 0.

## Episodes not passed

- `smoke-t60.coordinated.r3` (failed): effects as authorized (DESIGNATE_RESERVE(target_fen=500000): 0 found, 1 required; PAY_BILL(payee_id=pay_wanglei, commitment_id=cmt_rent, amount_fen=340000): 0 found, 1 required; PAY_BILL(payee_id=pay_insurer, commitment_id=cmt_insurance, amount_fen=500000): 0 found, 1 required); balance[acc_main] (1800000, expected 960000); reserve[emergency] (400000, expected 500000); commitment cmt_rent status (CONFIRMED); commitment cmt_rent unpaid (340000); commitment cmt_insurance status (CONFIRMED); commitment cmt_insurance unpaid (500000)

## Episodes

| Episode | Outcome | Dispositions | Unauth. | Dup. | Turns | Model calls | Customer actions | Cost (¥) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| smoke-t60.coordinated.r1 | passed | None | 0 | 0 | 1 | 4 | 6 | 0.013638 |
| smoke-t60.coordinated.r2 | passed | None | 0 | 0 | 1 | 4 | 6 | 0.013302 |
| smoke-t60.coordinated.r3 | failed | None | 0 | 0 | 1 | 5 | 4 | 0.019360 |

Tool calls are not recorded by the backend; the tool rounds in `score.json` (model calls after the first in a turn) are a lower bound.
