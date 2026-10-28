-- Approval cards (SPEC §9.2): what the trusted UI showed, with the terms and their hash.
-- A card is immutable except approval_id, which is set once when the customer confirms it.

CREATE TABLE cards (
    card_id TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL REFERENCES customers (customer_id),
    kind TEXT NOT NULL CHECK (kind IN ('action', 'plan', 'plan_revision')),
    plan_id TEXT REFERENCES plans (plan_id),
    logical_action_id TEXT,
    revision INTEGER,
    parent_approval_id TEXT REFERENCES approvals (approval_id),
    tier TEXT NOT NULL CHECK (tier IN ('GREEN', 'YELLOW', 'RED')),
    terms_json TEXT NOT NULL,
    terms_hash TEXT NOT NULL,
    content_json TEXT NOT NULL,
    cash_snapshot_id TEXT,
    calc_result_id TEXT,
    expires_at TEXT NOT NULL,
    approval_id TEXT REFERENCES approvals (approval_id),
    created_at TEXT NOT NULL,
    FOREIGN KEY (logical_action_id, revision) REFERENCES actions (logical_action_id, revision),
    CHECK ((logical_action_id IS NULL) = (revision IS NULL)),
    CHECK (kind <> 'action' OR logical_action_id IS NOT NULL),
    CHECK (kind = 'action' OR plan_id IS NOT NULL),
    CHECK ((kind = 'plan_revision') = (parent_approval_id IS NOT NULL))
);

CREATE INDEX cards_by_action ON cards (logical_action_id, revision);

CREATE TRIGGER cards_immutable BEFORE UPDATE ON cards
WHEN OLD.approval_id IS NOT NULL
    OR NEW.card_id IS NOT OLD.card_id
    OR NEW.customer_id IS NOT OLD.customer_id
    OR NEW.kind IS NOT OLD.kind
    OR NEW.plan_id IS NOT OLD.plan_id
    OR NEW.logical_action_id IS NOT OLD.logical_action_id
    OR NEW.revision IS NOT OLD.revision
    OR NEW.parent_approval_id IS NOT OLD.parent_approval_id
    OR NEW.tier IS NOT OLD.tier
    OR NEW.terms_json IS NOT OLD.terms_json
    OR NEW.terms_hash IS NOT OLD.terms_hash
    OR NEW.content_json IS NOT OLD.content_json
    OR NEW.cash_snapshot_id IS NOT OLD.cash_snapshot_id
    OR NEW.calc_result_id IS NOT OLD.calc_result_id
    OR NEW.expires_at IS NOT OLD.expires_at
    OR NEW.created_at IS NOT OLD.created_at
BEGIN
    SELECT RAISE(ABORT, 'cards are immutable except approval_id');
END;

-- At most one challenge per action revision is open (neither verified nor invalidated).
CREATE UNIQUE INDEX challenges_one_open
    ON challenges (logical_action_id, revision)
    WHERE verified_at IS NULL AND invalidated_at IS NULL;

-- At most one grant (verified, not consumed, not invalidated) per action revision.
CREATE UNIQUE INDEX challenges_one_grant
    ON challenges (logical_action_id, revision)
    WHERE verified_at IS NOT NULL AND consumed_at IS NULL AND invalidated_at IS NULL;

-- A consumed grant stays consumed: consumed_at and verified_at are set once.
CREATE TRIGGER challenges_set_once BEFORE UPDATE ON challenges
WHEN (OLD.consumed_at IS NOT NULL AND NEW.consumed_at IS NOT OLD.consumed_at)
    OR (OLD.verified_at IS NOT NULL AND NEW.verified_at IS NOT OLD.verified_at)
    OR (OLD.invalidated_at IS NOT NULL AND NEW.invalidated_at IS NOT OLD.invalidated_at)
    OR NEW.scope_hash IS NOT OLD.scope_hash
    OR NEW.code_hmac IS NOT OLD.code_hmac
    OR NEW.approval_id IS NOT OLD.approval_id
BEGIN
    SELECT RAISE(ABORT, 'challenge fields are set once');
END;
