import { api } from "./api.ts";
import type { DraftResult } from "./types.ts";

/** Ask for the trusted card of a feasible draft; nothing executes until the customer confirms it. */
export async function openCard(draft: DraftResult): Promise<string> {
  const requested = await api.requestApproval(draft.logical_action_id);
  return requested.card_id;
}
