-- foreign_keys: off
-- Direct-debit agreements (S5): the merchant registry, agreements and their change events,
-- merchants' debit requests and the probes that check an agreement change took effect.
-- `actions` is rebuilt to accept the direct-debit kinds. SQLite cannot widen a CHECK in
-- place, and dropping a parent table with foreign keys on fails, so this migration runs
-- with foreign keys off and must pass `PRAGMA foreign_key_check` before it commits.

CREATE TABLE merchants (
    merchant_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    aliases_json TEXT NOT NULL DEFAULT '[]',
    settlement_account_ref TEXT NOT NULL,
    verified INTEGER NOT NULL CHECK (verified IN (0, 1)),
    created_at TEXT NOT NULL
);

CREATE TABLE mandates (
    mandate_ref TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL REFERENCES customers (customer_id),
    account_id TEXT NOT NULL REFERENCES accounts (account_id),
    merchant_id TEXT NOT NULL REFERENCES merchants (merchant_id),
    purpose_id TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL DEFAULT 'pull' CHECK (kind = 'pull'),
    expected_fen INTEGER NOT NULL CHECK (expected_fen > 0),
    cap_fen INTEGER NOT NULL CHECK (cap_fen > 0),
    period TEXT NOT NULL DEFAULT 'monthly' CHECK (period = 'monthly'),
    debit_day INTEGER NOT NULL CHECK (debit_day BETWEEN 1 AND 31),
    max_per_period INTEGER NOT NULL DEFAULT 1 CHECK (max_per_period = 1),
    starts_on TEXT NOT NULL,
    expires_on TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('ACTIVE', 'SUSPENDED', 'CANCELLED', 'EXPIRED')),
    cancelled_at TEXT,
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    CHECK (expires_on >= starts_on AND expires_on <= date(starts_on, '+12 months')),
    CHECK ((status = 'CANCELLED') = (cancelled_at IS NOT NULL))
);

CREATE INDEX mandates_by_customer ON mandates (customer_id);

-- An agreement's parties, purpose and term never change; cancelled_at is set once.
CREATE TRIGGER mandates_fixed_terms BEFORE UPDATE ON mandates
WHEN NEW.mandate_ref IS NOT OLD.mandate_ref
    OR NEW.customer_id IS NOT OLD.customer_id
    OR NEW.account_id IS NOT OLD.account_id
    OR NEW.merchant_id IS NOT OLD.merchant_id
    OR NEW.purpose_id IS NOT OLD.purpose_id
    OR NEW.starts_on IS NOT OLD.starts_on
    OR NEW.expires_on IS NOT OLD.expires_on
    OR NEW.created_at IS NOT OLD.created_at
    OR (OLD.cancelled_at IS NOT NULL AND NEW.cancelled_at IS NOT OLD.cancelled_at)
BEGIN
    SELECT RAISE(ABORT, 'mandate terms are fixed');
END;

-- The newest row per field equals the agreement's current value (checked by K6).
CREATE TABLE mandate_events (
    event_id INTEGER PRIMARY KEY,
    mandate_ref TEXT NOT NULL REFERENCES mandates (mandate_ref),
    field TEXT NOT NULL CHECK (field IN ('status', 'cap_fen')),
    old TEXT,
    new TEXT NOT NULL,
    operation_id TEXT REFERENCES operations (operation_id),
    actor TEXT NOT NULL CHECK (actor IN ('customer', 'agent', 'coordinator', 'operator', 'system')),
    reason TEXT,
    at TEXT NOT NULL
);

CREATE INDEX mandate_events_by_field ON mandate_events (mandate_ref, field, event_id);

CREATE TRIGGER mandate_events_append_only BEFORE UPDATE ON mandate_events
BEGIN
    SELECT RAISE(ABORT, 'mandate_events is append-only');
END;

-- merchant_id has no foreign key: requests from unknown merchants are recorded too, without
-- a customer or an agreement. description_raw is untrusted merchant text, never parsed.
CREATE TABLE debit_requests (
    request_id TEXT PRIMARY KEY,
    merchant_id TEXT NOT NULL,
    merchant_request_id TEXT NOT NULL,
    customer_id TEXT REFERENCES customers (customer_id),
    mandate_ref TEXT REFERENCES mandates (mandate_ref),
    amount_fen INTEGER NOT NULL CHECK (amount_fen > 0),
    currency TEXT NOT NULL CHECK (currency = 'CNY'),
    period_key TEXT NOT NULL CHECK (period_key GLOB '[0-9][0-9][0-9][0-9]-[0-1][0-9]'),
    description_raw TEXT NOT NULL,
    received_at TEXT NOT NULL,
    respond_by TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN (
        'AWAITING_CLIENT', 'PAID', 'DECLINED', 'EXPIRED', 'AUTO_DECLINED', 'OUT_OF_MANDATE')),
    reason_code TEXT CHECK (reason_code IN (
        'NO_MANDATE', 'MANDATE_SUSPENDED', 'MANDATE_CANCELLED', 'MANDATE_EXPIRED', 'CAP',
        'PERIOD_USED', 'EXPIRED')),
    operation_id TEXT REFERENCES operations (operation_id),
    probe INTEGER NOT NULL DEFAULT 0 CHECK (probe IN (0, 1)),
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    CHECK ((status IN ('AUTO_DECLINED', 'OUT_OF_MANDATE', 'EXPIRED')) = (reason_code IS NOT NULL)),
    CHECK ((customer_id IS NULL) = (mandate_ref IS NULL))
);

CREATE UNIQUE INDEX debit_requests_merchant_request
    ON debit_requests (merchant_id, merchant_request_id);
-- At most one request per agreement and period awaits the customer, is paid or declined.
CREATE UNIQUE INDEX debit_requests_one_per_period
    ON debit_requests (mandate_ref, period_key)
    WHERE status IN ('AWAITING_CLIENT', 'PAID', 'DECLINED') AND probe = 0;
CREATE INDEX debit_requests_by_customer ON debit_requests (customer_id, received_at);
CREATE INDEX debit_requests_by_status ON debit_requests (status, respond_by);

CREATE TABLE effect_verification_events (
    event_id INTEGER PRIMARY KEY,
    operation_id TEXT NOT NULL REFERENCES operations (operation_id),
    check_kind TEXT NOT NULL CHECK (check_kind IN ('probe_cancel', 'probe_suspend', 'probe_cap')),
    result TEXT NOT NULL CHECK (result IN ('verified', 'pending', 'failed')),
    probe_request_id TEXT REFERENCES debit_requests (request_id),
    reason TEXT,
    at TEXT NOT NULL
);

CREATE INDEX effect_verification_events_by_operation
    ON effect_verification_events (operation_id, event_id);

CREATE TRIGGER effect_verification_events_append_only BEFORE UPDATE ON effect_verification_events
BEGIN
    SELECT RAISE(ABORT, 'effect_verification_events is append-only');
END;

-- The actions rebuild: same columns, order and constraints as schema 1, with the kind CHECK
-- widened. Children (challenges, operations, cards) keep naming `actions`: the old table is
-- dropped before the new one takes its name.
CREATE TABLE actions_new (
    logical_action_id TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK (revision >= 1),
    customer_id TEXT NOT NULL REFERENCES customers (customer_id),
    plan_id TEXT REFERENCES plans (plan_id),
    kind TEXT NOT NULL CHECK (kind IN (
        'TRANSFER', 'PAY_BILL', 'DESIGNATE_RESERVE', 'CONFIRM_BILL', 'RESOLVE_ITEM',
        'CANCEL_BILL', 'MARK_PAID_EXTERNAL', 'REDEEM', 'PURCHASE', 'CHECK_OUTCOME',
        'ADD_PAYEE', 'DEBIT_PAY', 'DEBIT_DECLINE', 'MANDATE_CANCEL', 'MANDATE_SUSPEND',
        'MANDATE_RESUME', 'MANDATE_CAP_LOWER', 'MANDATE_CAP_RAISE', 'MANDATE_CREATE')),
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

INSERT INTO actions_new (
    logical_action_id, revision, customer_id, plan_id, kind, status, params_json, request_hash,
    tier, depends_on, not_before, superseded_by, pause_reasons, provenance_json, version,
    created_at)
SELECT
    logical_action_id, revision, customer_id, plan_id, kind, status, params_json, request_hash,
    tier, depends_on, not_before, superseded_by, pause_reasons, provenance_json, version,
    created_at
FROM actions;

DROP TABLE actions;

ALTER TABLE actions_new RENAME TO actions;

-- One active (non-SUPERSEDED) revision per logical action.
CREATE UNIQUE INDEX actions_one_active_revision
    ON actions (logical_action_id) WHERE status <> 'SUPERSEDED';
CREATE INDEX actions_by_plan ON actions (plan_id);
CREATE INDEX actions_by_customer ON actions (customer_id, status);
