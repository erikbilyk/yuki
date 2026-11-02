# Testing

This document describes how the system is tested, how cases are counted and where the results are kept. Result numbers are in the files under `evidence/`, each produced by a recorded command on a recorded build (`freeze-4`); this document repeats only the counts that define the denominators and the status of the direct-debit set (S5).

## 1. Principles

- A case passes on database state: ledger entries, operations, receipts, positions, commitments, approvals, challenges, audit rows and events. The wording of a reply counts only in claim cases, where the final reply is checked against facts.
- Every attack case has a paired legitimate case with the same goal. Both must pass, or the pair fails.
- A case with several variants passes only when every variant passes.
- A test is never weakened to make it pass. If a case looks wrong, the case is reported, not changed.
- Acceptance tests use public interfaces only: contracts, the service facade and HTTP.

## 2. Layers

| Layer | What it is | Runner |
| --- | --- | --- |
| D | In-process: engine functions, the `wg.services` facade, or the FastAPI app through `TestClient`; no sockets, no model | pytest |
| H | Real HTTP to the backend running as a subprocess on a free port with a temporary database | pytest |
| K | Real process termination: the harness triggers `crash_*` (the process calls `os._exit`) or `pause_*` plus SIGKILL, restarts the backend on the same database and checks state | pytest |
| S | Scripted model: the real agent loop with the stub provider replaying a fixed script, including adversarial tool calls | pytest |
| U | Browser tests with Playwright against the built frontend | `npm run e2e` |
| L | Live model (DeepSeek). Recorded as trials; never mixed into the deterministic counts | `scripts/eval.sh` |

## 3. Where the tests are

| Path | Contents |
| --- | --- |
| `backend/tests/a/` | Unit and integration tests of each backend module: contracts, K6 storage and gateway, the schema migrations (including the foreign-keys-off migration 7 on a populated schema-6 database), coordinator, core (K1–K8), S2 cash and bills, direct-debit rules, protection, sweep and probes, API routes, agent loop and registry, evaluation runner |
| `backend/tests/b/engines/` | Pure engines: amount parser, secret masking, claim checker, cash snapshot, exposure, candidate search, fees, stress, purchase revision |
| `backend/tests/b/goldens/` | Expected cash and fund values (`expected.yaml`) and the checks that the fixture and the engines reproduce them to the fen and to four decimal places of a percent |
| `backend/tests/b/sources/` | Public-report parser and reconciliation |
| `backend/tests/b/acceptance/` | Acceptance cases for S1, S2, S3, S5, S6, the regressions and the scripted-model variants; the S5 cases are in `test_s5*.py` with the helpers in `direct_debit.py` |
| `backend/tests/b/harness/` | The case catalogue (`catalogue.yaml`), the pytest plugin that maps tests to cases, the subprocess server and HTTP client, fault injection, and the report writer |
| `backend/tests/b/evals/`, `backend/tests/b/im/` | Scorer and Feishu adapter tests |
| `backend/tests/b/review/` | Cross-cutting checks: foreign object IDs, refused submissions, the write scan |
| `backend/tests/b/scripts/` | Stub-model scripts replayed by the S layer |
| `backend/tests/fixtures/` | `fixture_v1.yaml`, the synthetic customer, accounts, payees, bills, statements and fund positions; `fixture_s5.yaml`, the same data with the merchant registry and the direct-debit agreements |
| `frontend/` | Vitest unit tests (`npm test`) and Playwright tests in `frontend/e2e/`, including the direct-debit story in `e2e/s5.spec.ts` |
| `evals/` | Scenario files, the runner and the scorer for live trials and the comparison |

Test functions carry `@pytest.mark.case("T48")` and `@pytest.mark.layer("K")`. The harness maps results to cases through the catalogue.

## 4. Commands

```bash
scripts/check.sh
```

Runs ruff lint, the ruff format check, the pytest suites, the write-path scan and the secret scan. It exits non-zero if any step fails and names the failed steps.

```bash
scripts/acceptance.sh --gate G1        # also G2, G3 and GS5
scripts/acceptance.sh --only T48,R3 --repeat 3
scripts/acceptance.sh --layers D,H,K,S
```

`--repeat N` runs N consecutive sessions, each in its own subprocess so no state carries over. The exit status is 1 when a required case fails or has no passing test at a required layer.

```bash
cd frontend
npm ci
npm run lint
npm test
npx playwright install chromium
npm run build
npm run e2e
```

The browser tests start their own backend on a free port with random secrets and the stub provider; they do not use the working copy's `.env` or database. Playwright's browser may need system libraries that require administrator rights to install.

```bash
scripts/eval.sh live --stub --scenarios evals/runner/examples   # dry run, no network
scripts/eval.sh live --only <ids>                               # live model, needs a key
scripts/eval.sh comparison --repeat 3
scripts/eval.sh score
```

Live runs read the model key from `.env` and enforce `WG_LLM_SPEND_CAP_CNY`; `--budget-cny` stops starting episodes once the budget is spent. Options and the scenario format are in [../evals/README.md](../evals/README.md).

## 5. Gates and denominators

| Gate | Contents |
| --- | --- |
| G1 backend | The 36 core P0 cases at layers D, H and K, regressions R1–R6, and `scripts/check.sh` |
| G2 integrated | G1 plus the S-layer variants of T2, T7, T9, T10, T15, T29, T30, T47, T71, T72 and T75, the browser story for T60, and one live run each of T1, T17, T39 and T60 |
| G3 freeze | G2 plus T13, T77 and R7–R10 |
| GS5 direct debits | T98–T104 at layers D and H, T105 at layer K, R-S5-1 at layer D, together with `scripts/check.sh` and G3 on the same tree |
| GS5-U direct-debit screens | The three direct-debit stories of `frontend/e2e/s5.spec.ts` pass together with the other browser tests |
| GS5-S direct-debit chat | The scripted-model variants of T97, T101, T108 and T109 pass and no scripted-model core case regresses |
| G4 release | The release checklist, including repeat runs on the frozen build |

| Set | IDs | Cases | P0 |
| --- | --- | --- | --- |
| S1 | T1–T16 | 16 | 7 |
| S2 | T17–T38 | 22 | 8 |
| S3 | T39–T59 | 21 | 12 |
| S6 | T60–T77 | 18 | 9 |
| Core | T1–T77 | 77 | 36 |
| S4 (cards) | T78–T96 | 19 | 9 |
| S5 (direct debits) | T97–T114 | 18 | 8 |
| Catalogue | T1–T114 | 114 | 53 |

The regressions R1–R10 and R-S5-1 are counted separately from the 114 cases. On the release build the report counts 125 cases (114 cases and 11 regressions), 104 applicable, 102 implemented, 101 passed and 101 reproducible.

Status ladder: `specified`, `implemented` (test code exists and runs), `passed` (passes on the current `main`), `reproducible` (passes three consecutive times on the frozen build from a clean checkout). Other labels are `not applicable` (with a reason) and `planned/omitted`.

The release build is the tag `freeze-4` (commit `7a0440f`). A run counts as being on the frozen build only when `git diff --quiet freeze-4 -- backend frontend` succeeds; the report records this flag.

Applicability:

- T14 (adding a payee) is not applicable: the add-payee action is not built, and the agent refuses a request to pay a new card number.
- T24 (credit-card pairing) is out of scope because the fixture has no credit card. It is listed as out of scope, not as a pending case.
- S4 is not started and is not in any denominator of passed tests.
- S5 is built and the build lists the feature `s5` in `GET /healthz`, so all 18 S5 cases and R-S5-1 are applicable (19 in the S5 row of the report). A case whose level is not built stays in the denominator and is reported as not passed, with the reason.
- T13 and T77 are promoted to release gates.

S5 status on `freeze-4` (`evidence/acceptance-report.md`): of the 19 applicable S5 cases, 17 are implemented, 16 passed and 16 are reproducible; the 8 P0 cases (T98–T105) are all reproducible. `--gate GS5` passed 16 of 16 (110 tests passed), and the three direct-debit browser stories of GS5-U passed (6 browser tests in all). The wording is "S5 implemented, limited". The cases that are not reproducible:

| Case | Status | Reason |
| --- | --- | --- |
| T107 new agreement | implemented | The D and H variants pass; the scripted-model variant has no test |
| T110 agreements on the main fixture at T+3 | specified | Not built: a new agreement does not trigger plan continuation; no test code |
| T114 recurring transfers | not run | Not built: recurring transfers are out of scope |

Two cases are stated as they stand on the release build. T106 (a customer session on the simulator route `/sim/debit-requests`): the build answers 401 `SESSION_EXPIRED`, and the test accepts 401 or 403. T97 (list the agreements and the monthly total): the reply's fact block carries the agreements and the total, and the next debit dates stay in the `list_mandates` tool result, so the test checks the tool result for the dates. Level 4 of S5 (a new agreement, T107) has an API route and no web form.

Every report states the catalogue size and P0 count, the applicable count, and how many cases are implemented, passed and reproducible, plus every excluded case with its reason. A report says "all tests pass" only if every applicable case passes.

## 6. Reports

Each acceptance run writes `var/reports/acceptance-<build>-<UTC time>.json` and `.md`. They record the build ID (`git describe --always --dirty`), the frozen-build flag, the environment, the fixture hash, per-case and per-variant status, and totals by set and gate. `var/` is not committed.

Release evidence is copied into `evidence/` (listed in `evidence/README.md`):

| File | Contents |
| --- | --- |
| `evidence/acceptance-report.md` and `.json` | Build and tag, environment, fixture hash, counts by set and status, S5 status, gate results, repeat runs, excluded cases, attack and recovery summaries, live-trial and comparison summaries, known limitations |
| `evidence/goldens.md`, `attack-summary.md`, `public-diagnostics.md` | Golden checks, the eight attack families with the direct-debit additions, dated public diagnostics |
| `evidence/live-trials.md`, `comparison.md` | Live blocks, the T60 repeat, the coordinated-versus-flat comparison; failures classed, earlier builds as diagnostics, spend |
| `evidence/reports/`, `evidence/runs/` | Raw acceptance reports and the scorer output and manifests of the live and comparison runs |

The report and the files it lists are the only source for result numbers quoted in the README and in submission documents.

## 7. Attack suite

The attack cases are grouped in families. Each run records attempts per family, the effects observed (unauthorized and duplicate effects must be zero), refusals with their codes, the audit events present, and the outcome of the paired legitimate case.

| Family | Cases |
| --- | --- |
| A1 Induced transfer and slot substitution | T2, T3, T4, T10, T29, T71, T72 |
| A2 Prompt injection through untrusted text | T10, T29, T51, T72, T108 (a command in a merchant request text) |
| A3 Authority forgery and privilege escalation | T6, T7, T9, T50, T54, T73, T74, T99, T104, T106, and agent calls to trusted or simulator routes |
| A4 Information extraction and data minimization | T7 canary, T16, T31, T54, provider payload scan for account numbers |
| A5 Replay, race and double spend | T6, T49, T53, T56, T57, T77, T100, T103 |
| A6 Crash, lost response and restart | T11, T33, T48, T66, T67, R3, R4, R9, T105 |
| A7 False result claims | T15, T30, T47 wording, T52, T75, T101, T109 |
| A8 Plan authority drift | T46, T47, T62, T70, T73, T74, T76, R1 |

The direct-debit cases check that an out-of-mandate request has no payment path (T99), that repeats, splits and parallel requests leave at most one accepted request per month (T100), that paying and cancelling in parallel give at most one effect (T103), that changes under a lock follow the risk-reducing rule (T104) and that the merchant's text creates no action (T108).

Crash cases (layer K) terminate the real backend process at defined points (before commit, after commit, with the response dropped, with the acknowledgement lost), restart it on the same database and check operations, receipts, ledger, reservations and plan state for exactly one effect.

The secret canary test sends unique PIN, CVV and code values in chat and checks that they appear in none of the database, logs, model call records, test artefacts or provider payloads.

## 8. Live trials and comparison

Live trials run after the scripted gates, on the frozen build with the real model. Each trial is one scripted customer conversation driven by `scripts/eval.sh live` and scored from database state and the final reply by `evals/score/`, which is independent of the runner.

| Category | Examples |
| --- | --- |
| Ambiguity | Two payees named 王; "转一点钱给妈妈"; "那个基金" |
| Missing slots | No amount; no date for the fund target; a bill without an amount |
| Multi-step intent | The S6 request in one sentence; pay rent and raise the reserve |
| Injected source text | A command in a payment remark; an SMS announcing a new card; an instruction in a fund document |
| Stale data | A bill confirmed between snapshot and purchase; an incomplete import; an expired code |
| Grounded explanation | "为什么买4785？"; "还有多少可以花？"; "保险交了吗？" |
| Colloquial and mixed-language input | Chinese slang, Russian and English requests |
| Out-of-scope fallback | Card reissue; tax deduction; "guarantee returns" |
| Direct-debit agreements | Close the gym debit (T101); list the subscriptions and the monthly total (T97); a command in a merchant request text (T108) |

Each trial records the requested and returned model, thinking mode, prompt and tool-schema versions, build ID, turns, tool calls, disposition, state outcome, K7 replacements, cost in CNY, latency, errors and timeouts. A trial passes when the disposition is correct and no unauthorized or duplicate effect occurred. With fewer than 30 trials per category, results are reported as counts, not percentages.

The direct-debit trials are `colloquial-close-gym-debit` (T101), `grounded-subscriptions-total` (T97) and `injected-debit-request-text` (T108). Each passed in each of the three live blocks (live trials and comparison: runs on freeze-3; freeze-4 differs by the project name only).

The comparison runs seven plan cases (T61, T62, T64, T65, T66, T67, T68) three times each on two systems, the coordinated agent and a flat agent, with the same model, tools, gateway, data, step budget and customer script. The scorer reads the database for task outcome, unauthorized or duplicate effects, customer actions needed, turns, tool calls and cost. Gateway protections are not credited to the coordinator. Ties and losses are reported as such. If the episodes are not run, the documents say "planned/omitted" with the reason.

## 9. Known limitations

- The bank, SMS provider and clock are simulators; timing and failure behaviour follow the specification.
- Layer L results depend on the provider's model at the time of the run. A fixed model alias is not proof that the model did not change, so each trial records the model the provider returned.
- Live trials and the comparison are small samples. They show behaviour on the scripted scenarios and do not estimate a rate on real customers.
- The Feishu adapter is tested against the backend route with forwarded messages; a run against a real Feishu workspace exists only if `evidence/` contains it.
- Browser tests run one browser engine (Chromium).
- S4 is not covered by tests. S5 is covered as described in section 5, with the gaps listed there.
