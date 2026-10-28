-- Core tables (SPEC §7). Times are ISO 8601 strings in Asia/Shanghai simulated time;
-- money is integer fen; shares, NAV and weights are decimal strings.

CREATE TABLE sim_clock (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    now TEXT NOT NULL,
    epoch TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

CREATE TABLE sim_profile (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    profile TEXT NOT NULL,
    variant TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE customers (
    customer_id TEXT PRIMARY KEY,
    handle TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    display_name TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE operators (
    operator_id TEXT PRIMARY KEY,
    handle TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('operator', 'demo')),
    created_at TEXT NOT NULL
);

CREATE TABLE sessions (
    session_id TEXT PRIMARY KEY,
    token_hash TEXT NOT NULL UNIQUE,
    role TEXT NOT NULL CHECK (role IN ('customer', 'operator', 'demo')),
    customer_id TEXT REFERENCES customers (customer_id),
    operator_id TEXT REFERENCES operators (operator_id),
    csrf_token TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    revoked_at TEXT,
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    CHECK ((role = 'customer') = (customer_id IS NOT NULL)),
    CHECK ((role = 'customer') = (operator_id IS NULL))
);

-- Internal accounts belong to the simulator (opening equity, clearing, fund settlement,
-- fee income) so that every operation's ledger entries sum to zero.
CREATE TABLE accounts (
    account_id TEXT PRIMARY KEY,
    customer_id TEXT REFERENCES customers (customer_id),
    kind TEXT NOT NULL CHECK (kind IN ('customer', 'internal')),
    name TEXT NOT NULL,
    currency TEXT NOT NULL DEFAULT 'CNY' CHECK (currency = 'CNY'),
    balance_fen INTEGER NOT NULL,
    covered INTEGER NOT NULL CHECK (covered IN (0, 1)),
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    CHECK ((kind = 'customer') = (customer_id IS NOT NULL)),
    CHECK (kind = 'internal' OR balance_fen >= 0)
);

CREATE TABLE payees (
    payee_id TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL REFERENCES customers (customer_id),
    alias TEXT NOT NULL,
    display_name TEXT NOT NULL,
    bank TEXT NOT NULL CHECK (bank IN ('same', 'other')),
    account_masked TEXT NOT NULL,
    account_ref TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'removed')),
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

-- Values of the profile that belong to one customer and have no table of their own:
-- the margin M of the cash snapshot and the plan constants of SPEC §23.2.
CREATE TABLE customer_settings (
    customer_id TEXT PRIMARY KEY REFERENCES customers (customer_id),
    margin_fen INTEGER NOT NULL CHECK (margin_fen >= 0),
    plan_constants_json TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

CREATE TABLE plans (
    plan_id TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL REFERENCES customers (customer_id),
    kind TEXT NOT NULL CHECK (kind IN ('single', 'rebalance', 'mixed')),
    status TEXT NOT NULL
        CHECK (status IN ('draft', 'active', 'paused', 'locked', 'completed', 'partial')),
    root_approval_id TEXT,
    active_revision INTEGER NOT NULL DEFAULT 1 CHECK (active_revision >= 1),
    mode TEXT NOT NULL CHECK (mode IN ('coordinated', 'flat')),
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

CREATE TABLE actions (
    logical_action_id TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK (revision >= 1),
    customer_id TEXT NOT NULL REFERENCES customers (customer_id),
    plan_id TEXT REFERENCES plans (plan_id),
    kind TEXT NOT NULL CHECK (kind IN (
        'TRANSFER', 'PAY_BILL', 'DESIGNATE_RESERVE', 'CONFIRM_BILL', 'RESOLVE_ITEM',
        'CANCEL_BILL', 'MARK_PAID_EXTERNAL', 'REDEEM', 'PURCHASE', 'CHECK_OUTCOME',
        'ADD_PAYEE')),
    status TEXT NOT NULL CHECK (status IN (
        'DRAFT', 'AWAITING_APPROVAL', 'AWAITING_STEPUP', 'READY', 'SUBMITTED', 'SETTLED',
        'REJECTED', 'CANCELLED', 'UNKNOWN', 'PAUSED', 'SUPERSEDED')),
    params_json TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    tier TEXT NOT NULL CHECK (tier IN ('GREEN', 'YELLOW', 'RED', 'FORBIDDEN')),
    depends_on TEXT NOT NULL DEFAULT '[]',
    not_before TEXT,
    superseded_by INTEGER,
    pause_reasons TEXT NOT NULL DEFAULT '[]',
    provenance_json TEXT NOT NULL DEFAULT '{}',
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    PRIMARY KEY (logical_action_id, revision),
    CHECK ((status = 'SUPERSEDED') = (superseded_by IS NOT NULL))
);

-- One active (non-SUPERSEDED) revision per logical action.
CREATE UNIQUE INDEX actions_one_active_revision
    ON actions (logical_action_id) WHERE status <> 'SUPERSEDED';
CREATE INDEX actions_by_plan ON actions (plan_id);
CREATE INDEX actions_by_customer ON actions (customer_id, status);

CREATE TABLE approvals (
    approval_id TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL REFERENCES customers (customer_id),
    scope_kind TEXT NOT NULL CHECK (scope_kind IN ('action', 'plan')),
    plan_id TEXT REFERENCES plans (plan_id),
    logical_action_id TEXT,
    revision INTEGER,
    terms_json TEXT NOT NULL,
    terms_hash TEXT NOT NULL,
    bounds_json TEXT,
    parent_approval_id TEXT REFERENCES approvals (approval_id),
    cash_snapshot_id TEXT,
    calc_result_id TEXT,
    expires_at TEXT NOT NULL,
    revoked_at TEXT,
    created_at TEXT NOT NULL,
    CHECK (scope_kind = 'plan' OR (logical_action_id IS NOT NULL AND revision IS NOT NULL)),
    CHECK (scope_kind = 'action' OR plan_id IS NOT NULL)
);

-- Approvals are immutable after insert; only revoked_at may be set, once.
CREATE TRIGGER approvals_immutable BEFORE UPDATE ON approvals
WHEN OLD.revoked_at IS NOT NULL
    OR NEW.approval_id IS NOT OLD.approval_id
    OR NEW.customer_id IS NOT OLD.customer_id
    OR NEW.scope_kind IS NOT OLD.scope_kind
    OR NEW.plan_id IS NOT OLD.plan_id
    OR NEW.logical_action_id IS NOT OLD.logical_action_id
    OR NEW.revision IS NOT OLD.revision
    OR NEW.terms_json IS NOT OLD.terms_json
    OR NEW.terms_hash IS NOT OLD.terms_hash
    OR NEW.bounds_json IS NOT OLD.bounds_json
    OR NEW.parent_approval_id IS NOT OLD.parent_approval_id
    OR NEW.cash_snapshot_id IS NOT OLD.cash_snapshot_id
    OR NEW.calc_result_id IS NOT OLD.calc_result_id
    OR NEW.expires_at IS NOT OLD.expires_at
    OR NEW.created_at IS NOT OLD.created_at
BEGIN
    SELECT RAISE(ABORT, 'approvals are immutable except revoked_at');
END;

CREATE TABLE challenges (
    challenge_id TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL REFERENCES customers (customer_id),
    approval_id TEXT NOT NULL REFERENCES approvals (approval_id),
    logical_action_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    scope_hash TEXT NOT NULL,
    code_hmac TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    max_attempts INTEGER NOT NULL DEFAULT 3 CHECK (max_attempts >= 1),
    issued_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    verified_at TEXT,
    consumed_at TEXT,
    invalidated_at TEXT,
    invalidation_reason TEXT,
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    FOREIGN KEY (logical_action_id, revision) REFERENCES actions (logical_action_id, revision),
    CHECK (attempts <= max_attempts),
    CHECK (consumed_at IS NULL OR verified_at IS NOT NULL),
    CHECK ((invalidated_at IS NULL) = (invalidation_reason IS NULL))
);

CREATE TABLE sms_outbox (
    sms_id INTEGER PRIMARY KEY,
    customer_id TEXT NOT NULL REFERENCES customers (customer_id),
    challenge_id TEXT NOT NULL UNIQUE REFERENCES challenges (challenge_id),
    code TEXT,
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

-- Customer operations carry their logical action; simulator operations (OPENING,
-- incoming payments, external debits) have none.
CREATE TABLE operations (
    operation_id TEXT PRIMARY KEY,
    logical_action_id TEXT,
    revision INTEGER,
    idempotency_key TEXT NOT NULL UNIQUE,
    request_hash TEXT NOT NULL,
    kind TEXT NOT NULL,
    customer_id TEXT REFERENCES customers (customer_id),
    account_id TEXT REFERENCES accounts (account_id),
    amount_fen INTEGER CHECK (amount_fen IS NULL OR amount_fen >= 0),
    shares TEXT,
    product_id TEXT,
    payee_id TEXT REFERENCES payees (payee_id),
    commitment_id TEXT,
    status TEXT NOT NULL CHECK (status IN ('SUBMITTED', 'SETTLED', 'REJECTED', 'UNKNOWN')),
    submitted_at TEXT NOT NULL,
    settle_due_at TEXT,
    settled_at TEXT,
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    FOREIGN KEY (logical_action_id, revision) REFERENCES actions (logical_action_id, revision),
    CHECK ((logical_action_id IS NULL) = (revision IS NULL))
);

CREATE INDEX operations_by_customer_day ON operations (customer_id, submitted_at);
CREATE INDEX operations_by_status ON operations (status);

CREATE TABLE operation_events (
    operation_event_id INTEGER PRIMARY KEY,
    operation_id TEXT NOT NULL REFERENCES operations (operation_id),
    event TEXT NOT NULL CHECK (event IN (
        'SUBMITTED', 'SETTLED', 'REJECTED', 'UNKNOWN_OBSERVED', 'RECONCILED')),
    reason TEXT,
    at TEXT NOT NULL
);

CREATE TABLE reservations (
    reservation_id TEXT PRIMARY KEY,
    operation_id TEXT NOT NULL UNIQUE REFERENCES operations (operation_id),
    purpose_id TEXT,
    account_id TEXT NOT NULL REFERENCES accounts (account_id),
    amount_fen INTEGER NOT NULL CHECK (amount_fen > 0),
    state TEXT NOT NULL CHECK (state IN ('ACTIVE', 'CONSUMED', 'RELEASED')),
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

-- A reservation is consumed or released once and never comes back.
CREATE TRIGGER reservations_state_once BEFORE UPDATE OF state ON reservations
WHEN OLD.state <> 'ACTIVE' AND NEW.state IS NOT OLD.state
BEGIN
    SELECT RAISE(ABORT, 'reservation already consumed or released');
END;

CREATE TABLE ledger_entries (
    entry_id INTEGER PRIMARY KEY,
    operation_id TEXT NOT NULL REFERENCES operations (operation_id),
    account_id TEXT NOT NULL REFERENCES accounts (account_id),
    amount_fen INTEGER NOT NULL CHECK (amount_fen <> 0),
    posted_at TEXT NOT NULL
);

CREATE INDEX ledger_entries_by_account ON ledger_entries (account_id);
CREATE INDEX ledger_entries_by_operation ON ledger_entries (operation_id);

CREATE TABLE receipts (
    receipt_id TEXT PRIMARY KEY,
    operation_id TEXT NOT NULL UNIQUE REFERENCES operations (operation_id),
    status TEXT NOT NULL CHECK (status IN ('SETTLED', 'REJECTED')),
    terms_hash TEXT,
    effect_json TEXT NOT NULL,
    reason_code TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE bank_requests (
    idempotency_key TEXT PRIMARY KEY,
    status TEXT NOT NULL CHECK (status IN ('EXECUTED', 'REJECTED')),
    effect_json TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

-- event_id is "evt_" plus the zero-padded sequence, so event_id order is insertion order.
CREATE TABLE domain_events (
    seq INTEGER PRIMARY KEY,
    event_id TEXT NOT NULL UNIQUE,
    type TEXT NOT NULL,
    customer_id TEXT,
    plan_id TEXT,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE event_cursors (
    consumer TEXT PRIMARY KEY,
    last_event_id TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

CREATE TABLE audit_log (
    audit_id INTEGER PRIMARY KEY,
    at TEXT NOT NULL,
    actor TEXT NOT NULL
        CHECK (actor IN ('customer', 'agent', 'coordinator', 'operator', 'system')),
    event TEXT NOT NULL,
    customer_id TEXT,
    ref TEXT,
    decision TEXT,
    reason TEXT
);

CREATE INDEX audit_log_by_customer ON audit_log (customer_id, audit_id);

CREATE TABLE locks (
    lock_id TEXT PRIMARY KEY,
    scope TEXT NOT NULL CHECK (scope IN ('plan', 'customer', 'global')),
    scope_ref TEXT NOT NULL,
    reason TEXT NOT NULL,
    since TEXT NOT NULL,
    cleared_at TEXT,
    cleared_by TEXT REFERENCES operators (operator_id),
    clear_reason TEXT,
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    CHECK ((cleared_at IS NULL) = (cleared_by IS NULL)),
    CHECK ((cleared_at IS NULL) = (clear_reason IS NULL))
);

CREATE UNIQUE INDEX locks_one_active_per_scope ON locks (scope, scope_ref)
    WHERE cleared_at IS NULL;

CREATE TABLE breaker_state (
    plan_id TEXT PRIMARY KEY REFERENCES plans (plan_id),
    consecutive_unexpected INTEGER NOT NULL DEFAULT 0 CHECK (consecutive_unexpected >= 0),
    last_failure_at TEXT,
    last_success_at TEXT,
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

-- No foreign keys: a failure record must survive even when the unit that failed was the
-- one creating the plan or action it names.
CREATE TABLE failure_records (
    failure_id INTEGER PRIMARY KEY,
    at TEXT NOT NULL,
    plan_id TEXT,
    logical_action_id TEXT,
    error_class TEXT NOT NULL,
    detail TEXT NOT NULL
);

CREATE TABLE faults (
    fault TEXT PRIMARY KEY CHECK (fault IN (
        'crash_before_commit', 'crash_after_commit', 'pause_before_commit',
        'pause_after_commit', 'drop_response', 'bank_ack_lost', 'settlement_reject',
        'probe_timeout', 'busy', 'unexpected', 'tick_crash')),
    remaining_count INTEGER NOT NULL CHECK (remaining_count >= 1),
    persistent INTEGER NOT NULL CHECK (persistent IN (0, 1)),
    armed_at TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE chat_messages (
    message_id INTEGER PRIMARY KEY,
    session_id TEXT NOT NULL,
    customer_id TEXT NOT NULL REFERENCES customers (customer_id),
    channel TEXT NOT NULL CHECK (channel IN ('web', 'im')),
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    masked_text TEXT NOT NULL,
    k7_replaced INTEGER NOT NULL DEFAULT 0 CHECK (k7_replaced IN (0, 1)),
    created_at TEXT NOT NULL
);

CREATE INDEX chat_messages_by_customer ON chat_messages (customer_id, message_id);

CREATE TABLE read_scopes (
    intent TEXT PRIMARY KEY,
    allowed_sources TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- Append-only and immutable rows.
CREATE TRIGGER operation_events_append_only BEFORE UPDATE ON operation_events
BEGIN
    SELECT RAISE(ABORT, 'operation_events is append-only');
END;

CREATE TRIGGER ledger_entries_append_only BEFORE UPDATE ON ledger_entries
BEGIN
    SELECT RAISE(ABORT, 'ledger_entries is append-only');
END;

CREATE TRIGGER receipts_immutable BEFORE UPDATE ON receipts
BEGIN
    SELECT RAISE(ABORT, 'receipts are immutable');
END;

CREATE TRIGGER domain_events_append_only BEFORE UPDATE ON domain_events
BEGIN
    SELECT RAISE(ABORT, 'domain_events is append-only');
END;

CREATE TRIGGER audit_log_append_only BEFORE UPDATE ON audit_log
BEGIN
    SELECT RAISE(ABORT, 'audit_log is append-only');
END;

CREATE TRIGGER failure_records_append_only BEFORE UPDATE ON failure_records
BEGIN
    SELECT RAISE(ABORT, 'failure_records is append-only');
END;
