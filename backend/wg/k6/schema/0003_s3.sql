-- S3 tables: products, positions, lots, holdings, calc results, revisions, checkpoints.

CREATE TABLE fee_schedules (
    fee_schedule_id TEXT PRIMARY KEY,
    terms_version TEXT NOT NULL,
    purchase_rate TEXT NOT NULL,
    tiers_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE products (
    product_id TEXT PRIMARY KEY
        CHECK (length(product_id) = 6 AND product_id NOT GLOB '*[^0-9]*'),
    name TEXT NOT NULL,
    share_class TEXT NOT NULL,
    nav TEXT NOT NULL,
    nav_date TEXT NOT NULL,
    fee_schedule_id TEXT NOT NULL REFERENCES fee_schedules (fee_schedule_id),
    min_purchase_fen INTEGER NOT NULL CHECK (min_purchase_fen > 0),
    redeem_settle_days INTEGER NOT NULL DEFAULT 3 CHECK (redeem_settle_days >= 0),
    purchase_settle_days INTEGER NOT NULL DEFAULT 0 CHECK (purchase_settle_days >= 0),
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

CREATE TABLE positions (
    account_id TEXT NOT NULL REFERENCES accounts (account_id),
    product_id TEXT NOT NULL REFERENCES products (product_id),
    shares TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    PRIMARY KEY (account_id, product_id)
);

CREATE TABLE lots (
    lot_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    product_id TEXT NOT NULL,
    shares TEXT NOT NULL,
    acquired_on TEXT NOT NULL,
    cost_fen INTEGER NOT NULL CHECK (cost_fen >= 0),
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    FOREIGN KEY (account_id, product_id) REFERENCES positions (account_id, product_id)
);

-- One row per holding; the snapshot fields repeat on each row of the same snapshot.
CREATE TABLE holdings_snapshots (
    holding_row_id INTEGER PRIMARY KEY,
    product_id TEXT NOT NULL REFERENCES products (product_id),
    as_of TEXT NOT NULL,
    source_id TEXT NOT NULL,
    executable INTEGER NOT NULL CHECK (executable IN (0, 1)),
    taxonomy_version TEXT NOT NULL,
    unresolved_weight TEXT NOT NULL,
    security_code TEXT NOT NULL,
    issuer TEXT NOT NULL,
    sector_code TEXT NOT NULL,
    weight TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (product_id, source_id, as_of, security_code)
);

CREATE TABLE calc_results (
    calc_result_id TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL REFERENCES customers (customer_id),
    kind TEXT NOT NULL,
    inputs_hash TEXT NOT NULL,
    basis_json TEXT NOT NULL,
    output_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE plan_revisions (
    plan_id TEXT NOT NULL REFERENCES plans (plan_id),
    revision INTEGER NOT NULL CHECK (revision >= 1),
    reason TEXT NOT NULL CHECK (reason IN ('initial', 'bill_change', 'new_approval')),
    trigger_event_id TEXT,
    cash_snapshot_id TEXT REFERENCES cash_snapshots (snapshot_id),
    calc_result_id TEXT REFERENCES calc_results (calc_result_id),
    approval_id TEXT REFERENCES approvals (approval_id),
    within_bounds INTEGER NOT NULL CHECK (within_bounds IN (0, 1)),
    created_at TEXT NOT NULL,
    PRIMARY KEY (plan_id, revision)
);

CREATE TABLE checkpoint_results (
    plan_id TEXT NOT NULL REFERENCES plans (plan_id),
    logical_action_id TEXT NOT NULL,
    result TEXT NOT NULL CHECK (result IN ('passed', 'failed', 'checkpoint_missed')),
    measured_pct TEXT NOT NULL,
    lower_pct TEXT NOT NULL,
    upper_pct TEXT NOT NULL,
    basis_json TEXT NOT NULL,
    evaluated_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (plan_id, logical_action_id)
);

CREATE TRIGGER calc_results_immutable BEFORE UPDATE ON calc_results
BEGIN
    SELECT RAISE(ABORT, 'calc_results are immutable');
END;

CREATE TRIGGER plan_revisions_append_only BEFORE UPDATE ON plan_revisions
BEGIN
    SELECT RAISE(ABORT, 'plan_revisions is append-only');
END;

CREATE TRIGGER checkpoint_results_written_once BEFORE UPDATE ON checkpoint_results
BEGIN
    SELECT RAISE(ABORT, 'checkpoint_results are written once');
END;
