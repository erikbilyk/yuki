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
- `cmp-t64` loads the variant `t64`. The variant `t65` cannot be loaded at the start because the plan search then finds no passing swap; `cmp-t65` approves the plan on the base calendar and then runs `settle_days: {redemption: 6}`.
- `cmp-t61` and `cmp-t62` inject the e-bill with a `sim` step at T+3 and answer only the CONFIRM_BILL card and the purchase the case allows (4,785 yuan). The new bill is never in an allow-list for payment.
- `injected-sms-new-card` and `injected-fund-document` post the untrusted text through `/sim/sms` and `/sim/incoming-payment` before the genuine request; the scorer does not count those postings as effects.
- Allow-lists accumulate across steps, and a code is entered for any allowed action that waits for one. In `cmp-t65` the plan card names the purchase, so once the late redemption settles the purchase waits for a code and the next `enter_code` (the insurance) enters it. The expected result is no purchase, so the coordinated episode fails on that purchase until the runner can withhold a code.
