# Coordinator against flat agent

Build of the runs: `freeze-3` (commit `6585924`, frozen build: yes). Live and comparison runs were made on freeze-3 (6585924). freeze-4 differs from freeze-3 by the project name in 11 files (12 lines); the system prompt system_zh-11, the tool schema versions f6d323703719 and 4f53cda18c2b and the model deepseek-flash with thinking disabled are unchanged, so the results apply to freeze-4. No model run was repeated on freeze-4. Same model (`deepseek-flash`, thinking disabled), prompt `system_zh-11`, tools, gateway, data, step budget and customer script for both systems; tool schema versions `f6d323703719` (coordinated) and `4f53cda18c2b` (flat). Seven cases (T61, T62, T64, T65, T66, T67, T68) × 3 runs × 2 systems = 42 episodes, run on 2026-11-01 (CST) with `scripts/eval.sh comparison --repeat 3 --budget-cny 3`; all 42 completed. Scored from the database by `evals/score/`, independent of the runner. Files: `runs/comparison/score.md`, `score.json`, `manifest.json`. Transcripts are not part of the evidence folder.

The customer script approves exactly the plan or action cards its allow-list names and enters codes when asked, except where a scenario withholds a code; it approves nothing else.

## Result

| System | Episodes | Passed | Unauthorized | Duplicate | K7 replaced turns | Cost (CNY) |
| --- | --- | --- | --- | --- | --- | --- |
| Coordinated | 21 | 19 | 0 | 0 | 48 of 111 | 0.989333 |
| Flat | 21 | 0 | 0 | 0 | 54 of 111 | 0.957146 |
| Total | 42 | 19 | 0 | 0 | 102 of 222 | 1.946479 |

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

The cost column is the scorer's per-episode mean rounded to two decimals; the totals above are exact.

Verdicts (passed episodes first, then unauthorized and duplicate effects): the coordinated agent is better in all seven cases. Gateway protections are not credited to either system. Customer actions are messages plus confirmations plus codes entered, averaged per episode. Turns are equal by construction because both systems receive the same script.

## Failures

Classes as in `live-trials.md`: "safe but incomplete" means no unauthorized or duplicate effect and the expected effect missing; "wrong effect" means an unauthorized, duplicate or different effect. Totals: 23 safe but incomplete, 0 wrong effect.

| Episodes | What the records show | Class |
| --- | --- | --- |
| Coordinated T62 run 2 | The redemption ran; the reserve change, the rent and the insurance did not. The first reply was a clarification; four later replies were replaced by K7 text | safe but incomplete |
| Coordinated T68 run 1 | The plan status is `completed` but the reserve change and the rent were not executed | safe but incomplete |
| Flat, 18 of 21 episodes | The redemption of fund C was not executed, so the purchase and bill payments that depend on it did not run either | safe but incomplete |
| Flat, T66 run 3 and T67 run 2 | The redemption was submitted but not settled when the episode ended; the purchase and the rent did not run | safe but incomplete |
| Flat, T68 run 2 | The redemption ran; the purchase of fund D did not, and two reply checks failed | safe but incomplete |

Why the flat agent did not execute the redemption was not investigated in this run.

## Scenario changes since the first comparison

- T67 expects the rent payment. The rent node belongs to the plan the customer approved and its code is entered after the restart, so the payment is an intended effect; the scenario now expects it and a balance of 1,460,000 fen. The earlier scorer count of unauthorized effects for T67 came from the scenario expectation, not from a missing approval.
- T65 withholds the code of the late purchase. After the late redemption settles, the plan's purchase node waits for a code; the runner's `withhold` option keeps the customer from entering it, which is the intended behaviour of the case (no purchase), and the scenario uses it.

## Earlier builds, as diagnostics

| Run | Build | Prompt | Coordinated passed | Flat passed | K7 replaced turns |
| --- | --- | --- | --- | --- | --- |
| freeze-2 (`runs/earlier-freeze-2/comparison`) | `freeze-2` | `system_zh-9` | 15 of 21 (T61, T62, T64, T66, T68 3/3; T65, T67 0/3) | 0 of 21 | 200 of 222 |
| After the reply-checker change, 1 run per case | 5dda082 | `system_zh-9` | 6 of 7 | 0 of 7 | 34 of 74 |
| freeze-3 (this report) | `freeze-3` | `system_zh-11` | 19 of 21 | 0 of 21 | 102 of 222 (48 of 111 coordinated) |

The first comparison, on the first frozen build, passed 0 of 42 on both systems; that report is superseded.
