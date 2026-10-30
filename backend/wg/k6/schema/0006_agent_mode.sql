-- Agent mode of a login session (SPEC §26): `flat` runs the same agent without the plan
-- coordinator, for the evaluation's comparison. Only the demo role changes it.

ALTER TABLE sessions ADD COLUMN agent_mode TEXT NOT NULL DEFAULT 'coordinated'
    CHECK (agent_mode IN ('coordinated', 'flat'));
