-- Where a bet came from, when the answer is a file rather than an import run.
--
-- SB-1049. The daily capture loop is a photograph of a bet slip, read by an
-- agent, written through `bet add --capture-method screenshot --source-file`.
-- That is a materially less trustworthy input than a sportsbook export, and
-- SB-759's risk section is explicit that it must never be indistinguishable
-- from one. `capture_method` already records *how* a bet was captured; this
-- table records *what it was captured from*, so a doubted figure can be
-- checked against the original image rather than argued about.
--
-- control.source_file is specified as an owned table in OWNERSHIP.md section
-- 2.1 and is ultimately SB-703's, alongside control.import_run and
-- raw.source_record. Built here in the minimal shape one image needs, because
-- the alternative was to archive files with no row pointing at them. SB-703
-- extends it; it does not have to invent it.

CREATE TABLE control.source_file (
    tenant_id     UUID          NOT NULL,
    user_id       UUID          NOT NULL,
    id            UUID          NOT NULL DEFAULT uuidv7(),

    -- Relative to Settings.source_archive_dir, never absolute: the archive is
    -- moved with the warehouse when data_dir changes, and an absolute path
    -- stored here would point at the old location forever.
    archived_path VARCHAR       NOT NULL,

    -- What the user called it. Kept because a phone screenshot's filename
    -- carries its capture timestamp, which is often the only record of when
    -- the slip was actually photographed.
    original_name VARCHAR       NOT NULL,

    -- Content address. Re-archiving the same image is idempotent rather than
    -- a second copy, and a hash mismatch later means the archive was edited.
    sha256        VARCHAR       NOT NULL,
    byte_size     BIGINT        NOT NULL,
    media_type    VARCHAR,

    -- How the file reached BET. Same vocabulary as core.bet.capture_method,
    -- because a source file and the bets drawn from it are captured the same
    -- way, and two drifting lists would be worse than one duplicated one.
    capture_method VARCHAR      NOT NULL DEFAULT 'screenshot',

    notes         VARCHAR,
    created_at    TIMESTAMPTZ   NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ   NOT NULL DEFAULT now(),

    PRIMARY KEY (tenant_id, user_id, id),

    -- No FOREIGN KEY to core.user. DuckDB does not support foreign keys across
    -- schemas (SB-703), so control tables cannot reference core ones at all.
    -- Ownership is enforced by ScopedRepository, which has no unscoped path.

    CONSTRAINT source_file_is_unique_per_user
        UNIQUE (tenant_id, user_id, sha256),
    CONSTRAINT source_file_capture_method_known
        CHECK (capture_method IN ('export', 'api', 'statement', 'manual', 'pdf', 'screenshot')),
    CONSTRAINT source_file_size_is_positive
        CHECK (byte_size > 0)
);


-- ------------------------------------------------------- the pointer on a bet

-- Nullable, and no foreign key, for the same cross-schema reason as
-- import_run_id and source_record_id above it. Most bets have no source file:
-- a bet typed in at the counter was never captured from anything.
ALTER TABLE core.bet ADD COLUMN source_file_id UUID;
