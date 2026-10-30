# Acceptance report

| Item | Value |
| --- | --- |
| Build | `freeze-1-2-g960ab88`: tag `freeze-1` (commit d153511) plus two documentation commits; `git diff --quiet freeze-1 -- backend frontend` succeeded before and after every block, so every run below is on the frozen code |
| Date (CST) | 2026-10-31 |
| Environment | Ubuntu 24.04.5 LTS under WSL2 (kernel 6.18.54.1), Python 3.12.16, pytest 9.2.0, Node v24.22.1, uv 0.13.6 |
| Model | requested `deepseek-flash`, returned `deepseek-flash`, thinking disabled; system prompt `system_zh-5`. Acceptance runs use the stub provider; only the live trials and the comparison call the model |
| Fixture hash | `11049f3d1e62dff51695cf20c14cb236a36b71e457d7b75f4338448bd2fcac04` |

Machine-readable form: `acceptance-report.json`. Raw reports: `reports/`.

## Counts by set

Applicability follows the denominator rules in `docs/testing.md`. Source: `reports/acceptance-freeze-1-2-g960ab88-20261030T180642Z.md`, 3 consecutive runs.

| Set | Cases | P0 | Applicable | Implemented | Passed | Reproducible | Not applicable |
| --- | --- | --- | --- | --- | --- | --- | --- |
| S1 | 16 | 7 | 15 | 15 | 15 | 15 | 1 |
| S2 | 22 | 8 | 21 | 21 | 21 | 21 | 1 |
| S3 | 21 | 12 | 21 | 21 | 21 | 21 | 0 |
| S6 | 18 | 9 | 18 | 18 | 18 | 18 | 0 |
| S4 | 19 | 9 | 0 | 0 | 0 | 0 | 19 |
| S5 | 19 | 8 | 0 | 0 | 0 | 0 | 19 |
| R | 10 | 0 | 10 | 10 | 10 | 10 | 0 |

85 cases are applicable and all 85 are reproducible, including all 36 applicable P0 cases, T13, T77 and R1 to R10. "Reproducible" means passed in each of the 3 runs.

## Gates

| Gate | Required | Passed | Result | Report |
| --- | --- | --- | --- | --- |
| G1 | 75 | 75 | passed | from the 3-run report |
| G2 | 89 | 89 | passed | from the 3-run report; live items T1/L, T17/L, T39/L, T60/L, T60/U are external to the deterministic run (see `live-trials.md`) |
| G3 | 97 | 97 | passed | `reports/acceptance-freeze-1-2-g960ab88-20261030T191355Z.md`: `--gate G3`, 306 passed, 1 deselected |

## Repeat runs

| Run set | Command | Result | Report |
| --- | --- | --- | --- |
| All applicable cases ×3 | `scripts/acceptance.sh --repeat 3` | 307 passed in each run (6:15, 6:15, 6:37 minutes), no failure or error | `reports/acceptance-freeze-1-2-g960ab88-20261030T180642Z.*` |
| S3 repeat set ×3 | `scripts/acceptance.sh --only T39,T46,T47,T48 --repeat 3` | 20 passed in each run, no failure | `reports/acceptance-freeze-1-2-g960ab88-20261030T183908Z.*` |
| Earlier all-case repeat | `scripts/acceptance.sh --repeat 3` | run 1 307 passed; run 2 306 passed and 1 error; run 3 307 passed | `reports/acceptance-freeze-1-2-g960ab88-20261030T173333Z.*` |

The error in the earlier set: `test_t7_reissued_code_executes_after_expiry[H]` failed at fixture setup with `ServerError: backend exited (-15) during startup`. The test backend received SIGTERM before it was healthy and the test body did not run. T7 alone passed 13 of 13 in each of 3 diagnostic runs, and 3 later all-case runs plus the G3 run on the same code did not repeat the error. The sender of the signal was not identified. It is treated as a failure of the test environment, not of the frozen build; the earlier report is kept because it shows G3 as 96/97 for that run.

## Excluded cases

| Cases | Reason |
| --- | --- |
| T14 | Adding a payee (`ADD_PAYEE`) is not built |
| T24 | Out of scope: no credit card in the fixture |
| T78 to T96 (S4) | Unscheduled; not started |
| T97 to T114, R-S5-1 (S5) | Designed, not implemented |

## Goldens

132 golden and fixture-consistency tests passed (`goldens.md`).

## Attack and recovery summary

All eight attack families passed in each of the 3 runs (`attack-summary.md`). Recovery cases (crash, lost response, restart: T11, T33, T48, T66, T67, R3, R4, R9) are reproducible in the deterministic suite. The live model is a separate matter, below.

## Live trials and comparison

- Live trials (`live-trials.md`): 24 category trials plus 4 smoke episodes, 13 of 28 passed; 0 unauthorized and 0 duplicate effects reported. The three T60 repeats passed 2 of 3.
- Comparison (`comparison.md`): 42 of 42 episodes completed, 0 passed on either system. The scorer reports 9 unauthorized effects on the coordinated side (each under an unrevoked plan approval with a consumed code) and none on the flat side.
- Total model spend for these runs: 1.908017 CNY.

The live model does not reliably finish multi-part requests, and K7 replaced the model's explanation in 23 of 28 live episodes. These results are recorded as they are; the criterion "T60, T61, T66 pass 3 consecutive live runs" is not met (T60 2 of 3, T61 0 of 3, T66 0 of 3).

## Public diagnostics

Implemented; see `public-diagnostics.md`.

## IM channel

A two-way Feishu adapter exists (`backend/wg/im/`) and is covered by deterministic tests. It has not been run against a Feishu tenant. No organizer answer on whether an IM channel is required is on record.

## Known limitations and residual risks

- S4 is not started and S5 is not built; neither is in any denominator.
- The bank, SMS, Feishu and fund-company interfaces are simulated.
- Live behaviour: the model often does not draft every part of a multi-part request, and K7 replaces explanations it cannot support, including some clarifying questions. No unauthorized money movement was observed, but task completion on the live model is low (live trials 13 of 28, comparison 0 of 42).
- In flat mode the model's redemption and purchase drafts were not presented for approval in the episode inspected, and none of the 21 flat comparison episodes executed them.
- The comparison scenario T65 cannot test its intended path because the runner enters every open code.
- The scorer's text checks are substring tests; four comparison episodes and three live episodes fail on them with correct system text (`comparison.md`, `live-trials.md`).
- `source_url` is null for the four public report files.
- Acceptance runs use the stub model; they establish the controls around the model, not the model's quality.
