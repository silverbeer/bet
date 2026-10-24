-- Credentials and refresh tokens for the API (ADR 0002 section 4).
--
-- SB-1397. Two new owned tables. Nothing existing is altered: the user who
-- `bet init` created, and every bet they own, are untouched. A user with no
-- credential row simply cannot log in yet; `bet user set-password` adds one.
--
-- Both tables are owned by OWNERSHIP.md's test -- delete the user and their
-- password and sessions go with them -- so both carry tenant_id and user_id,
-- NOT NULL, and reach core.user through the full composite key.
--
-- Secrets are never stored in a form that can be replayed. password_hash is an
-- argon2id PHC string, and refresh_token keeps only a SHA-256 digest of the
-- token it issued: a copy of this database is not a copy of anyone's session.


-- ------------------------------------------------------------ user credential

-- One password per user. rotated_at is NULL until the password is first
-- changed; created_at records when the user first got one.
CREATE TABLE core.user_credential (
    tenant_id     UUID        NOT NULL,
    user_id       UUID        NOT NULL,
    password_hash VARCHAR     NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    rotated_at    TIMESTAMPTZ,
    PRIMARY KEY (tenant_id, user_id),
    FOREIGN KEY (tenant_id, user_id) REFERENCES core.user (tenant_id, id),
    CONSTRAINT user_credential_hash_is_argon2id
        CHECK (starts_with(password_hash, '$argon2id$'))
);


-- -------------------------------------------------------------- refresh token

-- One row per refresh token issued. id is the token's jti claim.
--
-- Tokens rotate: a refresh revokes the presented token and names its successor
-- in replaced_by, so each login is a chain. Presenting a token that is already
-- revoked means a copy is in someone else's hands, and the chain from that
-- token onward is revoked. replaced_by carries no foreign key: DuckDB refuses
-- an UPDATE of a row another row's foreign key references (see 0006), and
-- revoking is an UPDATE.
CREATE TABLE core.refresh_token (
    tenant_id   UUID        NOT NULL,
    user_id     UUID        NOT NULL,
    id          UUID        NOT NULL,
    -- Hex SHA-256 of the encoded token. The token itself is never stored.
    token_hash  VARCHAR     NOT NULL,
    issued_at   TIMESTAMPTZ NOT NULL,
    expires_at  TIMESTAMPTZ NOT NULL,
    revoked_at  TIMESTAMPTZ,
    replaced_by UUID,
    PRIMARY KEY (tenant_id, user_id, id),
    FOREIGN KEY (tenant_id, user_id) REFERENCES core.user (tenant_id, id),
    CONSTRAINT refresh_token_expires_after_issue CHECK (expires_at > issued_at),
    CONSTRAINT refresh_token_hash_is_sha256 CHECK (length(token_hash) = 64),
    CONSTRAINT refresh_token_replaced_only_when_revoked
        CHECK (replaced_by IS NULL OR revoked_at IS NOT NULL)
);
