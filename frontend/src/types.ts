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
  | "ADD_PAYEE"
  | "DEBIT_PAY"
  | "DEBIT_DECLINE"
  | "MANDATE_CANCEL"
  | "MANDATE_SUSPEND"
  | "MANDATE_RESUME"
  | "MANDATE_CAP_LOWER"
  | "MANDATE_CAP_RAISE"
  | "MANDATE_CREATE";
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
  settle_due_at: string | null;
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
  // Direct-debit kinds only; effect_check is set only for cancel, suspend and cap lower.
  mandate_ref?: string | null;
  request_id?: string | null;
  merchant_display?: string | null;
  effect_check?: EffectCheck | null;
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

export type PlanKind = "rebalance" | "mixed" | "single" | string;
export type PlanStatus = "draft" | "active" | "paused" | "completed" | "partial" | "locked";
export type RevisionReason = "initial" | "bill_change" | "new_approval";
export type CheckpointResult = "passed" | "failed" | "checkpoint_missed";

export interface PlanNodeView {
  logical_action_id: string;
  revision: number;
  kind: ActionKind;
  status: ActionStatus;
  phrase: NodePhrase;
  tier: Tier;
  depends_on: string[];
  not_before: string | null;
  due_date: string | null;
  amount_fen: number | null;
  shares: string | null;
  product_id: string | null;
  payee_id: string | null;
  payee_display: string | null;
  commitment_id: string | null;
  pause_reasons: string[];
  challenge_expires_at: string | null;
  card_id: string | null;
  operation_id: string | null;
  receipt_id: string | null;
  submitted_at: string | null;
  settled_at: string | null;
}

export interface PlanRevisionView {
  revision: number;
  reason: RevisionReason;
  trigger_event_id: string | null;
  cash_snapshot_id: string;
  calc_result_id: string | null;
  approval_id: string | null;
  within_bounds: boolean;
  purchase_before_fen: number | null;
  purchase_after_fen: number | null;
  created_at: string;
}

export interface CheckpointView {
  logical_action_id: string;
  result: CheckpointResult;
  measured_pct: string;
  lower_pct: string;
  upper_pct: string;
  target_max_pct: string;
  quality: PercentQuality;
  evaluated_at: string;
}

export interface PlanSummary {
  plan_id: string;
  kind: PlanKind;
  status: PlanStatus;
  mode: string;
  created_at: string;
  expires_at: string | null;
  next_operation: NextOperationView | null;
}

export interface PlanView extends PlanSummary {
  active_revision: number;
  root_approval_id: string | null;
  card_id: string | null;
  nodes: PlanNodeView[];
  revisions: PlanRevisionView[];
  checkpoint: CheckpointView | null;
}

export interface AuditLine {
  at: string;
  actor: string;
  event: string;
  ref: string | null;
  decision: string | null;
  reason: string | null;
  text: string;
}

export type EffectCheck = "verified" | "pending" | "failed";
export type MandateStatus = "ACTIVE" | "SUSPENDED" | "CANCELLED" | "EXPIRED";
export type MandateChange = "cancel" | "suspend" | "resume" | "set_cap";
export type DebitRequestStatus =
  | "AWAITING_CLIENT"
  | "PAID"
  | "DECLINED"
  | "EXPIRED"
  | "AUTO_DECLINED"
  | "OUT_OF_MANDATE";
export type DebitReason =
  | "NO_MANDATE"
  | "MANDATE_SUSPENDED"
  | "MANDATE_CANCELLED"
  | "MANDATE_EXPIRED"
  | "CAP"
  | "PERIOD_USED"
  | "EXPIRED";
export type DebitDecision = "pay" | "decline";

export interface ProtectionPeriodView {
  period_key: string;
  due_date: string;
  expected_fen: number;
  until: string | null;
}

export interface MandateView {
  mandate_ref: string;
  merchant_id: string;
  merchant_display: string;
  account_id: string;
  status: MandateStatus;
  expected_fen: number;
  cap_fen: number;
  period: "monthly";
  debit_day: number;
  next_debit_date: string | null;
  starts_on: string;
  expires_on: string;
  version: number;
  protection: ProtectionPeriodView[];
  pending_requests: number;
}

export interface MandatesView {
  as_of: string;
  monthly_total_fen: number;
  mandates: MandateView[];
}

export interface DebitRequestView {
  request_id: string;
  mandate_ref: string;
  merchant_id: string;
  merchant_display: string;
  amount_fen: number;
  currency: "CNY";
  period_key: string;
  status: DebitRequestStatus;
  reason_code: DebitReason | null;
  received_at: string;
  respond_by: string;
  description_raw: string;
  payable: boolean;
  operation_id: string | null;
}

export interface DebitRequestsView {
  requests: DebitRequestView[];
}

export interface SimDebitResult {
  request_id: string;
  status: DebitRequestStatus;
  reason_code: DebitReason | null;
  respond_by: string;
  duplicate: boolean;
}

export interface SimDebitBody {
  merchant_id: string;
  merchant_request_id: string;
  mandate_ref: string;
  amount_fen: number;
  currency: string;
  description_raw: string;
}

export interface HealthView {
  status: "ok";
  build: string;
  demo: boolean;
  features: string[];
}

export interface ClockView {
  now: string;
  epoch: string;
  day: number;
}

export type FaultName =
  | "crash_before_commit"
  | "crash_after_commit"
  | "pause_before_commit"
  | "pause_after_commit"
  | "drop_response"
  | "bank_ack_lost"
  | "settlement_reject"
  | "probe_timeout"
  | "busy"
  | "unexpected"
  | "tick_crash";

export interface FaultView {
  fault: FaultName;
  remaining_count: number;
  persistent: boolean;
  armed_at: string;
}

export interface SimResetResult {
  profile: string;
  variant: string | null;
  clock: ClockView;
}
