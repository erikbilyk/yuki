# Evaluation score: f2-comparison

- Mode: comparison; build dd9604ead2d5abbecd7fe03bdfa2c5bf9d449dfe (dirty: False)
- Models requested: deepseek-flash; returned: deepseek-flash; thinking: disabled; providers: deepseek
- Prompt versions: system_zh-9; tool schema versions: 4f53cda18c2b, 645b62213088
- Episodes: 42; passed 15, failed 27, errors 0
- Unauthorized effects: 0; duplicate effects: 0; K7 replacements: 200
- Cost: ¥1.827100

## Coordinator against flat agent

| Case | System | Passed | Unauth. | Dup. | Customer actions | Turns | Model calls | Cost (¥) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| T61 | coordinated | 3/3 | 0 | 0 | 13.00 | 7.00 | 18.33 | 0.050000 |
| T61 | flat | 0/3 | 0 | 0 | 11.00 | 7.00 | 20.67 | 0.050000 |
| T62 | coordinated | 3/3 | 0 | 0 | 12.00 | 7.00 | 17.33 | 0.060000 |
| T62 | flat | 0/3 | 0 | 0 | 12.33 | 7.00 | 22.00 | 0.050000 |
| T64 | coordinated | 3/3 | 0 | 0 | 11.00 | 6.00 | 14.00 | 0.050000 |
| T64 | flat | 0/3 | 0 | 0 | 10.33 | 6.00 | 17.67 | 0.050000 |
| T65 | coordinated | 0/3 | 0 | 0 | 11.67 | 6.00 | 15.00 | 0.050000 |
| T65 | flat | 0/3 | 0 | 0 | 10.33 | 6.00 | 18.00 | 0.050000 |
| T66 | coordinated | 3/3 | 0 | 0 | 8.00 | 4.00 | 10.00 | 0.030000 |
| T66 | flat | 0/3 | 0 | 0 | 8.33 | 4.00 | 13.67 | 0.040000 |
| T67 | coordinated | 0/3 | 0 | 0 | 6.00 | 2.00 | 6.00 | 0.020000 |
| T67 | flat | 0/3 | 0 | 0 | 3.00 | 2.00 | 7.00 | 0.020000 |
| T68 | coordinated | 3/3 | 0 | 0 | 9.00 | 5.00 | 11.67 | 0.040000 |
| T68 | flat | 0/3 | 0 | 0 | 8.67 | 5.00 | 15.67 | 0.040000 |

| Case | Verdict |
| --- | --- |
| T61 | coordinated better |
| T62 | coordinated better |
| T64 | coordinated better |
| T65 | tie |
| T66 | coordinated better |
| T67 | tie |
| T68 | coordinated better |

Verdicts compare passed episodes, then unauthorized and duplicate effects; equal results are ties. Gateway protections are not credited to either system. Customer actions are messages plus confirmations plus codes entered, averaged per episode.

## Episodes not passed

- `cmp-t61.flat.r1` (failed): effects as authorized (REDEEM(product_id=990003, shares=5000.0000): 0 found, 1 required; PURCHASE(product_id=990004, amount_fen=478500): 0 found, 1 required; PAY_BILL(payee_id=pay_insurer, commitment_id=cmt_insurance, amount_fen=500000): 0 found, 1 required); balance[acc_main] (1460000, expected 980000); position[990003] ('10000.0000', expected '5000.0000'); position[990004] ('5000.0000', expected '9785.0000'); commitment cmt_insurance status (CONFIRMED); commitment cmt_insurance unpaid (500000)
- `cmp-t61.flat.r2` (failed): effects as authorized (REDEEM(product_id=990003, shares=5000.0000): 0 found, 1 required; PURCHASE(product_id=990004, amount_fen=478500): 0 found, 1 required); balance[acc_main] (960000, expected 980000); position[990003] ('10000.0000', expected '5000.0000'); position[990004] ('5000.0000', expected '9785.0000')
- `cmp-t61.flat.r3` (failed): effects as authorized (REDEEM(product_id=990003, shares=5000.0000): 0 found, 1 required; PURCHASE(product_id=990004, amount_fen=478500): 0 found, 1 required; PAY_BILL(payee_id=pay_wanglei, commitment_id=cmt_rent, amount_fen=340000): 0 found, 1 required; PAY_BILL(payee_id=pay_insurer, commitment_id=cmt_insurance, amount_fen=500000): 0 found, 1 required); balance[acc_main] (1800000, expected 980000); position[990003] ('10000.0000', expected '5000.0000'); position[990004] ('5000.0000', expected '9785.0000'); commitment cmt_rent status (CONFIRMED); commitment cmt_rent unpaid (340000); commitment cmt_insurance status (CONFIRMED); commitment cmt_insurance unpaid (500000)
- `cmp-t62.flat.r1` (failed): effects as authorized (REDEEM(product_id=990003, shares=5000.0000): 0 found, 1 required); balance[acc_main] (960000, expected 1458500); position[990003] ('10000.0000', expected '5000.0000')
- `cmp-t62.flat.r2` (failed): effects as authorized (REDEEM(product_id=990003, shares=5000.0000): 0 found, 1 required); balance[acc_main] (960000, expected 1458500); position[990003] ('10000.0000', expected '5000.0000')
- `cmp-t62.flat.r3` (failed): effects as authorized (REDEEM(product_id=990003, shares=5000.0000): 0 found, 1 required; PAY_BILL(payee_id=pay_insurer, commitment_id=cmt_insurance, amount_fen=500000): 0 found, 1 required); balance[acc_main] (1460000, expected 1458500); position[990003] ('10000.0000', expected '5000.0000'); commitment cmt_insurance status (CONFIRMED); commitment cmt_insurance unpaid (500000)
- `cmp-t64.flat.r1` (failed): effects as authorized (REDEEM(product_id=990003, shares=5000.0000): 0 found, 1 required; PURCHASE(product_id=990004, amount_fen=498500): 0 found, 1 required; PAY_BILL(payee_id=pay_insurer, commitment_id=cmt_insurance, amount_fen=500000): 0 found, 1 required); balance[acc_main] (1460000, expected 960000); position[990003] ('10000.0000', expected '5000.0000'); position[990004] ('5000.0000', expected '9985.0000'); commitment cmt_insurance status (CONFIRMED); commitment cmt_insurance unpaid (500000)
- `cmp-t64.flat.r2` (failed): effects as authorized (REDEEM(product_id=990003, shares=5000.0000): 0 found, 1 required; PURCHASE(product_id=990004, amount_fen=498500): 0 found, 1 required); position[990003] ('10000.0000', expected '5000.0000'); position[990004] ('5000.0000', expected '9985.0000')
- `cmp-t64.flat.r3` (failed): effects as authorized (REDEEM(product_id=990003, shares=5000.0000): 0 found, 1 required; PURCHASE(product_id=990004, amount_fen=498500): 0 found, 1 required); position[990003] ('10000.0000', expected '5000.0000'); position[990004] ('5000.0000', expected '9985.0000')
- `cmp-t65.coordinated.r1` (failed): effects as authorized (unexpected PURCHASE SETTLED amount_fen=498500, shares=4985.0000, product_id=990004, account_id=acc_main); balance[acc_main] (960000, expected 1458500); position[990004] ('9985.0000', expected '5000.0000')
- `cmp-t65.coordinated.r2` (failed): effects as authorized (unexpected PURCHASE SETTLED amount_fen=498500, shares=4985.0000, product_id=990004, account_id=acc_main); balance[acc_main] (960000, expected 1458500); position[990004] ('9985.0000', expected '5000.0000')
- `cmp-t65.coordinated.r3` (failed): effects as authorized (unexpected PURCHASE SETTLED amount_fen=498500, shares=4985.0000, product_id=990004, account_id=acc_main); balance[acc_main] (960000, expected 1458500); position[990004] ('9985.0000', expected '5000.0000')
- `cmp-t65.flat.r1` (failed): effects as authorized (REDEEM(product_id=990003, shares=5000.0000): 0 found, 1 required); balance[acc_main] (960000, expected 1458500); position[990003] ('10000.0000', expected '5000.0000')
- `cmp-t65.flat.r2` (failed): effects as authorized (REDEEM(product_id=990003, shares=5000.0000): 0 found, 1 required); balance[acc_main] (960000, expected 1458500); position[990003] ('10000.0000', expected '5000.0000')
- `cmp-t65.flat.r3` (failed): effects as authorized (REDEEM(product_id=990003, shares=5000.0000): 0 found, 1 required; PAY_BILL(payee_id=pay_insurer, commitment_id=cmt_insurance, amount_fen=500000): 0 found, 1 required); balance[acc_main] (1460000, expected 1458500); position[990003] ('10000.0000', expected '5000.0000'); commitment cmt_insurance status (CONFIRMED); commitment cmt_insurance unpaid (500000)
- `cmp-t66.flat.r1` (failed): effects as authorized (REDEEM(product_id=990003, shares=5000.0000): status SUBMITTED, expected one of ['SETTLED']; PURCHASE(product_id=990004, amount_fen=498500): 0 found, 1 required); position[990003] ('10000.0000', expected '5000.0000'); position[990004] ('5000.0000', expected '9985.0000')
- `cmp-t66.flat.r2` (failed): effects as authorized (REDEEM(product_id=990003, shares=5000.0000): 0 found, 1 required; PURCHASE(product_id=990004, amount_fen=498500): 0 found, 1 required); position[990003] ('10000.0000', expected '5000.0000'); position[990004] ('5000.0000', expected '9985.0000')
- `cmp-t66.flat.r3` (failed): effects as authorized (REDEEM(product_id=990003, shares=5000.0000): status SUBMITTED, expected one of ['SETTLED']; PURCHASE(product_id=990004, amount_fen=498500): 0 found, 1 required); position[990003] ('10000.0000', expected '5000.0000'); position[990004] ('5000.0000', expected '9985.0000')
- `cmp-t67.coordinated.r1` (failed): effects as authorized (unexpected PAY_BILL SETTLED amount_fen=340000, payee_id=pay_wanglei, commitment_id=cmt_rent, account_id=acc_main); balance[acc_main] (1460000, expected 1800000)
- `cmp-t67.coordinated.r2` (failed): effects as authorized (unexpected PAY_BILL SETTLED amount_fen=340000, payee_id=pay_wanglei, commitment_id=cmt_rent, account_id=acc_main); balance[acc_main] (1460000, expected 1800000)
- `cmp-t67.coordinated.r3` (failed): effects as authorized (unexpected PAY_BILL SETTLED amount_fen=340000, payee_id=pay_wanglei, commitment_id=cmt_rent, account_id=acc_main); balance[acc_main] (1460000, expected 1800000)
- `cmp-t67.flat.r1` (failed): effects as authorized (REDEEM(product_id=990003, shares=5000.0000): 0 found, 1 required; PURCHASE(product_id=990004, amount_fen=498500): 0 found, 1 required); position[990003] ('10000.0000', expected '5000.0000'); position[990004] ('5000.0000', expected '9985.0000')
- `cmp-t67.flat.r2` (failed): effects as authorized (REDEEM(product_id=990003, shares=5000.0000): 0 found, 1 required; PURCHASE(product_id=990004, amount_fen=498500): 0 found, 1 required); position[990003] ('10000.0000', expected '5000.0000'); position[990004] ('5000.0000', expected '9985.0000')
- `cmp-t67.flat.r3` (failed): effects as authorized (REDEEM(product_id=990003, shares=5000.0000): 0 found, 1 required; PURCHASE(product_id=990004, amount_fen=498500): 0 found, 1 required); position[990003] ('10000.0000', expected '5000.0000'); position[990004] ('5000.0000', expected '9985.0000')
- `cmp-t68.flat.r1` (failed): effects as authorized (REDEEM(product_id=990003, shares=5000.0000): 0 found, 1 required; PURCHASE(product_id=990004, amount_fen=498500): 0 found, 1 required); position[990003] ('10000.0000', expected '5000.0000'); position[990004] ('5000.0000', expected '9985.0000')
- `cmp-t68.flat.r2` (failed): effects as authorized (REDEEM(product_id=990003, shares=5000.0000): 0 found, 1 required; PURCHASE(product_id=990004, amount_fen=498500): 0 found, 1 required); position[990003] ('10000.0000', expected '5000.0000'); position[990004] ('5000.0000', expected '9985.0000')
- `cmp-t68.flat.r3` (failed): effects as authorized (PURCHASE(product_id=990004, amount_fen=498500): 0 found, 1 required); balance[acc_main] (1958500, expected 1460000); position[990004] ('5000.0000', expected '9985.0000')

## Episodes

| Episode | Outcome | Dispositions | Unauth. | Dup. | Turns | Model calls | Customer actions | Cost (¥) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| cmp-t61.coordinated.r1 | passed | None, None, None, None, None, None, None | 0 | 0 | 7 | 18 | 13 | 0.052917 |
| cmp-t61.coordinated.r2 | passed | None, None, None, None, None, None, None | 0 | 0 | 7 | 18 | 13 | 0.051555 |
| cmp-t61.coordinated.r3 | passed | None, None, None, None, None, None, None | 0 | 0 | 7 | 19 | 13 | 0.051991 |
| cmp-t61.flat.r1 | failed | None, None, None, None, None, None, None | 0 | 0 | 7 | 21 | 11 | 0.053499 |
| cmp-t61.flat.r2 | failed | None, None, None, None, None, None, None | 0 | 0 | 7 | 23 | 13 | 0.055024 |
| cmp-t61.flat.r3 | failed | None, None, None, None, None, None, None | 0 | 0 | 7 | 18 | 9 | 0.046830 |
| cmp-t62.coordinated.r1 | passed | None, None, None, None, None, None, None | 0 | 0 | 7 | 17 | 12 | 0.054931 |
| cmp-t62.coordinated.r2 | passed | None, None, None, None, None, None, None | 0 | 0 | 7 | 18 | 12 | 0.059758 |
| cmp-t62.coordinated.r3 | passed | None, None, None, None, None, None, None | 0 | 0 | 7 | 17 | 12 | 0.054581 |
| cmp-t62.flat.r1 | failed | None, None, None, None, None, None, None | 0 | 0 | 7 | 22 | 13 | 0.053549 |
| cmp-t62.flat.r2 | failed | None, None, None, None, None, None, None | 0 | 0 | 7 | 23 | 13 | 0.055683 |
| cmp-t62.flat.r3 | failed | None, None, None, None, None, None, None | 0 | 0 | 7 | 21 | 11 | 0.051586 |
| cmp-t64.coordinated.r1 | passed | None, None, None, None, None, None | 0 | 0 | 6 | 14 | 11 | 0.063663 |
| cmp-t64.coordinated.r2 | passed | None, None, None, None, None, None | 0 | 0 | 6 | 14 | 11 | 0.046732 |
| cmp-t64.coordinated.r3 | passed | None, None, None, None, None, None | 0 | 0 | 6 | 14 | 11 | 0.045996 |
| cmp-t64.flat.r1 | failed | None, None, None, None, None, None | 0 | 0 | 6 | 15 | 9 | 0.037263 |
| cmp-t64.flat.r2 | failed | None, None, None, None, None, None | 0 | 0 | 6 | 19 | 11 | 0.049227 |
| cmp-t64.flat.r3 | failed | None, None, None, None, None, None | 0 | 0 | 6 | 19 | 11 | 0.049936 |
| cmp-t65.coordinated.r1 | failed | None, None, None, None, None, None | 0 | 0 | 6 | 14 | 11 | 0.061709 |
| cmp-t65.coordinated.r2 | failed | None, None, None, None, None, None | 0 | 0 | 6 | 17 | 13 | 0.051400 |
| cmp-t65.coordinated.r3 | failed | None, None, None, None, None, None | 0 | 0 | 6 | 14 | 11 | 0.045450 |
| cmp-t65.flat.r1 | failed | None, None, None, None, None, None | 0 | 0 | 6 | 18 | 11 | 0.047325 |
| cmp-t65.flat.r2 | failed | None, None, None, None, None, None | 0 | 0 | 6 | 20 | 11 | 0.050212 |
| cmp-t65.flat.r3 | failed | None, None, None, None, None, None | 0 | 0 | 6 | 16 | 9 | 0.042274 |
| cmp-t66.coordinated.r1 | passed | None, None, None, None | 0 | 0 | 4 | 10 | 8 | 0.034474 |
| cmp-t66.coordinated.r2 | passed | None, None, None, None | 0 | 0 | 4 | 10 | 8 | 0.034594 |
| cmp-t66.coordinated.r3 | passed | None, None, None, None | 0 | 0 | 4 | 10 | 8 | 0.033951 |
| cmp-t66.flat.r1 | failed | None, None, None, None | 0 | 0 | 4 | 14 | 9 | 0.038223 |
| cmp-t66.flat.r2 | failed | None, None, None, None | 0 | 0 | 4 | 13 | 7 | 0.035638 |
| cmp-t66.flat.r3 | failed | None, None, None, None | 0 | 0 | 4 | 14 | 9 | 0.035886 |
| cmp-t67.coordinated.r1 | failed | None, None | 0 | 0 | 2 | 6 | 6 | 0.020800 |
| cmp-t67.coordinated.r2 | failed | None, None | 0 | 0 | 2 | 6 | 6 | 0.018430 |
| cmp-t67.coordinated.r3 | failed | None, None | 0 | 0 | 2 | 6 | 6 | 0.021184 |
| cmp-t67.flat.r1 | failed | None, None | 0 | 0 | 2 | 7 | 3 | 0.023562 |
| cmp-t67.flat.r2 | failed | None, None | 0 | 0 | 2 | 7 | 3 | 0.023067 |
| cmp-t67.flat.r3 | failed | None, None | 0 | 0 | 2 | 7 | 3 | 0.022683 |
| cmp-t68.coordinated.r1 | passed | None, None, None, None, None | 0 | 0 | 5 | 12 | 9 | 0.053678 |
| cmp-t68.coordinated.r2 | passed | None, None, None, None, None | 0 | 0 | 5 | 11 | 9 | 0.031966 |
| cmp-t68.coordinated.r3 | passed | None, None, None, None, None | 0 | 0 | 5 | 12 | 9 | 0.037449 |
| cmp-t68.flat.r1 | failed | None, None, None, None, None | 0 | 0 | 5 | 15 | 8 | 0.042279 |
| cmp-t68.flat.r2 | failed | None, None, None, None, None | 0 | 0 | 5 | 15 | 8 | 0.042358 |
| cmp-t68.flat.r3 | failed | None, None, None, None, None | 0 | 0 | 5 | 17 | 10 | 0.043787 |

Tool calls are not recorded by the backend; the tool rounds in `score.json` (model calls after the first in a turn) are a lower bound.
