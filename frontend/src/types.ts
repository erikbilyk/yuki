// Response shapes of the HTTP API (backend/wg/contracts/api.py). Fen are integers; Decimals are strings.

export type Tier = "GREEN" | "YELLOW" | "RED" | "FORBIDDEN";
export type ActionStatus =
  | "DRAFT"
  | "AWAITING_APPROVAL"
  | "AWAITING_STEPUP"
  | "READY"
  | "SUBMITTED"
  | "SETTLED"
  | "REJECTED"
  | "CANCELLED"
  | "UNKNOWN"
  | "PAUSED"
  | "SUPERSEDED";
export type OperationStatus = "SUBMITTED" | "SETTLED" | "REJECTED" | "UNKNOWN";
export type ActionKind =
  | "TRANSFER"
  | "PAY_BILL"
  | "DESIGNATE_RESERVE"
  | "CONFIRM_BILL"
  | "RESOLVE_ITEM"
  | "CANCEL_BILL"
  | "MARK_PAID_EXTERNAL"
  | "REDEEM"
  | "PURCHASE"
  | "CHECK_OUTCOME"
  | "ADD_PAYEE";
export type Provenance = "user_utterance" | "server_record" | "untrusted_text";
export type PercentQuality = "estimated" | "confirmed";
export type NodePhrase =
  | "scheduled"
  | "awaiting_approval"
  | "awaiting_code"
  | "ready"
  | "submitted"
  | "checking"
  | "done"
  | "rejected"
  | "cancelled"
  | "needs_decision"
  | "superseded";

export interface ErrorBody {
  code: string;
  message_zh: string;
  detail: Record<string, unknown>;
}

export interface SessionEntry {
  role: "customer" | "operator" | "demo";
  subject_id: string;
  display_name: string | null;
  csrf_token: string;
  expires_at: string;
}

export interface SessionView {
  sessions: SessionEntry[];
}

export interface AccountView {
  account_id: string;
  name: string;
  posted_fen: number;
  available_fen: number;
  covered: boolean;
}

export interface PayeeView {
  payee_id: string;
  alias: string;
  display_name: string;
  bank: string;
  account_masked: string;
}

export interface NextOperationView {
  logical_action_id: string;
  plan_id: string | null;
  kind: ActionKind;
  status: ActionStatus;
  phrase: NodePhrase;
  amount_fen: number | null;
  shares: string | null;
  not_before: string | null;
}

export interface AccountAvailability {
  account_id: string;
  name: string;
  covered: boolean;
  posted_fen: number;
  available_fen: number;
  reconciled: boolean | null;
}

export interface Assumption {
  kind: string;
  ref: string;
}

export interface SnapshotQuality {
  as_of: string;
  uncovered_accounts: string[];
  unresolved_count: number;
  unresolved_impact_fen: number;
  reconciled: Record<string, boolean>;
  assumptions: Assumption[];
  incomplete: boolean;
}

export interface CashDetails {
  rules_version: string;
  c_fen: number;
  e_fen: number;
  l_fen: number;
  b_fen: number;
  m_fen: number;
  u_fen: number;
  accounts: AccountAvailability[];
  monthly_estimate_fen: number;
  quality: SnapshotQuality;
}

export interface CashSnapshotView {
  snapshot_id: string;
  as_of: string;
  mode: "headroom" | "shortfall";
  headroom_fen: number;
  shortfall_fen: number;
  protected_fen: number;
  next_operation: NextOperationView | null;
  unpaid_bills_fen: number;
  unpaid_bills_count: number;
  details: CashDetails;
}

export interface DraftResult {
  logical_action_id: string;
  revision: number;
  kind: ActionKind;
  status: ActionStatus;
  tier: Tier;
  feasible: boolean;
  reasons: string[];
}

export interface ApprovalRequestResult {
  logical_action_id: string;
  revision: number;
  status: ActionStatus;
  tier: Tier;
  card_id: string;
}

export interface CardLine {
  key: string;
  label: string;
  text: string;
}

export type CardChange =
  | { unit: "fen"; key: string; label: string; before: number | null; after: number }
  | {
      unit: "percent";
      key: string;
      label: string;
      before: string | null;
      after: string;
      quality: PercentQuality;
    };

export interface SourceLabel {
  slot: string;
  provenance: Provenance;
  label: string;
}

export interface Card {
  card_id: string;
  kind: "action" | "plan" | "plan_revision";
  status: "open" | "confirmed" | "expired" | "superseded";
  title: string;
  tier: "GREEN" | "YELLOW" | "RED";
  requires_code: boolean;
  logical_action_id: string | null;
  revision: number | null;
  action_kind: ActionKind | null;
  plan_id: string | null;
  approval_id: string | null;
  lines: CardLine[];
  changes: CardChange[];
  sources: SourceLabel[];
  warnings: string[];
  terms_hash: string;
  expires_at: string;
}

export interface OperationView {
  operation_id: string;
  logical_action_id: string;
  revision: number;
  plan_id: string | null;
  kind: ActionKind;
  status: OperationStatus;
  account_id: string;
  amount_fen: number | null;
  payee_id: string | null;
  submitted_at: string;
  settled_at: string | null;
  receipt_id: string | null;
}

export interface ReceiptView {
  receipt_id: string;
  operation_id: string;
  logical_action_id: string;
  status: "SETTLED" | "REJECTED";
  kind: ActionKind;
  account_id: string;
  amount_fen: number | null;
  fee_fen: number | null;
  net_fen: number | null;
  payee_display: string | null;
  unpaid_after_fen: number | null;
  reason_code: string | null;
  created_at: string;
}

export interface ChallengeView {
  challenge_id: string;
  logical_action_id: string;
  revision: number;
  expires_at: string;
  attempts_left: number;
}

export interface ConfirmResult {
  card_id: string;
  approval_id: string;
  plan_id: string | null;
  logical_action_id: string | null;
  action_status: ActionStatus | null;
  challenge: ChallengeView | null;
  operation: OperationView | null;
}

export interface VerifyResult {
  challenge_id: string;
  logical_action_id: string;
  revision: number;
  action_status: ActionStatus;
  operation: OperationView | null;
}

export interface SmsMessage {
  challenge_id: string;
  logical_action_id: string;
  kind: ActionKind;
  amount_fen: number | null;
  code: string;
  created_at: string;
  expires_at: string;
}

export interface CancelResult {
  logical_action_id: string;
  revision: number;
  status: ActionStatus;
  reservation_released: boolean;
}

export interface ChatReply {
  disposition: string | null;
  fact_lines: string[];
  explanation: string | null;
  explanation_replaced: boolean;
  card_ids: string[];
  logical_action_ids: string[];
  plan_id: string | null;
}

export interface ChatMessageView {
  role: "user" | "assistant";
  channel: "web" | "im";
  text: string;
  explanation_replaced: boolean;
  created_at: string;
}
