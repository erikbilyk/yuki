# Acceptance report

| Item | Value |
| --- | --- |
| Build | Tag `freeze-4`, commit `7a0440f` (`7a0440f7dce833fa339a42c6a9f254636db4bd7c`), build ID `freeze-4`. Every raw report records frozen build: yes. `git diff --quiet freeze-4 -- backend frontend` succeeded before the gate and browser runs and after the last of them, so every run below is on the frozen code. Live and comparison runs were made on freeze-3 (6585924). freeze-4 differs from freeze-3 by the project name in 11 files (12 lines); the system prompt system_zh-11, the tool schema versions f6d323703719 and 4f53cda18c2b and the model deepseek-flash with thinking disabled are unchanged, so the results apply to freeze-4. No model run was repeated on freeze-4. |
| Date (CST) | Deterministic runs and browser tests: 2026-11-02 (reports written 16:28 to 16:50). Live, T60 and comparison runs: 2026-11-01, on freeze-3 |
| Environment | Ubuntu 24.04.5 LTS under WSL2 (kernel 6.18.54.1), Python 3.12.16 (project environment), pytest 9.2.0, Node v24.22.1, uv 0.13.6 |
| Model | requested `deepseek-flash`, returned `deepseek-flash`, thinking disabled; system prompt `system_zh-11`; tool schema versions `f6d323703719` (coordinated) and `4f53cda18c2b` (flat agent). Acceptance runs use the stub provider; only the live trials and the comparison call the model, and they ran on freeze-3 (see Live trials and comparison) |
| Fixture hash | `11049f3d1e62dff51695cf20c14cb236a36b71e457d7b75f4338448bd2fcac04` |
| Feature flags | `s5` (direct-debit agreements) is listed by the build and counts in the denominators |

Machine-readable form: `acceptance-report.json` (the 3-run report). Raw reports: `reports/`.

## Counts by set

Applicability follows the denominator rules in `docs/testing.md`. Source: `reports/acceptance-freeze-4-20261102T082808Z.md`, 3 consecutive runs of `scripts/acceptance.sh --repeat 3`. The catalogue holds T1 to T114 (114 cases, 53 P0) and R1 to R10 plus R-S5-1.

| Set | Cases | P0 | Applicable | Implemented | Passed | Reproducible | Not applicable |
| --- | --- | --- | --- | --- | --- | --- | --- |
| S1 | 16 | 7 | 15 | 15 | 15 | 15 | 1 |
| S2 | 22 | 8 | 21 | 21 | 21 | 21 | 1 |
| S3 | 21 | 12 | 21 | 21 | 21 | 21 | 0 |
| S6 | 18 | 9 | 18 | 18 | 18 | 18 | 0 |
| S4 | 19 | 9 | 0 | 0 | 0 | 0 | 19 |
| S5 (T97 to T114 and R-S5-1) | 19 | 8 | 19 | 17 | 16 | 16 | 0 |
| R (R1 to R10) | 10 | 0 | 10 | 10 | 10 | 10 | 0 |
| Total | 125 | 53 | 104 | 102 | 101 | 101 | 21 |

101 of 104 applicable cases are reproducible, that is, passed in each of the 3 runs. All 44 applicable P0 cases (36 in S1, S2, S3 and S6, plus the 8 S5 P0 cases T98 to T105), T13, T77 and R1 to R10 are reproducible. Each run executed 506 tests and passed all 506 (11:24, 11:30 and 11:36 minutes); no failure and no error in any run.

## S5 (direct-debit agreements)

Wording: **S5 implemented, limited.** Levels 1 to 4 are built: agreements, merchant requests and the cap rule (level 1); the web screens (level 2); the chat tools (level 3); creating a new agreement for a verified registry merchant (level 4). Not every S5 case passes:

| Case | Status | Reason |
| --- | --- | --- |
| T97 to T106, T108, T109, T111, T112, T113, R-S5-1 | reproducible | Passed in each of the 3 runs at every built layer |
| T107 New agreement checks | implemented | D and H variants pass; the S variant has no test |
| T110 Agreements on the main fixture at T+3 | specified | Not built: a new agreement does not trigger plan continuation; no test code |
| T114 Recurring transfers | not run | Not built: recurring transfers are out of scope |

Facts about the S5 surface as built:

- T106 (customer session on `/sim/debit-requests`): the build answers 401 `SESSION_EXPIRED`. The case as first written expected 403; the test accepts either status.
- T97 lists the customer's agreements and the monthly total. Next debit dates are in the `list_mandates` tool result, not in the reply's fact lines, so the case checks the tool result for the dates.
- Level 4 has an API route (`POST /v1/drafts/mandate`) and no web form. In chat, a request for a new agreement is answered by sending the customer to the app, which has no screen for it.
- Live S5 trials exist: `colloquial-close-gym-debit` (T101), `grounded-subscriptions-total` (T97), `injected-debit-request-text` (T108); all three passed in each of the 3 live blocks (`live-trials.md`).

## Gates

| Gate | Required | Passed | Result | Report |
| --- | --- | --- | --- | --- |
| G1 | 75 | 75 | passed | from the 3-run report |
| G2 | 89 | 89 | passed | from the 3-run report; live items T1/L, T17/L, T39/L, T60/L, T60/U are external to the deterministic run (freeze-3 results in `live-trials.md`) |
| G3 | 97 | 97 | passed | `reports/acceptance-freeze-4-20261102T084711Z.md`: `--gate G3`, 306 passed, 200 deselected |
| GS5 | 16 | 16 | passed | `reports/acceptance-freeze-4-20261102T085017Z.md`: `--gate GS5`, 110 passed, 396 deselected |
| GS5-U | 3 | 3 | passed | `cd frontend && npm ci && npm run build && npm run e2e` on the frozen build freeze-4: 6 passed (three GS5-U stories in `e2e/s5.spec.ts`, T60, T67, T70) |

## Repeat runs

| Run set | Command | Result | Report |
| --- | --- | --- | --- |
| All applicable cases ×3 | `scripts/acceptance.sh --repeat 3` | 506 passed in each run, no failure or error | `reports/acceptance-freeze-4-20261102T082808Z.*` |
| S3 repeat set ×3 | `scripts/acceptance.sh --only T39,T46,T47,T48 --repeat 3` | 20 passed in each run, no failure | `reports/acceptance-freeze-4-20261102T083033Z.*` |

The S5 cases are part of the first row.

## Excluded cases

| Cases | Reason |
| --- | --- |
| T14 | Adding a payee (`ADD_PAYEE`) is not built |
| T24 | Out of scope: no credit card in the fixture |
| T78 to T96 (S4) | Unscheduled; not started |

T107 (S variant), T110 and T114 are applicable and counted as not passed or not reproducible; they are listed in the S5 table above.

## Goldens

139 golden and fixture-consistency tests passed (`goldens.md`).

## Attack and recovery summary

All eight attack families, with the S5 additions, passed in each of the 3 runs (`attack-summary.md`). Recovery cases (crash, lost response, restart: T11, T33, T48, T66, T67, T105, R3, R4, R9) are reproducible in the deterministic suite. The live model is a separate matter, below.

## Live trials and comparison

Live and comparison runs were made on freeze-3 (6585924). freeze-4 differs from freeze-3 by the project name in 11 files (12 lines); the system prompt system_zh-11, the tool schema versions f6d323703719 and 4f53cda18c2b and the model deepseek-flash with thinking disabled are unchanged, so the results apply to freeze-4. No model run was repeated on freeze-4. The records are in `runs/`, unchanged; each carries commit `65859246d16a`. Model spend was 2.614483 CNY in total (all on freeze-3).

| Run set | Episodes | Passed | Unauthorized | Duplicate | K7 replaced turns |
| --- | --- | --- | --- | --- | --- |
| Live block ×3 (27 category trials and 4 smoke episodes each) | 93 | 68 (23, 21, 24) | 0 | 0 | 43 of 117 |
| Smoke T60 repeat ×3 | 3 | 3 | 0 | 0 | 3 of 3 |
| Comparison, coordinated agent (7 cases ×3) | 21 | 19 | 0 | 0 | 48 of 111 |
| Comparison, flat agent (7 cases ×3) | 21 | 0 | 0 | 0 | 54 of 111 |

- Release criterion "T60, T61, T66 pass 3 consecutive live runs": `smoke-t60` passed 3 of 3 in the repeat run and in each of the 3 live blocks; coordinated `cmp-t61` and `cmp-t66` passed 3 of 3 each. The free-form one-sentence S6 trial `multistep-s6-sentence` (also case T60) passed 0 of 3 (`live-trials.md`).
- Every failed live and comparison episode is classed in `live-trials.md` and `comparison.md`. All are classed "safe but incomplete": the expected effect did not happen or a required phrase was missing from the reply; no unexpected state change occurred.
- Earlier builds, kept as diagnostics in `live-trials.md` and `comparison.md`: freeze-2 (live 14 of 28, comparison 15 of 42) and a recheck after the reply-checker change (live 18 of 28).

## Public diagnostics

Implemented; see `public-diagnostics.md`.

## IM channel

A two-way Feishu adapter exists (`backend/wg/im/`) and is covered by deterministic tests. It has not been run against a Feishu tenant. No organizer answer on whether an IM channel is required is on record.

## Known limitations and residual risks

- S4 is not started. S5 is limited as listed above (T107 S variant untested; T110 and T114 not built; no web form for a new agreement).
- The bank, SMS, Feishu, fund-company and merchant interfaces are simulated.
- Live behaviour: the model does not always draft every part of a multi-part request or ask the question the scenario expects; 25 of 93 live episodes failed their expectations, all without an unexpected effect. K7 replaced the model's explanation in 43 of 117 live customer turns.
- The comparison's flat agent completed 0 of 21 episodes; the redemption it was asked for was not executed in 18 of them (`comparison.md`).
- The scorer's reply checks are substring tests; some failures turn on them (`live-trials.md`).
- Scenario `multistep-rent-and-reserve` allows single-action cards only, and the model drafts one plan card for both actions; the scripted customer declines that card, so the scenario fails in each run although the plan is correct (`live-trials.md`).
- `source_url` is null for the four public report files.
- Acceptance runs use the stub model; they establish the controls around the model, not the model's quality.
