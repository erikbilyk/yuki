-- S2 tables: statements, commitments, reserves, allowances and cash snapshots (SPEC §7).

CREATE TABLE import_batches (
    batch_id INTEGER PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES accounts (account_id),
    statement_id TEXT NOT NULL,
    period TEXT NOT NULL,
    opening_fen INTEGER NOT NULL,
    closing_fen INTEGER NOT NULL,
    new_count INTEGER NOT NULL CHECK (new_count >= 0),
    duplicate_count INTEGER NOT NULL CHECK (duplicate_count >= 0),
    created_at TEXT NOT NULL
);

CREATE INDEX import_batches_by_statement ON import_batches (statement_id);

CREATE TABLE categories (
    category_id TEXT PRIMARY KEY,
    label TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- Imported statement lines are history, not ledger postings. Fields ending in _raw and
-- category_hint come from the statement and are untrusted text.
CREATE TABLE transactions (
    txn_row_id INTEGER PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES accounts (account_id),
    source_txn_id TEXT NOT NULL,
    batch_id INTEGER NOT NULL REFERENCES import_batches (batch_id),
    posted_on TEXT NOT NULL,
    amount_fen INTEGER NOT NULL,
    counterparty_raw TEXT NOT NULL,
    description_raw TEXT NOT NULL,
    category_hint TEXT,
    category_id TEXT REFERENCES categories (category_id),
    resolution TEXT,
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    UNIQUE (account_id, source_txn_id)
);

CREATE TABLE commitments (
    commitment_id TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL REFERENCES customers (customer_id),
    purpose_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    payee_id TEXT NOT NULL REFERENCES payees (payee_id),
    amount_fen INTEGER NOT NULL CHECK (amount_fen > 0),
    unpaid_fen INTEGER NOT NULL CHECK (unpaid_fen >= 0 AND unpaid_fen <= amount_fen),
    due_date TEXT NOT NULL,
    recurrence TEXT NOT NULL CHECK (recurrence IN ('monthly', 'none')),
    source TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN (
        'CANDIDATE', 'CONFIRMED', 'PARTIALLY_PAID', 'PAID', 'PAID_REPORTED', 'CANCELLED',
        'DISMISSED')),
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

CREATE INDEX commitments_by_customer ON commitments (customer_id, status);

CREATE TABLE commitment_events (
    commitment_event_id INTEGER PRIMARY KEY,
    commitment_id TEXT NOT NULL REFERENCES commitments (commitment_id),
    change TEXT NOT NULL,
    delta_fen INTEGER NOT NULL,
    unpaid_fen INTEGER NOT NULL CHECK (unpaid_fen >= 0),
    commitment_version INTEGER NOT NULL,
    source_ref TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE reserves (
    customer_id TEXT NOT NULL REFERENCES customers (customer_id),
    kind TEXT NOT NULL,
    target_fen INTEGER NOT NULL CHECK (target_fen >= 0),
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    PRIMARY KEY (customer_id, kind)
);

CREATE TABLE allowances (
    customer_id TEXT NOT NULL REFERENCES customers (customer_id),
    period TEXT NOT NULL,
    budget_fen INTEGER NOT NULL CHECK (budget_fen >= 0),
    excluded_purpose_ids TEXT NOT NULL DEFAULT '[]',
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    PRIMARY KEY (customer_id, period)
);

CREATE TABLE cash_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL REFERENCES customers (customer_id),
    as_of TEXT NOT NULL,
    inputs_hash TEXT NOT NULL,
    rules_version TEXT NOT NULL,
    c_fen INTEGER NOT NULL,
    e_fen INTEGER NOT NULL,
    l_fen INTEGER NOT NULL,
    b_fen INTEGER NOT NULL,
    m_fen INTEGER NOT NULL,
    u_fen INTEGER NOT NULL,
    protected_fen INTEGER NOT NULL,
    headroom_fen INTEGER NOT NULL CHECK (headroom_fen >= 0),
    shortfall_fen INTEGER NOT NULL CHECK (shortfall_fen >= 0),
    bills_json TEXT NOT NULL,
    unresolved_json TEXT NOT NULL,
    monthly_json TEXT NOT NULL,
    quality_json TEXT NOT NULL,
    commitments_version INTEGER NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX cash_snapshots_by_customer ON cash_snapshots (customer_id, created_at);

CREATE TRIGGER commitment_events_append_only BEFORE UPDATE ON commitment_events
BEGIN
    SELECT RAISE(ABORT, 'commitment_events is append-only');
END;

CREATE TRIGGER cash_snapshots_immutable BEFORE UPDATE ON cash_snapshots
BEGIN
    SELECT RAISE(ABORT, 'cash_snapshots are immutable');
END;
