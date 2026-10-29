import { formatTime } from "./format.ts";
import { S } from "./strings.ts";
import type { PlanNodeView } from "./types.ts";

/** The plain sentence for a plan node, from its phrase key and its own dates (S6 §8). */
export function nodeSentence(node: PlanNodeView): string {
  switch (node.phrase) {
    case "scheduled":
      if (node.not_before) return S.plan.scheduledAt(formatTime(node.not_before));
      return node.depends_on.length > 0 ? S.plan.scheduledAfter : S.plan.scheduledPlain;
    case "awaiting_code":
      return S.plan.needsCode;
    case "needs_decision":
      return S.plan.needsDecision;
    case "submitted":
      return S.plan.submitted;
    case "checking":
      return S.plan.checking;
    case "done":
      return S.plan.done;
    case "ready":
      return S.plan.ready;
    case "awaiting_approval":
      return S.plan.awaitingApproval;
    case "rejected":
      return S.plan.rejected;
    case "cancelled":
      return S.plan.cancelled;
    case "superseded":
      return S.plan.superseded;
  }
}

export function pauseReasonText(code: string): string {
  return S.plan.pauseReasons[code] ?? S.plan.pauseReasonOther;
}
