# WealthGuard

## 1. Project Overview

- Track: AI track (domestic)
- Team name: [to be confirmed]
- Project name: WealthGuard
- One-line summary: a banking agent prototype that plans and carries out everyday money tasks (transfers, cash and bills, fund rebalancing) on a simulated bank, where the language model can only read and draft and every money movement needs the customer's approval at the right tier.

The customer talks to the agent in Chinese, English or Russian; all customer-facing text is Simplified Chinese. The agent reads the customer's accounts, proposes actions and explains results. It cannot approve, verify, submit, settle or unlock anything. Approval happens on a server-rendered card outside the chat, and risky actions also need a one-time code that appears only in a simulated SMS panel.

Core scenarios:

| Scenario | What the customer asks for |
| --- | --- |
| S1 transfers | Pay a saved payee; the system resolves ambiguous names and refuses amounts or payees taken from untrusted text |
| S2 cash and bills | How much can I spend, confirm or pay a bill, change the reserve, resolve an unclear transaction |
| S3 fund rebalancing | Bring technology-sector exposure under a target by redeeming one fund and buying another, within a fee cap and a turnover cap |
| S6 cross-scenario plan | One request that combines a redemption, a bill payment, a purchase and a reserve change, with a checkpoint five days later |

Everything runs on synthetic data and a simulated bank. No real bank, card network or brokerage is contacted.

## 2. System Architecture

One repository, one backend process, one SQLite database file per working copy.

```mermaid
flowchart LR
    subgraph U["Untrusted zone"]
        Customer["Customer text, IM messages"]
        Ext["Remarks, SMS, merchant names, documents"]
        LLM["Language model (DeepSeek)"]
    end

    subgraph UI["Trusted UI surface"]
        Card["Approval card"]
        Code["Code entry"]
        Sms["Simulated SMS panel"]
    end

    subgraph Core["Trusted core: one backend process"]
        K1["K1 session and identity"]
        K2["K2 context filter"]
        Tools["Tool registry: read and draft tools only"]
        K3["K3 policy and tiers"]
        K4["K4 approval and step-up"]
        K5["K5 coordinator"]
        K7["K7 result verification"]
        K8["K8 audit, breaker, locks"]
        K6["K6 gateway and persistence"]
        Bank["Simulated bank"]
        DB[("SQLite, WAL")]
    end

    Customer --> K2
    Ext --> K2
    K2 --> LLM
    LLM -->|tool calls| Tools
    Tools --> K3
    Tools -->|drafts| K6
    Card --> K4
    Code --> K4
    Sms -.->|code shown to customer| Code
    K4 --> K6
    K5 --> K6
    K6 -->|"BEGIN IMMEDIATE"| DB
    K6 --> Bank
    K6 --> K8
    LLM -->|explanation| K7
    K7 -->|fact block and checked text| Customer
    K1 --- Tools
```

Components:

- Backend: Python 3.12, FastAPI, Pydantic v2, SQLite in WAL mode (package `wg`).
- Frontend: React and TypeScript built with Vite, served by the backend at `/` in release mode.
- Model: DeepSeek through an OpenAI-compatible chat completions adapter with tool calls; a stub provider replays fixed scripts for deterministic tests.
- Bank: a simulator inside K6 (`backend/wg/k6/bank.py`) that keeps its own request records and posts balanced ledger entries. Step-up codes are delivered through a `VerificationProvider` interface; the shipped implementation simulates SMS.
- Feishu adapter: an optional separate process that forwards direct messages to the backend over HTTP.

| Module | Path | Role |
| --- | --- | --- |
| K1 | `backend/wg/core/k1_session.py` | Login, sessions, roles, ownership checks, masked handles |
| K2 | `backend/wg/core/k2_context.py` | Provenance labels, amount cross-check, secret masking, minimal data to the model |
| K3 | `backend/wg/core/k3_policy.py` | Tiers, daily sum, limits, read scopes |
| K4 | `backend/wg/core/k4_approval.py` | Cards, approvals, challenges, grants |
| K5 | `backend/wg/core/k5_coordinator.py` | Plans, dependency validation, revisions within bounds, restart recovery |
| K6 | `backend/wg/k6/` | The only write connection, repositories, gateway, simulator |
| K7 | `backend/wg/core/k7_verify.py` | Fact blocks and claim checks on the model's explanation |
| K8 | `backend/wg/core/k8_audit.py` | Audit log, failure breaker, locks, operator unlock |

Rules that shape the design:

- Single write path. Only `wg/k6` executes SQL writes, always inside `unit_of_work` (`BEGIN IMMEDIATE`). Domain events and audit rows commit in the same transaction as the change they describe. A source scan (`scripts/scan_writes.py`, part of `scripts/check.sh`) fails if a SQL write statement appears outside `wg/k6`.
- Model boundary. The model gets read and draft tools. Every tool argument is validated, every tool declares the data sources it may read, and the customer comes from the session, never from tool arguments.
- Facts only. Every number a customer sees comes from an engine result or a database record. The reply has a server-rendered fact block and an optional model explanation; K7 replaces the explanation with a template if any amount, percent, count or status word is not backed by a fact.
- Money is integer fen. Shares, NAV and percentages are `Decimal`, serialized as strings. No float appears in a money, share or percent field.

Data sources: a fixture (`backend/tests/fixtures/fixture_v1.yaml`) for the simulated customer, accounts, payees, bills, statements and fund positions; public interim reports of two real funds (010737 and 019018, as of 2026-06-30) extracted offline into `data/public/` for read-only diagnostics.

Further detail: [docs/architecture.md](docs/architecture.md).

## 3. Environment and Dependencies

- Operating system: Ubuntu 24.04, or another Linux, with a shell and `curl`. Developed on Ubuntu 24.04 under WSL2.
- Runtime versions: Python 3.12 (`>=3.12,<3.13`), Node 24, npm 11, [uv](https://docs.astral.sh/uv/) (0.13 was used).
- Backend dependencies (`backend/pyproject.toml`, locked in `backend/uv.lock`): fastapi, uvicorn, pydantic, httpx, pyyaml. Development group: pytest, ruff. Optional extras: `public` (pdfplumber, for re-extracting the public reports) and `feishu` (lark-oapi, for the Feishu adapter).
- Frontend dependencies (`frontend/package.json`, locked in `frontend/package-lock.json`): react, react-dom; Vite, TypeScript, ESLint, Vitest and Playwright for development.
- Network: needed only for installing packages and for live model calls. The default configuration makes no outbound call.
- Environment variables: copy `.env.example` to `.env` (the file is gitignored). Never commit real secrets.

| Variable | Meaning | Default |
| --- | --- | --- |
| `WG_PORT`, `WG_WEB_PORT` | Backend port and Vite development server port | 8100, 5170 |
| `WG_DB_PATH` | SQLite file | `var/wg.db` |
| `WG_FAULT_WAIT_FILE` | Marker file used by the pause faults of the crash tests | `var/fault.wait` |
| `WG_DEMO_MODE` | `1` enables `/sim` routes, the demo login and API docs | `0` |
| `WG_CODE_HMAC_KEY`, `WG_SESSION_SECRET` | Random secrets for step-up codes and sessions; the backend refuses to start if either is empty | empty |
| `WG_CHANNEL_SECRET` | Shared secret between the backend and the Feishu adapter | empty |
| `WG_LLM_PROVIDER` | `deepseek` or `stub` | `stub` |
| `WG_LLM_API_KEY` | Provider key; read by the backend only | empty |
| `WG_LLM_BASE_URL`, `WG_LLM_MODEL`, `WG_LLM_THINKING`, `WG_LLM_TIMEOUT_S` | Model endpoint, model name, thinking mode, timeout in seconds | `https://api.deepseek.com`, `deepseek-flash`, `disabled`, `30` |
| `WG_LLM_STUB_SCRIPT` | Script file replayed by the stub provider | empty |
| `WG_LLM_PRICE_TABLE`, `WG_LLM_SPEND_CAP_CNY` | CNY per million tokens as `hit/miss/out`, and the spend cap for model calls | `0.04/2/8`, `50` |
| `WG_OUTBOUND_PROXY` | Proxy for the application's own outbound calls only | empty |
| `WG_FEISHU_APP_ID`, `WG_FEISHU_APP_SECRET`, `WG_BACKEND_URL` | Feishu adapter settings | empty, empty, `http://127.0.0.1:8100` |
| `WG_WEB_BASE_URL` | Base of the card links sent over IM | `http://127.0.0.1:8100` |

The price table is the peak-hour price of the flash model, so the spend guard over-estimates off-peak cost.

## 4. Installation and Execution

Commands run from the repository root on a clean machine.

### 4.1 Install

```bash
(cd backend && uv sync)

cp .env.example .env
for name in WG_CODE_HMAC_KEY WG_SESSION_SECRET WG_CHANNEL_SECRET; do
  sed -i "s|^$name=.*|$name=$(python3 -c 'import secrets; print(secrets.token_hex(32))')|" .env
done
sed -i 's|^WG_DEMO_MODE=.*|WG_DEMO_MODE=1|' .env

scripts/build.sh
scripts/reset-fixture.sh
```

- `uv sync` creates `backend/.venv` with the locked dependencies.
- The `sed` loop writes three random secrets into `.env` without printing them.
- `scripts/build.sh` runs `npm ci && npm run build` in `frontend/` and writes the build ID to `var/build_id`.
- `scripts/reset-fixture.sh` deletes this working copy's database and `var/` logs and loads the `fixture_v1` profile. Run it with the backend stopped.

The default `WG_LLM_PROVIDER=stub` needs no key. With the stub, the guided forms, approval cards, plans, simulator controls and every non-chat route work; the chat box needs a model. To use the real model, set `WG_LLM_PROVIDER=deepseek` and put your key in `WG_LLM_API_KEY` in `.env`.

### 4.2 Start

```bash
uv --project backend run python -m wg.api.serve
```

The backend listens on `127.0.0.1:8100` and serves the built frontend at `/`.

```bash
curl --noproxy '*' http://127.0.0.1:8100/healthz
```

returns `{"status":"ok","build":"<build ID>","demo":true,"features":[]}`. With `WG_DEMO_MODE=1`, API documentation is at `http://127.0.0.1:8100/docs`.

To work on the frontend with hot reload, run `scripts/dev.sh` instead. It starts the backend with reload on port 8100 and the Vite server on port 5170.

### 4.3 Demo logins

These are fixture handles with demo passwords for the synthetic data. Sign in at `http://127.0.0.1:8100/`.

| Role | Handle | Password |
| --- | --- | --- |
| Customer (周宁) | `demo-main` | `wg-demo-main-2026` |
| Second customer (赵一), for ownership checks | `demo-other` | `wg-demo-other-2026` |
| Demo panel (clock, faults, crash, reset) | `demo-panel` | `wg-demo-panel-2026` |
| Operator (locks, reconcile, unlock) | `demo-ops` | `wg-demo-ops-2026` |

Each role has its own cookie, so the customer screen and the demo panel can be open side by side in one browser. The simulated clock starts at 2026-11-02 09:00 (+08:00) and moves only through the demo panel or `POST /sim/clock/advance`.

### 4.4 Reset

- Backend running, demo role: the demo panel's reset button, or `POST /sim/reset` with `{"profile": "fixture_v1"}`. An optional `variant` selects a fixture variant.
- Backend stopped: `scripts/reset-fixture.sh [VARIANT]`.

A reset removes the public fund diagnostics. Load them again with:

```bash
(cd backend && uv run python -m wg.services.public_import)
```

The importer refuses to run unless every reconciliation check, the report hashes and the recomputed overlap agree with `data/public/`. Afterwards `GET /v1/funds/public-diagnostics` returns the dated, non-executable diagnostics.

### 4.5 Restart loop for the crash demo

```bash
scripts/supervise.sh
```

runs the backend and starts it again after any exit except Ctrl+C, at most 20 times, logging to `var/supervise.log`. The demo panel's crash button (`POST /sim/crash`) ends the process; on each start the backend reconciles unfinished operations and resumes plans from the database. Stop the loop with Ctrl+C.

### 4.6 Feishu channel (optional)

The adapter needs a Feishu enterprise self-built app with bot capability and long-connection event mode. Put `WG_FEISHU_APP_ID` and `WG_FEISHU_APP_SECRET` in `.env`, keep the backend running, and start the adapter:

```bash
(cd backend && uv run --extra feishu python -m wg.im.feishu)
```

In the web app, create a pairing code, then send `绑定 <code>` to the bot. Feishu replies carry facts and links to the trusted web card; approvals and codes never happen in IM.

## 5. Testing

### 5.1 Commands

| Command | What it runs |
| --- | --- |
| `scripts/check.sh` | ruff lint, ruff format check, the pytest suites (`tests/a` and B's `engines`, `goldens`, `sources`, `evals`, `im`), the write-path scan and the secret scan |
| `scripts/acceptance.sh --gate G1` | The backend gate: the 36 core P0 cases and regressions R1–R6 at layers D, H and K |
| `scripts/acceptance.sh --gate G2` | G1 plus the scripted-model variants and the other G2 cases |
| `scripts/acceptance.sh --gate G3` | The freeze gate: G2 plus T13, T77 and R7–R10 |
| `scripts/acceptance.sh --only T48,R3 --repeat 3` | Chosen cases, repeated in separate processes |
| `cd frontend && npm ci && npm run lint && npm test` | Frontend lint and unit tests |
| `cd frontend && npx playwright install chromium && npm run e2e` | Browser tests against a backend the tests start themselves |
| `scripts/eval.sh live\|comparison\|score` | Live-model trials, the coordinated-versus-flat comparison and the scorer; see [evals/README.md](evals/README.md) |

`scripts/acceptance.sh` options: `--gate`, `--only`, `--repeat`, `--layers D,H,K,S`. Each run writes `var/reports/acceptance-<build>-<UTC time>.json` and `.md` with the build ID, a frozen-build flag, the fixture hash, per-case and per-variant status, and totals by set and gate.

### 5.2 Cases and denominators

Cases are judged on database state (ledger entries, operations, receipts, positions, approvals, challenges, audit); reply wording counts only in claim cases. Every attack case has a paired legitimate case, and both must pass. The layers are D (in-process), H (HTTP to a backend subprocess), K (real process termination), S (scripted model), U (browser) and L (live model; recorded as trials, never mixed into the deterministic count).

| Set | IDs | Cases | P0 | Release status |
| --- | --- | --- | --- | --- |
| S1 transfers and security core | T1–T16 | 16 | 7 | Required |
| S2 cash and bills | T17–T38 | 22 | 8 | Required |
| S3 funds | T39–T59 | 21 | 12 | Required |
| S6 cross-scenario plan | T60–T77 | 18 | 9 | Required |
| Core | T1–T77 | 77 | 36 | Required |
| S4 cards | T78–T96 | 19 | 9 | Not started |
| S5 direct debits | T97–T114 | 18 | 8 | Designed, not implemented |
| Catalogue | T1–T114 | 114 | 53 | — |

T14 (adding a payee) is not applicable because the add-payee action is not built; the agent refuses a request to pay a new card number. T24 (credit-card pairing) is out of scope because the fixture has no credit card. A case counts as passed only when its acceptance test passes on the current build, and as reproducible when it passes three consecutive times on the frozen build (`freeze-1`) from a clean checkout. No report says "all tests pass" unless every applicable case passes.

Eight attack families are tracked: induced transfer, prompt injection, authority forgery, information extraction, replay and race, crash and restart, false result claims, and plan authority drift.

### 5.3 Results

Result numbers are kept in `evidence/` and quoted nowhere else:

- `evidence/acceptance-report.md`: build ID, gate results, counts by set and status, excluded cases, repeat runs, attack and recovery summaries.
- Live-model trials and the coordinated-versus-flat comparison: the reports listed in that file.

How the test layers are built and what each one checks is in [docs/testing.md](docs/testing.md).

### 5.4 Known limitations

- The bank, SMS provider and clock are simulators. Timing, settlement and failure behaviour follow the specification, not a real bank.
- The simulated SMS code is shown in the web app; it demonstrates step-up but is not a second channel.
- S4 (cards) is not started and S5 (direct debits) is designed but not implemented. The catalogue lists their cases as not started.
- Public fund diagnostics use dated reports (2026-06-30) and never back an executable order.
- Live-model trials are small samples and are reported as counts, not percentages. A fixed model alias does not prove the provider's model stayed unchanged; each trial records the returned model.
- The Feishu route and adapter are covered by tests that post messages to the backend. A run against a real Feishu workspace is recorded in `evidence/` only if it took place.

## 6. Repository and Deployment

- GitHub / Git repository: [to be added at release]
- Live demo: none. The prototype is intended for local use; a public deployment would need HTTPS and its own review.
- Deployment instructions: local only. Follow section 4. The backend binds to `127.0.0.1`; put a reverse proxy with TLS in front of it if it is ever exposed, set `WG_DEMO_MODE=0` and change the fixture passwords.

With `WG_DEMO_MODE=0`, the `/sim` routes, the demo login and the API documentation are disabled. A release run needs `scripts/build.sh` first, because the backend serves `frontend/dist` only when it exists.

## 7. Security Notes

Details: [docs/security.md](docs/security.md).

### 7.1 Permission tiers

| Tier | When | Requirement |
| --- | --- | --- |
| GREEN | Reads, calculations, drafts, cancelling an unsubmitted action | Signed-in session |
| YELLOW | Transfer or bill payment when the day's total, including this one, is at most ¥1,000; reserve increase; confirming a bill; plan approval | Approval on the trusted card |
| RED | Day's total above ¥1,000; reserve decrease; cancelling a bill; marking a bill paid elsewhere; fund redemption and purchase | Trusted card and a one-time code |
| FORBIDDEN | A single transfer above ¥50,000; a day's total above ¥100,000; any write under an active lock except risk-reducing ones | Refused by the gateway |

The tier is the maximum over the risk factors of the action. The gateway recomputes it inside the submitting transaction; if it rose in the meantime (for example after a parallel transfer), the action returns to step-up.

### 7.2 Approval and step-up

- Only the trusted confirm route creates an approval, from a card the server rendered. The client sends the terms hash it displayed; any mismatch with the current terms is refused.
- A red action gets a code per action and revision, never per plan. A code is bound to the exact action, is valid for five simulated minutes, allows three attempts, and is consumed by the transaction that submits the action.
- A code typed into chat is refused and removed before the model sees it.
- A plan approval covers listed actions within stated bounds. A change outside the bounds needs a new approval; bounds cannot add a payee, lower a reserve, change a product or extend the expiry.
- Approvals do not extend across restarts or clock jumps. Expiry is checked against the simulated clock inside the executing transaction.
- Every write carries an idempotency key; a repeated request returns the original operation. After a crash or a lost bank response, the operation stays unknown until reconciliation finds the bank record.
- Three consecutive unexpected failures lock a plan, and integrity violations lock the plan or customer. Only an operator can unlock, with a recorded reason, and unlocking approves nothing.

### 7.3 Untrusted text

Remarks, SMS text, merchant names and documents are opaque strings. They never set a payee, an amount or an action. The model must quote the words of the customer's message that name an amount or a payee; the quote is parsed independently and must lie outside the remark. An ambiguous or mismatching payee ends in a clarification question, and a slot taken from untrusted text is refused.

### 7.4 Data handling

- All data is synthetic. No real customer data is stored.
- The model receives only what one turn needs: masked payee handles, aggregates and fact values, and the masked user message. It never receives account numbers, codes, hashes, session tokens or raw import files.
- PINs, CVVs, card numbers and step-up codes next to keywords are replaced with `[已隐藏]` before the model, logs, audit rows and provider payloads. The original text is not stored.
- Model call records (`var/llm_calls.jsonl`) hold metadata only: model, tokens, cost, latency, error code.
- Secrets live in `.env` and the process environment. The frontend bundle contains none. API responses carry `Cache-Control: no-store`, a restrictive content security policy and no CORS in release mode.
- `scripts/reset-fixture.sh` deletes the database and logs of a working copy.

### 7.5 Known risks

- Step-up uses a simulated SMS panel; a real deployment needs a real verification channel.
- Demo passwords are in the fixture file. They are for the synthetic data only.
- The model can still misunderstand a request. The controls limit the effect (a wrong draft needs the customer's approval on a card showing the exact terms), but the customer has to read the card.
- K7 checks amounts, percents, counts and status words in the explanation. It does not judge advice quality, and the system gives no suitability advice.
- SQLite with one backend process suits a prototype; it is not a scaling design.
- Calls to the DeepSeek API leave the machine when the live provider is enabled. The spend guard limits cost, not content.
