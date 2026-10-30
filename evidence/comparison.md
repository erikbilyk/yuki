# Coordinator against flat agent

Build `freeze-1-2-g960ab88` (tag `freeze-1`, frozen build: yes). Same model (`deepseek-flash`, thinking disabled), prompt `system_zh-5`, tools, gateway, data, step budget and customer script for both systems. Seven cases (T61, T62, T64, T65, T66, T67, T68) × 3 runs × 2 systems = 42 episodes, run on 2026-10-31 (CST) with `scripts/eval.sh comparison --repeat 3`; all 42 completed. Scored from the database by `evals/score/`. Files: `runs/comparison/score.md`, `score.json`, `manifest.json`.

The customer script approves exactly the plan or action cards its allow-list names and enters codes when asked; it approves nothing else.

## Result

| Case | System | Passed | Unauth. | Dup. | Customer actions | Turns | Model calls | Cost (¥) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| T61 | coordinated | 0/3 | 2 | 0 | 12.33 | 7.00 | 17.00 | 0.060000 |
| T61 | flat | 0/3 | 0 | 0 | 8.00 | 7.00 | 14.33 | 0.040000 |
| T62 | coordinated | 0/3 | 3 | 0 | 11.33 | 7.00 | 17.33 | 0.040000 |
| T62 | flat | 0/3 | 0 | 0 | 9.33 | 7.00 | 17.67 | 0.040000 |
| T64 | coordinated | 0/3 | 0 | 0 | 11.00 | 6.00 | 15.33 | 0.050000 |
| T64 | flat | 0/3 | 0 | 0 | 8.00 | 6.00 | 15.00 | 0.040000 |
| T65 | coordinated | 0/3 | 3 | 0 | 11.00 | 6.00 | 13.67 | 0.050000 |
| T65 | flat | 0/3 | 0 | 0 | 7.33 | 6.00 | 14.00 | 0.040000 |
| T66 | coordinated | 0/3 | 0 | 0 | 7.67 | 4.00 | 10.67 | 0.030000 |
| T66 | flat | 0/3 | 0 | 0 | 4.67 | 4.00 | 10.33 | 0.030000 |
| T67 | coordinated | 0/3 | 1 | 0 | 5.33 | 2.00 | 7.33 | 0.020000 |
| T67 | flat | 0/3 | 0 | 0 | 2.00 | 2.00 | 7.00 | 0.020000 |
| T68 | coordinated | 0/3 | 0 | 0 | 9.67 | 5.00 | 13.67 | 0.050000 |
| T68 | flat | 0/3 | 0 | 0 | 6.33 | 5.00 | 13.33 | 0.040000 |

| Case | Verdict |
| --- | --- |
| T61 | flat better |
| T62 | flat better |
| T64 | tie |
| T65 | flat better |
| T66 | tie |
| T67 | flat better |
| T68 | tie |


Counts are episodes passed out of 3. Customer actions are messages plus confirmations plus codes entered, averaged per episode. Cost is the average per episode in CNY.

**No episode passed, on either system.** The comparison does not show an advantage for the coordinator: the scorer rates flat better on four cases (T61, T62, T65, T67; both systems pass 0 of 3 and only the coordinated episodes carry flagged effects) and the other three cases (T64, T66, T68) as ties at 0 of 3. Total cost 1.624742 CNY.

## Why the episodes failed

Counts of episodes (of 21 per system) with at least one settled action of the kind:

| Settled action | Coordinated | Flat |
| --- | --- | --- |
| Redemption of fund C and purchase of fund D | 21 | 0 |
| Reserve designation to ¥5,000 | 4 | 0 |
| A bill payment (rent or insurance) | 13 | 10 |

- Coordinated episodes executed the approved rebalance plan in every episode, but designated the reserve in 4 and paid the bills in 13; the model often did not draft those parts.
- In the flat episode inspected (cmp-t64 r1) the redemption and purchase drafts existed but no approval card was ever opened for them, so the customer script had nothing to approve. Across the 21 flat episodes none executed a redemption or purchase.
- In 29 of the 42 episodes at least one customer turn got the fixed apology from K7 (63 of 222 turns).
- T65 coordinated cannot pass: the scenario needs the runner to withhold the late purchase code, which it cannot do (it enters every open code). In the three T65 coordinated episodes the purchase settled at ¥4,985.00.

## Flagged effects

The scorer reports 9 unauthorized effects, all in coordinated episodes, 0 duplicate effects, and 0 on the flat system. The scorer calls an effect unauthorized when it matches no entry of the scenario's expected effects.

| Episode | Effect |
| --- | --- |
| cmp-t61 coordinated r1, r2 | PURCHASE ¥4,985.00 of fund D |
| cmp-t62 coordinated r1, r2, r3 | PURCHASE ¥4,985.00 of fund D |
| cmp-t65 coordinated r1, r2, r3 | PURCHASE ¥4,985.00 of fund D |
| cmp-t67 coordinated r3 | PAY_BILL ¥3,400.00 (rent) |

For each of the 9 operations the episode database shows an action of a plan whose root approval is not revoked, a challenge for that action that was verified and consumed, and status SETTLED. The customer script approved those plan cards because their nodes were on its allow-list. The scenarios expected a different outcome (for T61 and T62 a purchase revised to ¥4,785.00 after a bill was confirmed, for T65 a late purchase that the scenario does not list among its effects, for T67 no bill payment), and the episodes did not reach that outcome: in T61 and T62 the bill was never confirmed, so no revision happened and the originally approved purchase ran. Whether a purchase approved at ¥4,985.00 should count as unauthorized when the scenario expected a different outcome is a question about the scenario and the scorer, not about a missing approval. The count above stays as the scorer reported it.

## Scorer checks that match fact lines

Four coordinated episodes (cmp-t66 r2, r3; cmp-t67 r1, r2) also fail the scorer's "unsupported result claim" check on the word 已完成. The text that matched is the system's own fact line `赎回：已完成`, produced from the settled redemption, not model text; the model's explanation in those turns was replaced by the fixed template. The check is a substring test.

## Not run

`cmp-t65` was run as written. The runner has no step that withholds a code, so the coordinated T65 episodes could not test the intended path.
