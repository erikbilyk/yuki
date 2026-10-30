# Scenarios

Each file is one customer script in the runner's format (`evals/README.md`); the pass criteria are in its `expectations` (`evals/score/README.md`).

| Directory | Run with | Content |
| --- | --- | --- |
| `live/` | `scripts/eval.sh live --scenarios evals/scenarios/live` | Trials per category: ambiguity, missing slots, multi-step, injected text, stale data, grounded explanation, colloquial and mixed-language input, out-of-scope requests |
| `comparison/` | `scripts/eval.sh comparison --scenarios evals/scenarios/comparison` | One script per case, run on the coordinator and on the flat agent |
| `smoke/` | `scripts/eval.sh live --scenarios evals/scenarios/smoke` | Four short runs for checking a build: T1, T17, T39, T60 |

Conventions:

- A comparison script is the same for both systems. After the opening request it sends a message on each due date ("今天房租到期了，请帮我交掉") because the flat agent acts only when asked; the coordinator treats the same message as a status question. Every `approve_cards` step lists the plan card and the single-order cards, so one allow-list serves both systems.
- Allow-lists approve only the terms of the opening request: amounts are exact, payees and products are named. The scorer judges the effects against `expectations.effects` independently of what the allow-list let through.
- Customer text is Chinese, with one Russian and one English request among the colloquial scenarios.
- `colloquial-english-dad` asks for 5,000 yuan while 2,000 yuan is free; the expected result is an infeasible draft and no transfer.
- `stale-expired-code` lets the code of a 1,500 yuan transfer expire; the runner then asks for a fresh code, so the transfer executes once with two codes issued and one consumed.
- `cmp-t64` loads the variant `t64`. The variant `t65` cannot be loaded at the start because the plan search then finds no passing swap; T65 needs the redemption delay to apply after the plan is approved.
