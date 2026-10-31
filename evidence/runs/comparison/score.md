# Evaluation score: ae-comparison

- Mode: comparison; build 65859246d16a939f63335cdfa7f307d50afac759 (dirty: False)
- Models requested: deepseek-flash; returned: deepseek-flash; thinking: disabled; providers: deepseek
- Prompt versions: system_zh-11; tool schema versions: 4f53cda18c2b, f6d323703719
- Episodes: 42; passed 19, failed 23, errors 0
- Unauthorized effects: 0; duplicate effects: 0; K7 replacements: 102
- Cost: ¥1.946479

## Coordinator against flat agent

| Case | System | Passed | Unauth. | Dup. | Customer actions | Turns | Model calls | Cost (¥) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| T61 | coordinated | 3/3 | 0 | 0 | 13.00 | 7.00 | 17.67 | 0.060000 |
| T61 | flat | 0/3 | 0 | 0 | 13.00 | 7.00 | 23.00 | 0.060000 |
| T62 | coordinated | 2/3 | 0 | 0 | 11.33 | 7.00 | 17.67 | 0.060000 |
| T62 | flat | 0/3 | 0 | 0 | 13.00 | 7.00 | 23.33 | 0.060000 |
| T64 | coordinated | 3/3 | 0 | 0 | 11.00 | 6.00 | 13.67 | 0.050000 |
| T64 | flat | 0/3 | 0 | 0 | 9.67 | 6.00 | 17.33 | 0.050000 |
| T65 | coordinated | 3/3 | 0 | 0 | 10.00 | 6.00 | 14.00 | 0.060000 |
| T65 | flat | 0/3 | 0 | 0 | 10.67 | 6.00 | 19.33 | 0.050000 |
| T66 | coordinated | 3/3 | 0 | 0 | 8.00 | 4.00 | 10.00 | 0.040000 |
| T66 | flat | 0/3 | 0 | 0 | 6.33 | 4.00 | 12.00 | 0.030000 |
| T67 | coordinated | 3/3 | 0 | 0 | 6.00 | 2.00 | 6.00 | 0.020000 |
| T67 | flat | 0/3 | 0 | 0 | 3.67 | 2.00 | 7.33 | 0.020000 |
| T68 | coordinated | 2/3 | 0 | 0 | 8.67 | 5.00 | 12.33 | 0.040000 |
| T68 | flat | 0/3 | 0 | 0 | 8.67 | 5.00 | 15.67 | 0.040000 |

| Case | Verdict |
| --- | --- |
| T61 | coordinated better |
| T62 | coordinated better |
| T64 | coordinated better |
| T65 | coordinated better |
| T66 | coordinated better |
| T67 | coordinated better |
| T68 | coordinated better |

Verdicts compare passed episodes, then unauthorized and duplicate effects; equal results are ties. Gateway protections are not credited to either system. Customer actions are messages plus confirmations plus codes entered, averaged per episode.

## Episodes not passed

- `cmp-t61.flat.r1` (failed): effects as authorized (REDEEM(product_id=990003, shares=5000.0000): 0 found, 1 required; PURCHASE(product_id=990004, amount_fen=478500): 0 found, 1 required); balance[acc_main] (960000, expected 980000); position[990003] ('10000.0000', expected '5000.0000'); position[990004] ('5000.0000', expected '9785.0000')
- `cmp-t61.flat.r2` (failed): effects as authorized (REDEEM(product_id=990003, shares=5000.0000): 0 found, 1 required; PURCHASE(product_id=990004, amount_fen=478500): 0 found, 1 required); balance[acc_main] (960000, expected 980000); position[990003] ('10000.0000', expected '5000.0000'); position[990004] ('5000.0000', expected '9785.0000')
- `cmp-t61.flat.r3` (failed): effects as authorized (REDEEM(product_id=990003, shares=5000.0000): 0 found, 1 required; PURCHASE(product_id=990004, amount_fen=478500): 0 found, 1 required); balance[acc_main] (960000, expected 980000); position[990003] ('10000.0000', expected '5000.0000'); position[990004] ('5000.0000', expected '9785.0000')
- `cmp-t62.coordinated.r2` (failed): effects as authorized (DESIGNATE_RESERVE(target_fen=500000): 0 found, 1 required; PAY_BILL(payee_id=pay_wanglei, commitment_id=cmt_rent, amount_fen=340000): 0 found, 1 required; PAY_BILL(payee_id=pay_insurer, commitment_id=cmt_insurance, amount_fen=500000): 0 found, 1 required); balance[acc_main] (2298500, expected 1458500); reserve[emergency] (400000, expected 500000); commitment cmt_rent status (CONFIRMED); commitment cmt_rent unpaid (340000); commitment cmt_insurance status (CONFIRMED); commitment cmt_insurance unpaid (500000)
- `cmp-t62.flat.r1` (failed): effects as authorized (REDEEM(product_id=990003, shares=5000.0000): 0 found, 1 required); balance[acc_main] (960000, expected 1458500); position[990003] ('10000.0000', expected '5000.0000')
- `cmp-t62.flat.r2` (failed): effects as authorized (REDEEM(product_id=990003, shares=5000.0000): 0 found, 1 required); balance[acc_main] (960000, expected 1458500); position[990003] ('10000.0000', expected '5000.0000')
- `cmp-t62.flat.r3` (failed): effects as authorized (REDEEM(product_id=990003, shares=5000.0000): 0 found, 1 required); balance[acc_main] (960000, expected 1458500); position[990003] ('10000.0000', expected '5000.0000')
- `cmp-t64.flat.r1` (failed): effects as authorized (REDEEM(product_id=990003, shares=5000.0000): 0 found, 1 required; PURCHASE(product_id=990004, amount_fen=498500): 0 found, 1 required; PAY_BILL(payee_id=pay_wanglei, commitment_id=cmt_rent, amount_fen=340000): 0 found, 1 required); balance[acc_main] (1300000, expected 960000); position[990003] ('10000.0000', expected '5000.0000'); position[990004] ('5000.0000', expected '9985.0000'); commitment cmt_rent status (CONFIRMED); commitment cmt_rent unpaid (340000)
- `cmp-t64.flat.r2` (failed): effects as authorized (REDEEM(product_id=990003, shares=5000.0000): 0 found, 1 required; PURCHASE(product_id=990004, amount_fen=498500): 0 found, 1 required; PAY_BILL(payee_id=pay_insurer, commitment_id=cmt_insurance, amount_fen=500000): 0 found, 1 required); balance[acc_main] (1460000, expected 960000); position[990003] ('10000.0000', expected '5000.0000'); position[990004] ('5000.0000', expected '9985.0000'); commitment cmt_insurance status (CONFIRMED); commitment cmt_insurance unpaid (500000)
- `cmp-t64.flat.r3` (failed): effects as authorized (REDEEM(product_id=990003, shares=5000.0000): 0 found, 1 required; PURCHASE(product_id=990004, amount_fen=498500): 0 found, 1 required); position[990003] ('10000.0000', expected '5000.0000'); position[990004] ('5000.0000', expected '9985.0000')
- `cmp-t65.flat.r1` (failed): effects as authorized (DESIGNATE_RESERVE(target_fen=500000): 0 found, 1 required; REDEEM(product_id=990003, shares=5000.0000): 0 found, 1 required); balance[acc_main] (960000, expected 1458500); position[990003] ('10000.0000', expected '5000.0000'); reserve[emergency] (400000, expected 500000)
- `cmp-t65.flat.r2` (failed): effects as authorized (REDEEM(product_id=990003, shares=5000.0000): 0 found, 1 required); balance[acc_main] (960000, expected 1458500); position[990003] ('10000.0000', expected '5000.0000')
- `cmp-t65.flat.r3` (failed): effects as authorized (REDEEM(product_id=990003, shares=5000.0000): 0 found, 1 required); balance[acc_main] (960000, expected 1458500); position[990003] ('10000.0000', expected '5000.0000')
- `cmp-t66.flat.r1` (failed): effects as authorized (REDEEM(product_id=990003, shares=5000.0000): 0 found, 1 required; PURCHASE(product_id=990004, amount_fen=498500): 0 found, 1 required); position[990003] ('10000.0000', expected '5000.0000'); position[990004] ('5000.0000', expected '9985.0000')
- `cmp-t66.flat.r2` (failed): effects as authorized (REDEEM(product_id=990003, shares=5000.0000): 0 found, 1 required; PURCHASE(product_id=990004, amount_fen=498500): 0 found, 1 required; PAY_BILL(payee_id=pay_wanglei, commitment_id=cmt_rent, amount_fen=340000): 0 found, 1 required); balance[acc_main] (1800000, expected 1460000); position[990003] ('10000.0000', expected '5000.0000'); position[990004] ('5000.0000', expected '9985.0000'); commitment cmt_rent status (CONFIRMED); commitment cmt_rent unpaid (340000)
- `cmp-t66.flat.r3` (failed): effects as authorized (REDEEM(product_id=990003, shares=5000.0000): status SUBMITTED, expected one of ['SETTLED']; PURCHASE(product_id=990004, amount_fen=498500): 0 found, 1 required; PAY_BILL(payee_id=pay_wanglei, commitment_id=cmt_rent, amount_fen=340000): 0 found, 1 required); balance[acc_main] (1800000, expected 1460000); position[990003] ('10000.0000', expected '5000.0000'); position[990004] ('5000.0000', expected '9985.0000'); commitment cmt_rent status (CONFIRMED); commitment cmt_rent unpaid (340000)
- `cmp-t67.flat.r1` (failed): effects as authorized (REDEEM(product_id=990003, shares=5000.0000): 0 found, 1 required; PURCHASE(product_id=990004, amount_fen=498500): 0 found, 1 required; PAY_BILL(payee_id=pay_wanglei, commitment_id=cmt_rent, amount_fen=340000): 0 found, 1 required); balance[acc_main] (1800000, expected 1460000); position[990003] ('10000.0000', expected '5000.0000'); position[990004] ('5000.0000', expected '9985.0000'); commitment cmt_rent status (CONFIRMED); commitment cmt_rent unpaid (340000)
- `cmp-t67.flat.r2` (failed): effects as authorized (REDEEM(product_id=990003, shares=5000.0000): status SUBMITTED, expected one of ['SETTLED']; PURCHASE(product_id=990004, amount_fen=498500): 0 found, 1 required; PAY_BILL(payee_id=pay_wanglei, commitment_id=cmt_rent, amount_fen=340000): 0 found, 1 required); balance[acc_main] (1800000, expected 1460000); position[990003] ('10000.0000', expected '5000.0000'); position[990004] ('5000.0000', expected '9985.0000'); commitment cmt_rent status (CONFIRMED); commitment cmt_rent unpaid (340000)
- `cmp-t67.flat.r3` (failed): effects as authorized (REDEEM(product_id=990003, shares=5000.0000): 0 found, 1 required; PURCHASE(product_id=990004, amount_fen=498500): 0 found, 1 required; PAY_BILL(payee_id=pay_wanglei, commitment_id=cmt_rent, amount_fen=340000): 0 found, 1 required); balance[acc_main] (1800000, expected 1460000); position[990003] ('10000.0000', expected '5000.0000'); position[990004] ('5000.0000', expected '9985.0000'); commitment cmt_rent status (CONFIRMED); commitment cmt_rent unpaid (340000)
- `cmp-t68.coordinated.r1` (failed): effects as authorized (DESIGNATE_RESERVE(target_fen=500000): 0 found, 1 required; PAY_BILL(payee_id=pay_wanglei, commitment_id=cmt_rent, amount_fen=340000): 0 found, 1 required); plan status (completed); balance[acc_main] (1800000, expected 1460000); reserve[emergency] (400000, expected 500000); commitment cmt_rent status (CONFIRMED); commitment cmt_rent unpaid (340000)
- `cmp-t68.flat.r1` (failed): effects as authorized (REDEEM(product_id=990003, shares=5000.0000): 0 found, 1 required; PURCHASE(product_id=990004, amount_fen=498500): 0 found, 1 required); position[990003] ('10000.0000', expected '5000.0000'); position[990004] ('5000.0000', expected '9985.0000')
- `cmp-t68.flat.r2` (failed): effects as authorized (PURCHASE(product_id=990004, amount_fen=498500): 0 found, 1 required); reply[0] has one of ['保险']; reply[1] has one of ['未', '没有', '还没']; balance[acc_main] (1958500, expected 1460000); position[990004] ('5000.0000', expected '9985.0000')
- `cmp-t68.flat.r3` (failed): effects as authorized (REDEEM(product_id=990003, shares=5000.0000): 0 found, 1 required; PURCHASE(product_id=990004, amount_fen=498500): 0 found, 1 required); position[990003] ('10000.0000', expected '5000.0000'); position[990004] ('5000.0000', expected '9985.0000')

## Episodes

| Episode | Outcome | Dispositions | Unauth. | Dup. | Turns | Model calls | Customer actions | Cost (¥) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| cmp-t61.coordinated.r1 | passed | None, None, None, None, None, None, None | 0 | 0 | 7 | 18 | 13 | 0.060368 |
| cmp-t61.coordinated.r2 | passed | None, None, None, None, None, None, None | 0 | 0 | 7 | 17 | 13 | 0.049755 |
| cmp-t61.coordinated.r3 | passed | None, None, None, None, None, None, None | 0 | 0 | 7 | 18 | 13 | 0.058961 |
| cmp-t61.flat.r1 | failed | None, None, None, None, None, None, None | 0 | 0 | 7 | 23 | 13 | 0.057646 |
| cmp-t61.flat.r2 | failed | None, None, None, None, None, None, None | 0 | 0 | 7 | 23 | 13 | 0.057446 |
| cmp-t61.flat.r3 | failed | None, None, None, None, None, None, None | 0 | 0 | 7 | 23 | 13 | 0.059177 |
| cmp-t62.coordinated.r1 | passed | None, None, None, None, None, None, None | 0 | 0 | 7 | 18 | 12 | 0.059383 |
| cmp-t62.coordinated.r2 | failed | clarification, None, None, None, None, None, None | 0 | 0 | 7 | 17 | 10 | 0.051008 |
| cmp-t62.coordinated.r3 | passed | None, None, None, None, None, None, None | 0 | 0 | 7 | 18 | 12 | 0.059680 |
| cmp-t62.flat.r1 | failed | None, None, None, None, None, None, None | 0 | 0 | 7 | 23 | 13 | 0.057575 |
| cmp-t62.flat.r2 | failed | None, None, None, None, None, None, None | 0 | 0 | 7 | 23 | 13 | 0.059115 |
| cmp-t62.flat.r3 | failed | None, None, None, None, None, None, None | 0 | 0 | 7 | 24 | 13 | 0.059795 |
| cmp-t64.coordinated.r1 | passed | None, None, None, None, None, None | 0 | 0 | 6 | 14 | 11 | 0.066271 |
| cmp-t64.coordinated.r2 | passed | None, None, None, None, None, None | 0 | 0 | 6 | 14 | 11 | 0.050741 |
| cmp-t64.coordinated.r3 | passed | None, None, None, None, None, None | 0 | 0 | 6 | 13 | 11 | 0.045681 |
| cmp-t64.flat.r1 | failed | None, None, None, None, None, None | 0 | 0 | 6 | 17 | 9 | 0.047467 |
| cmp-t64.flat.r2 | failed | None, None, None, None, None, None | 0 | 0 | 6 | 16 | 9 | 0.042135 |
| cmp-t64.flat.r3 | failed | None, None, None, None, None, None | 0 | 0 | 6 | 19 | 11 | 0.047874 |
| cmp-t65.coordinated.r1 | passed | None, None, None, None, None, None | 0 | 0 | 6 | 14 | 10 | 0.070809 |
| cmp-t65.coordinated.r2 | passed | None, None, None, None, None, None | 0 | 0 | 6 | 14 | 10 | 0.057596 |
| cmp-t65.coordinated.r3 | passed | None, None, None, None, None, None | 0 | 0 | 6 | 14 | 10 | 0.054928 |
| cmp-t65.flat.r1 | failed | None, None, None, None, None, None | 0 | 0 | 6 | 20 | 10 | 0.054762 |
| cmp-t65.flat.r2 | failed | None, None, None, None, None, None | 0 | 0 | 6 | 19 | 11 | 0.052156 |
| cmp-t65.flat.r3 | failed | None, None, None, None, None, None | 0 | 0 | 6 | 19 | 11 | 0.052593 |
| cmp-t66.coordinated.r1 | passed | None, None, None, None | 0 | 0 | 4 | 10 | 8 | 0.035084 |
| cmp-t66.coordinated.r2 | passed | None, None, None, None | 0 | 0 | 4 | 10 | 8 | 0.034630 |
| cmp-t66.coordinated.r3 | passed | None, None, None, None | 0 | 0 | 4 | 10 | 8 | 0.035846 |
| cmp-t66.flat.r1 | failed | None, None, None, None | 0 | 0 | 4 | 13 | 7 | 0.035668 |
| cmp-t66.flat.r2 | failed | None, None, None, None | 0 | 0 | 4 | 11 | 5 | 0.033798 |
| cmp-t66.flat.r3 | failed | None, None, None, None | 0 | 0 | 4 | 12 | 7 | 0.034312 |
| cmp-t67.coordinated.r1 | passed | None, None | 0 | 0 | 2 | 6 | 6 | 0.020527 |
| cmp-t67.coordinated.r2 | passed | None, None | 0 | 0 | 2 | 6 | 6 | 0.021697 |
| cmp-t67.coordinated.r3 | passed | None, None | 0 | 0 | 2 | 6 | 6 | 0.021447 |
| cmp-t67.flat.r1 | failed | None, None | 0 | 0 | 2 | 7 | 3 | 0.024923 |
| cmp-t67.flat.r2 | failed | None, None | 0 | 0 | 2 | 8 | 5 | 0.024615 |
| cmp-t67.flat.r3 | failed | None, None | 0 | 0 | 2 | 7 | 3 | 0.023622 |
| cmp-t68.coordinated.r1 | failed | None, None, None, None, None | 0 | 0 | 5 | 13 | 8 | 0.054786 |
| cmp-t68.coordinated.r2 | passed | None, None, None, None, None | 0 | 0 | 5 | 12 | 9 | 0.040728 |
| cmp-t68.coordinated.r3 | passed | None, None, None, None, None | 0 | 0 | 5 | 12 | 9 | 0.039407 |
| cmp-t68.flat.r1 | failed | None, None, None, None, None | 0 | 0 | 5 | 15 | 8 | 0.043888 |
| cmp-t68.flat.r2 | failed | None, None, None, None, None | 0 | 0 | 5 | 16 | 10 | 0.044532 |
| cmp-t68.flat.r3 | failed | None, None, None, None, None | 0 | 0 | 5 | 16 | 8 | 0.044047 |

Tool calls are not recorded by the backend; the tool rounds in `score.json` (model calls after the first in a turn) are a lower bound.
