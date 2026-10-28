-- IM channel tables (SPEC §21).

CREATE TABLE im_bindings (
    binding_id INTEGER PRIMARY KEY,
    channel TEXT NOT NULL CHECK (channel IN ('feishu')),
    external_user_id TEXT NOT NULL,
    customer_id TEXT NOT NULL REFERENCES customers (customer_id),
    bound_at TEXT NOT NULL,
    revoked_at TEXT,
    created_at TEXT NOT NULL
);

CREATE UNIQUE INDEX im_bindings_one_active ON im_bindings (channel, external_user_id)
    WHERE revoked_at IS NULL;

CREATE TABLE im_pairing_codes (
    code_hmac TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL REFERENCES customers (customer_id),
    expires_at TEXT NOT NULL,
    consumed_at TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE im_messages_seen (
    channel TEXT NOT NULL CHECK (channel IN ('feishu')),
    message_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (channel, message_id)
);
