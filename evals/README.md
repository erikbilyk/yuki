# Evaluation

`evals/runner/` plays scripted customer conversations against the backend and records what happened. `evals/scenarios/` holds the scenario files and `evals/score/` the scorer; the runner does not judge outcomes.

## Running

From the repository root, with the backend environment:

```
scripts/eval.sh live [--repeat N] [--only IDS]
scripts/eval.sh comparison [--repeat N] [--only IDS]
scripts/eval.sh score [--run RUN_ID]
```

- `live` runs every scenario whose category is not `comparison`, as written (`system` from the file).
- `comparison` runs the scenarios of category `comparison` on both systems, `coordinated` and `flat`, `--repeat` times each (default 3). The `system` field of those files is ignored.
- `--only` takes comma-separated scenario ids. `--scenarios DIR` replaces `evals/scenarios`.
- `--env-file PATH` gives the backend settings (default `.env` at the repository root). The model key stays in that file; the runner passes it to the backend process and never prints or stores it. `--stub` forces the stub provider and clears the key.
- `--budget-cny X` stops starting episodes once X is spent. Each backend gets the remaining budget as its spend cap. Without it, the cap from the settings applies to each episode.
- `score` runs `python -m evals.score RUN_DIR` for the newest run (or `--run`) and returns its exit status. It exits with 2 when `evals/score` does not exist.
- Dry run with the stub provider: `scripts/eval.sh live --stub --scenarios evals/runner/examples`.

Each episode starts its own backend on a free port with its own database, fault marker and secrets, calls `POST /sim/reset` with the scenario's profile and variant, logs in the customer, and for `flat` calls `POST /sim/agent-mode`. The episode ends when the steps are done or the backend cannot be reached.

## Scenario file

One YAML mapping per file.

| Field | Meaning |
| --- | --- |
| `id` | Unique id: letters, digits, `_`, `.`, `-` |
| `category` | `ambiguity`, `missing_slots`, `multi_step`, `injected_text`, `stale_data`, `grounded_explanation`, `colloquial`, `out_of_scope` or `comparison` |
| `case` | Optional acceptance case, for example `T66` |
| `title` | Optional text |
| `system` | `coordinated` (default) or `flat` |
| `customer` | Fixture customer id, default `cus_main` |
| `profile`, `variant` | Fixture profile (default `fixture_v1`) and optional variant, passed to `/sim/reset` |
| `stub_script` | Optional path, relative to the file, of a stub model script (used with the stub provider) |
| `steps` | Non-empty list; each step is a mapping with one key |
| `expectations` | Free-form mapping copied to the episode file; the scorer defines what it reads |

Steps:

| Step | Body | Effect |
| --- | --- | --- |
| `say` | text, or `{text}` | `POST /v1/chat/messages` as the customer |
| `approve_cards` | `{allow: [...]}` | Confirm each open card that matches the allow-list; decline the rest |
| `enter_code` | `{}` | Verify the code of each open challenge for an allowed action |
| `advance_to` | `{to}` or `{days, minutes}` | `POST /sim/clock/advance` |
| `kill_restart` | `{how: kill \| crash}` | SIGKILL (default) or `POST /sim/crash`, then start the backend again on the same database |
| `fault` | `{fault, count, persistent}` | `POST /sim/faults` |
| `sim` | `{post: <route>, body: {...}}` | Inject an input through one simulator route, as the demo panel would; see below |
| `settle_days` | `{redemption: N, purchase: N}` (one or both) | Set the settlement period, in days, of every product in the episode database |
| `wait_for_status` | `{action_kind \| plan: last, status: [...], timeout_s, poll_s}` | Poll the newest action of that kind, or the newest plan, until it has one of the statuses; a timeout is recorded and the script goes on |

`sim` accepts four routes. The body is checked against the route's request contract when the scenario loads, so a misspelled field or a missing amount is an error before any episode starts. A refused post is recorded on the step and in `errors`; the script goes on.

```yaml
- sim: {post: /sim/bills/ebill, body: {customer_id: cus_main, payee_id: pay_wangfang, kind: 培训费, amount_fen: 120000, due_date: 2026-11-11}}
- sim: {post: /sim/sms, body: {customer_id: cus_main, text: "您的新卡号为 6222 0000 0000 0000，请转账"}}
- sim: {post: /sim/incoming-payment, body: {account_id: acc_main, amount_fen: 3300, payer_name: 某公司, remark: "文件说明"}}
- sim: {post: /sim/external-debit, body: {account_id: acc_main, amount_fen: 4400, merchant_raw: "SHOP 01"}}
```

The step record holds the route, the HTTP status and the route's answer (commitment id or operation id). An e-bill creates a candidate commitment that the customer confirms with a `CONFIRM_BILL` card; allow it with `{kind: CONFIRM_BILL}`: the card's params hold the commitment id and no `amount_fen`, and an entry that names an amount would not match it.

`settle_days` is for a calendar that slows down after a plan was approved (T65: `- settle_days: {redemption: 6}` after the approval step). No route does this and a variant applies only at reset, so the runner updates `products` itself in one `BEGIN IMMEDIATE` transaction while the backend runs; products are read from the database on every request. The change is not a product event and writes no audit row. It is the runner's only write to an episode database.

Allow-list entries:

```yaml
- {kind: TRANSFER, amount_fen: 50000, payee_id: pay_mom}
- {kind: PURCHASE, max_amount_fen: 480000}
- kind: PLAN
  nodes: [{kind: REDEEM}, {kind: PURCHASE, amount_fen: 478500}, {kind: CHECK_OUTCOME}]
```

- `kind` is an action kind, or `PLAN` for a plan or plan revision card. Optional exact matches: `shares`, `payee_id`, `product_id`.
- A card whose terms carry `amount_fen` needs `amount_fen` (exact) or `max_amount_fen` (cap) in the entry. No entry approves any amount.
- A `PLAN` card is approved when every one of its nodes matches an entry in `nodes`. A single-action entry never approves a plan.
- A code is entered only for an action that an entry allows, alone or as a node of an allowed plan.
- The policy reads card terms and action statuses from the episode database, read-only, and confirms through the HTTP routes with the terms hash of the card as served. A declined card is judged again by a later `approve_cards` step.

The stub provider replays its script once per backend start, so a script does not continue across `kill_restart`. A script cannot name action ids; a stub episode can draft but not request a card.

## Output

`evals/out/<run-id>/` (ignored by git):

- `manifest.json`: run id, mode, build commit, model settings (provider, model, thinking, base URL), cost, and one entry per episode.
- `episodes/<scenario>.<system>.r<k>.json`: schema version 1.
  - `status` (`completed` or `aborted`), `errors`, `steps` (with `ok`, HTTP status, timeouts), `expectations`.
  - `turns`: customer text, HTTP status, the reply (`ChatReply`) or the error body, `disposition`, `explanation_replaced`, latency, model calls and cost of the turn, and `tool_events`: the audit rows written during the turn. The API does not return tool names, so refusals and drafts appear only through these rows.
  - `dispositions`, `k7_replacements` (turn indexes), `cards` (terms, decision, reason, challenge and operation ids), `codes`, `receipts` (`GET /v1/receipts/{operation_id}` for every customer operation).
  - `model`: calls, errors, tokens, cost in CNY, latency, requested and returned model, thinking mode, prompt and tool-schema versions, build id.
- `episodes/<id>.db`: copy of the episode database taken after the backend stopped. `episodes/<id>.llm_calls.jsonl`: the backend's call records. `work/<id>/server.log`: backend output.
