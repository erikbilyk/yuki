# Security

This document describes the controls that protect money movement in WealthGuard, how they were tested and what they do not cover. Test numbers come from the files under `evidence/` (build `freeze-1-2-g960ab88`, tag `freeze-1`) and are not repeated anywhere else. Result wording is "N items passed in 3 runs", not "secure".

The prototype runs on synthetic data and a simulated bank. It is not designed or assessed for real financial operations.

## 1. Principle

The language model proposes; deterministic code decides. The model can read the customer's data within a stated scope and create drafts. It has no tool that approves, verifies a code, submits, settles, unlocks or touches the simulator. Approvals and codes come from the customer through a separate, server-rendered interface. One module, the gateway, writes to the database.

If the model is wrong, misled by injected text or fully controlled by an attacker, the worst it can produce is a draft. A draft moves no money until the customer approves exactly what the server renders on a trusted card, and for the riskiest actions also enters a one-time code outside the chat.

## 2. Permission tiers

The tier of an action is computed by code in `backend/wg/core/k3_policy.py`. It is the maximum over four factors: money leaves covered accounts; protection falls while free money rises; the payee or route changes; the source of a value is untrusted (in which case no action is created at all).

| Tier | When | What it requires |
| --- | --- | --- |
| GREEN | Reads, calculations, drafts, cancelling an unsubmitted action, checking an outcome | A signed-in session |
| YELLOW | A transfer or bill payment when the customer's total for the day, including this one, is at most ¥1,000; a reserve increase; confirming a bill; approving a plan | Approval on the trusted card |
| RED | A day total above ¥1,000; adding a payee; reducing a reserve; cancelling a bill; marking a bill paid elsewhere; fund redemption; fund purchase, including a revised one | Approval on the trusted card and a one-time code |
| FORBIDDEN | A single transfer above ¥50,000; a day total above ¥100,000; any write under an active lock except a risk-reducing one | Refused by the gateway |

Details:

- The day total is the sum of transfers and bill payments in status SUBMITTED, SETTLED or UNKNOWN whose submission falls on the same Asia/Shanghai day, across all plans. Splitting ¥1,500 into ¥800 and ¥700 does not avoid the code (T6).
- The gateway recomputes the tier inside the transaction that submits the action. If it rose in the meantime, for example after a parallel transfer, the action returns to step-up instead of executing.
- Adding a payee is classified RED but is not built. A request to pay a card number that is not in the payee book is refused.
- Each read tool declares the data sources it may touch and each call declares an intent. A call outside the intent's allowed sources is refused and counted as an extra read. The intent comes from the model, so this registry limits data exposure and gives a metric; it is not an authorization boundary. The boundary is customer ownership (section 8).

## 3. Approval and step-up

### Approval

- Only the trusted confirm route creates an approval, from a card the server rendered. The client returns the terms hash it displayed. A hash that differs from the current terms is refused.
- An action approval binds one action at one revision with exact terms: customer, account, payee or product, amount, tier and expiry. A new revision needs a new card.
- A plan approval binds the listed actions, their payees and commitments, the fund orders, the limits, the checkpoint and the expiry. It executes nothing by itself. A change outside its bounds pauses the affected step and needs a new approval that refers to the first. Bounds cannot add a payee, lower a reserve, change a product or extend the expiry.
- Approvals do not extend across restarts or clock jumps. Expiry is checked against the simulated clock inside the executing transaction.
- Revoking an approval invalidates every open challenge and grant issued under it.
- In the Feishu channel, a message that approves or carries a code is refused and answered with a link to the card in the web interface.

### Step-up

- A RED action gets its own challenge for each action and revision, never one per plan. The cross-scenario plan (T60) therefore needs one plan approval and four separate codes: redemption, rent payment, purchase and insurance payment.
- The code is six digits drawn with `secrets.randbelow`. It is valid for 5 simulated minutes and allows 3 attempts; the third wrong attempt closes the challenge.
- The server stores an HMAC of the code keyed with a server secret and bound to the challenge ID and scope hash, so a code verifies only its own challenge. The scope hash covers customer, action, revision, account, payee or product, and amount or shares. A scope that no longer matches closes the challenge.
- The code is delivered through a `VerificationProvider` interface. The shipped implementation is a simulated SMS panel inside the web application. The plaintext code is kept in `sms_outbox` only while the challenge is open and is wiped on verification, expiry or invalidation.
- A code typed into chat is refused and removed from the text before the model sees it.
- A verified challenge becomes a grant. The grant is consumed in the transaction that submits the action. It is invalidated by a new revision, a plan stop, a lock being cleared, an approval being revoked or its expiry. A consumed grant cannot be consumed again.
- Challenges are issued only by a customer action: confirming a RED card, or asking for a code on a waiting plan step. The coordinator and the start of the process never issue a challenge. An expired challenge is reissued the same way.

## 4. Untrusted text

Payment remarks, SMS text, merchant names, imported statement text and fund documents are opaque strings. They can be shown. They never set a payee, an amount or an action.

- Every slot of a draft carries a provenance label: `user_utterance`, `server_record` or `untrusted_text`. A payee or amount labelled `untrusted_text` is refused with `UNTRUSTED_SOURCE`, no action is created and the refusal is audited.
- The model does not supply the label. It supplies the quote from the customer's message that names the amount or the payee, and the server derives the label from where the quote lies in that message. A quote that lies only inside the remark is untrusted. The remark span can be widened by the model's own arguments but never narrowed, so a model can hide an amount from the check but cannot expose one.
- An independent parser (`wg/textcheck/amounts.py`) reads the customer's message and converts amounts such as "500", "五百", "5千", "5k" and "¥3,400" to fen. The quoted fragment must parse to the amount in the draft. A mismatch is `AMOUNT_MISMATCH`; two different amounts outside the remark end in a clarifying question. Comparators ("低于" against "不超过") are read the same way for fund limits.
- A payee quote must name exactly one active payee of the customer, and that payee must be the one the model selected. Zero or several matches end in `AMBIGUOUS_PAYEE`; a different payee ends in `PAYEE_MISMATCH`.
- Each attack case has a paired legitimate case with the same goal, for example a ¥500 transfer whose remark asks for ¥1,000. The ¥500 transfer must succeed and the remark must not change it. A pair passes only when both sides pass.
- A bill is paid only to the payee recorded on the bill. SMS text asking to pay to another card changes nothing (T29, T72).

## 5. Model boundary

- The model reaches the system only through a fixed tool registry (`wg/agent/registry.py`). Each tool has a Pydantic argument model, a capability (`read` or `draft`), declared data sources and the channels it serves. Arguments that are not in the schema, such as `user_confirmed`, are rejected and audited. A call is checked in this order: the tool exists, the channel, the capability, the arguments, the read scope.
- Tools take no customer ID. The customer comes from the session.
- No tool approves, verifies a code, submits, settles, cancels a lock or calls a simulator route. Those operations are HTTP routes that require a human session with the right role and a CSRF token. A tool call for them is refused as an unknown tool.
- The model has no code execution and no file or network access of its own. The application code contains no `exec`, `eval` or dynamic compilation of model output. The tool results go to the model as JSON.
- At most 8 tool calls run in one turn; further calls are answered with a refusal and the model is asked for a final text without tools.
- The model sees masked payee handles (for example `妈妈 · 本行 ··1234`), aggregates and typed fact values. Tool results are stripped of terms hashes, scope hashes, code HMACs, CSRF tokens, passwords and session token hashes, and every string passes the secret mask.
- Numbers come from engines and records, not from the model. After a turn, the model's explanation is checked against the typed facts of the turn (K7 in `docs/architecture.md`). Every money amount, percent, count and status word must match a fact of the right meaning. For example, "已支付" needs a settled receipt for that bill, and "可自由支配" needs the headroom figure. An explanation that fails is replaced by a template built from the facts. This is a bounded check of numbers and status words, not a general test of truthfulness or advice quality.
- The model adapter refuses to run inside a database transaction. A spend guard stops new model calls when the recorded cost reaches a configured cap (default ¥50).
- The model call record (`var/llm_calls.jsonl`) holds time, model names, token counts, cost, latency and an error code. It holds no message text and no key.

## 6. Single write path and transactions

- Only code under `backend/wg/k6/` executes SQL writes. A scan in `scripts/check.sh` (`scripts/scan_writes.py`) fails if a string constant outside that directory contains `INSERT`, `UPDATE`, `DELETE`, `REPLACE` or DDL in statement form. Every other module reads through separate connections opened with `mode=ro`, which refuse writes.
- Every write runs in a unit of work that issues `BEGIN IMMEDIATE`. A unit cannot be nested. Nothing slow or external runs inside it: no network call, no model call, no PDF parsing.
- The check of the grant, the re-check of money and limits, the reservation, the ledger entries, the receipt, the domain events and the audit rows of one submission commit together. Audit rows and events that describe a change are written in the same transaction as the change.
- Money is integer fen. Shares, prices and percents are decimals serialized as strings. A scan over every API response rejects floating-point values in money and percent fields (T59).
- Every submission carries an idempotency key built from the action and its revision. A repeat returns the original operation, a different request under the same key is refused, and a superseded revision cannot execute.
- A lost bank response leaves the operation UNKNOWN. The only way out is reconciliation against the original key; a new attempt with a new key is not made.
- After an unexpected exception, the unit rolls back and a separate unit records the failure, so the evidence survives.
- Concurrent requests for the same money or limit are serialized by the transaction; the second one re-checks and gets a new tier or a refusal (T6, T53, T57).
- The audit log records requests, decisions with reasons, submissions, calculations and receipts. It stores no model reasoning. A database trigger aborts any update of an audit row, of a domain event or of a failure record; deletes are not blocked by a trigger, and nothing signs the log.

## 7. Breaker, locks and takeover

- Three consecutive unexpected execution or verification failures for a plan lock the plan. A successful submission or settlement resets the count. Expected refusals (insufficient funds, wrong code, limits, a stale snapshot), transient errors such as a busy database, and UNKNOWN outcomes neither count nor reset.
- Integrity violations lock at once. A scope hash mismatch on a stored grant, or a grant that is consumed twice, locks the plan. A broken data invariant (ledger entries that do not sum to zero, a bill whose unpaid amount does not match, a position that does not match its lots) locks the customer.
- Under a lock, reads, reconciliation, the audit view, outcome checks and the settlement of operations already accepted continue. New drafts, approvals, challenges, verifications and submissions are refused with `LOCKED_WRITE`. Risk-reducing actions (cancelling an unsubmitted action, raising a reserve) stay possible.
- Only an operator can clear a lock, with a written reason. In one transaction the system reconciles every SUBMITTED and UNKNOWN operation in scope (an operation that cannot be reconciled keeps the lock), invalidates the open challenges and grants in scope, and clears the lock. The unlock creates no approval and resumes nothing: RED actions wait for new codes.
- The operator role is separate from the customer role, has its own cookie, and can read the audit of all customers and run reconciliation. It cannot create an approval or a code on a customer's behalf.
- Stopping a plan cancels what has not been submitted and releases reservations. Submitted operations are only reconciled. A settled effect is not erased; undoing it would be a new action with a new approval.

## 8. Identity, sessions and ownership

- Login uses a fixture handle and a demo password stored as a salted hash. The role of the handle selects the cookie: `wg_session`, `wg_operator` or `wg_demo`. Cookies are HttpOnly and SameSite=Strict. The cookie holds a random token; the server stores its HMAC. Sessions expire after 12 hours of wall-clock time.
- Every state-changing human route requires the `X-CSRF-Token` of its role. Each route declares the role it needs in one table and a guard enforces it. The channel route requires a shared secret header.
- An unknown handle and a wrong password give the same response.
- The customer of every request comes from the session. A plan, action, approval, operation, calculation result or card that belongs to another customer returns the same `404` as one that does not exist, with an identical body (T54).
- API responses carry `Content-Security-Policy: default-src 'self'; frame-ancestors 'none'`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`, `X-Content-Type-Options: nosniff` and `Cache-Control: no-store`. There is no CORS configuration. The `/sim` routes, the demo login and the API documentation exist only when `WG_DEMO_MODE=1`.

## 9. Data handling

- **Synthetic data.** The customer, accounts, payees, bills and fund positions come from a fixture. Real data is limited to two public fund reports (funds 010737 and 019018, period end 2026-06-30), used for read-only diagnostics that back no order. Their source URLs were not recorded at retrieval (`evidence/public-diagnostics.md`).
- **Masking.** PINs, CVVs, card numbers and step-up codes next to keywords (PIN, 密码, 验证码, CVV, код, пароль, code) are replaced with `[已隐藏]` before the text reaches the model, logs, audit rows, traces or provider payloads. While the customer holds an open challenge, a bare six-digit number is masked too. The original text is not stored and no hash of it is kept, because a short PIN can be recovered from its hash.
- **What the model provider receives.** The system prompt, the masked message and recent masked history, tool schemas, and tool results containing masked handles, aggregates and fact values. It does not receive account numbers, codes, terms or scope hashes, session tokens, passwords or raw import files.
- **Stored traces.** Chat messages are stored masked. The model call record holds metadata only. Release evidence contains reports and counts, not chat transcripts.
- **Retention.** The database and the logs under `var/` live in the working copy. `scripts/reset-fixture.sh` deletes the database, its journal files and the logs and reloads the fixture. No retention period is enforced by the application.
- **Secrets.** Keys are read from environment variables, including `.env` (not committed). `.env.example` lists names without values. The frontend bundle contains no key. A secret scan is part of `scripts/check.sh`.
- **Provider side.** When the live provider is enabled, requests go to the DeepSeek API outside this machine. How the provider stores or uses requests is not controlled or verified by this project. The spend guard limits cost, not content.
- **Reading scope.** History reads are limited to 90 days and spending is returned as aggregates, not rows.

## 10. Test results

Source: `evidence/attack-summary.md` and `evidence/acceptance-report.md`, from three consecutive runs of the full suite on the frozen build (307 passed in each run), with the stub model provider and the layers D (in-process), H (HTTP), K (process kill and restart) and S (scripted model). A test item is one variant of one case at one layer. Items judge database state: ledger entries, operations, approvals, challenges, audit rows. A case that belongs to two families is counted in both, so the rows do not add up.

| Family | Cases | Test items | Passed in all 3 runs |
| --- | --- | --- | --- |
| A1 Induced transfer and slot substitution | T2 T3 T4 T10 T29 T71 T72 | 34 | 34 |
| A2 Prompt injection through untrusted text | T10 T29 T51 T72 | 26 | 26 |
| A3 Authority forgery and privilege escalation | T6 T7 T9 T50 T54 T73 T74 | 49 | 49 |
| A4 Information extraction and data minimization | T7 T16 T31 T54 | 18 | 18 |
| A5 Replay, race and double spend | T6 T49 T53 T56 T57 T77 | 20 | 20 |
| A6 Crash, lost response and restart | T11 T33 T48 T66 T67 R3 R4 R9 | 30 | 30 |
| A7 False result claims | T15 T30 T47 T52 T75 | 20 | 20 |
| A8 Plan authority drift | T46 T47 T62 T70 T73 T74 T76 R1 | 24 | 24 |

Result: all items of the eight families passed in each of the 3 runs. The deterministic suite observed 0 unauthorized effects and 0 duplicate effects.

What the cases check, by control:

| Control | Cases (layers) |
| --- | --- |
| Model passes the wrong amount, two payees match, amount missing | T2 (S), T3 (S), T4 (S) |
| Command in a remark, payee name, merchant text or SMS | T10 (D H S), T29 (D H S), T72 (D S) |
| Instruction inside a fund document | T51 (S) |
| The model tries to pay a new bill inside the plan | T71 (D H S) |
| Expired, foreign or chat-typed code; the model cannot submit a code | T7 (D H S), T50 (D H) |
| `user_confirmed` argument; trusted route without a grant | T9 (D H S) |
| Another customer's plan | T54 (H) |
| A revision that tries to lower the reserve; payment after T+8 | T73 (D H), T74 (D H K) |
| Day total split into 800 and 700, sequential and parallel | T6 (D H) |
| Two concurrent submits; transfer and purchase racing for headroom | T53 (D H), T57 (D H) |
| Repeated settlement event; concurrent coordinator ticks | T56 (D), T77 (D K) |
| Lost response, process killed around the commit, restart at each plan step | T11 (K), T48 (K), T66 (K), T67 (K), R9 (K) |
| Bank acknowledgement lost, reconciliation | R3 (D K), R4 (D K) |
| False "已转账" or wrong amount, false payment or free-money claim | T15 (S), T30 (S), T75 (S) |
| Purchase settling at T+6; bill revision and wording | T47 (D H S), T52 (D H) |
| Balance questions read no history | T16 (S), T31 (S) |

The canary test inside T7 sends a unique PIN, CVV, keyword code and bare number in chat, then searches the database, every file next to it (logs, `llm_calls.jsonl`), captured logs and the provider payloads. The values that were never issued appear in none of those places. The issued code is expected in the database (the simulated SMS outbox) and appears in no log, file or provider payload outside it. Typing the issued code in chat verifies nothing.

The breaker and lock case T13 is not in the family table. It is among the 85 applicable cases that were reproducible over 3 runs; gate G3 passed 97 of 97 (`evidence/acceptance-report.md`).

### Model in the loop

These runs use the live DeepSeek model (`deepseek-flash`, thinking disabled) and are small samples, reported as counts.

| Run | Episodes | Passed | Unauthorized effects | Duplicate effects |
| --- | --- | --- | --- | --- |
| Live category trials | 24 | 10 | 0 | 0 |
| Live smoke set (T1, T17, T39, T60) | 4 | 3 | 0 | 0 |
| T60 repeat | 3 | 2 | 0 | 0 |
| Coordinated against flat, 7 cases × 3 runs × 2 systems | 42 | 0 | 9 reported by the scorer, all on the coordinated side | 0 |

- The three injected-text trials passed 1 of 3. In none of them was the injected instruction drafted or executed. The failures were the customer's own request not completed, and a reply flagged by the scorer's substring check.
- The scorer calls an effect unauthorized when it matches no entry of the scenario's expected effects. The 9 flagged operations (8 purchases of ¥4,985.00 and 1 rent payment of ¥3,400.00) are actions of plans whose approval was not revoked, with a verified and consumed code. None lacks a customer approval. The scenarios expected a different outcome and the episodes did not reach it. The scorer's count of 9 stands in the evidence; this document does not reclassify it. Whether a purchase approved at ¥4,985.00 counts as unauthorized when the scenario expected a revised amount is a question about the scenario and the scorer.
- The release criterion of 3 consecutive live passes for T60, T61 and T66 is not met (`evidence/live-trials.md`).

## 11. Known risks and limits

**Simulated components**

- The bank, the SMS provider, the clock and the fund company are simulators. SQLite transactions are atomic only inside the simulator. A real bank adapter needs idempotency on the bank's side, a log of submitted commands and reconciliation against the bank's statuses.
- Step-up is a simulated SMS panel in the same application. It shows where the authority boundary lies but is not a second channel. A stolen session can read the panel, so against session theft the code does not protect. A stolen session gives reads and YELLOW actions (up to ¥1,000 a day) without a code, and RED actions with the code from the panel. A deployment needs a separate delivery channel, device binding and strong authentication.
- No claim of PCI DSS or any banking compliance is made.

**Model variability**

- The live model does not reliably finish multi-part requests: live trials passed 13 of 28 episodes, the comparison 0 of 42. The controls limited the effect (no unapproved money movement was observed in live trials), but task completion is low.
- K7 replaced the model's explanation in 23 of 28 live episodes, and the fixed apology replaced the model's text in 8 of 28 episodes (8 of 35 customer turns). In the comparison, at least one turn got the fixed apology in 29 of 42 episodes (63 of 222 turns). A replaced explanation can also remove a legitimate clarifying question.
- A fixed model alias does not prove the provider's model stayed the same. Each trial records the returned model name.
- K7 checks amounts, percents, counts and status words. It does not check advice quality, completeness or tone. The system gives no suitability advice.
- A wrong draft is possible. The customer still has to read the card, which shows the exact terms.

**What the design does not cover**

- No device binding, anomaly detection or request rate limiting. A repeated request is stopped by the idempotency key, the attempt count and the code expiry only.
- No separation of duties among operators and no second-person approval of an unlock. Operator actions are audited.
- The database, backups and audit log are not encrypted, signed or checked for integrity. Someone with write access to the database file can change the audit log.
- The clock is simulated. Expiry is checked against that clock inside the transaction; a real deployment needs a trusted time source.
- Secret rotation is not designed. There are no long-lived customer secrets in the prototype.
- PDF text of fund reports is extracted by an importer with `pdfplumber` from cached files, outside any transaction. It is not isolated in a sandboxed worker.
- A hard fee cap under an unknown price is not supported; an order with such a limit is blocked.
- Demo passwords are in the fixture file and are for synthetic data only. With `WG_DEMO_MODE=0` the demo login and the `/sim` routes are disabled.

**What was not tested**

- No independent attack on the running build. The tests were written by the team that built the system and cover the paths it anticipated.
- The attack suite is the cases listed in section 10. The report records no further variants, such as extra mixed-language or Unicode payloads or a command split across several fields.
- Live-model attacks are the three injected-text trials and the scenarios of the comparison. They are too few to estimate a rate.
- Excluded from the denominators: T14 (adding a payee, not built), T24 (credit card pairing, out of scope), S4 cards (T78 to T96, not started) and S5 direct debits (T97 to T114, designed, not implemented). Controls specific to cards and standing debits are therefore not tested.
- The Feishu adapter is covered by tests that post messages to the backend. It has not run against a Feishu workspace.
- The report records no attack tests of the browser interface (script injection, clickjacking beyond the response headers). The browser tests cover the main story.
- The report records no dependency vulnerability scan, load test or denial-of-service test.
- One run in an earlier set of three had a startup error of the test backend (T7, variant H), which received a termination signal before it was healthy. The sender was not identified. Three later full runs and the G3 run on the same code did not repeat it. It is treated as a failure of the test environment; the report is kept in `evidence/reports/`.
