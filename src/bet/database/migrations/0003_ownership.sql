-- Ownership chain: Tenant -> User -> SportsbookAccount.
--
-- Implements docs/OWNERSHIP.md (SB-687). BET is a single-user product with a
-- multi-user schema: these columns exist from the first migration because they
-- are cheap now and expensive to retrofit, while consent, sharing, following
-- and cohorts are deliberately absent until there is a real second user.
--
-- Identifiers are uuidv7 — globally unique, so two BET databases can be merged,
-- and time-ordered, so primary keys keep insertion locality without a sequence.
-- Reference rows use a stable natural key instead, because 'fanduel' is the
-- same value in every deployment and that is what makes a merge tractable.


-- The isolation boundary. Locally there is exactly one row.
CREATE TABLE core.tenant (
    id          UUID        NOT NULL PRIMARY KEY DEFAULT uuidv7(),
    name        VARCHAR     NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);


-- A person. Tenant-scoped but not user-scoped: its own id *is* the user id.
--
-- timezone is the display timezone. Every stored timestamp is UTC
-- (DATA_DICTIONARY.md); this is what a bet placed at 22:00 EDT is rendered as.
CREATE TABLE core.user (
    tenant_id    UUID        NOT NULL,
    id           UUID        NOT NULL DEFAULT uuidv7(),
    display_name VARCHAR     NOT NULL,
    locale       VARCHAR     NOT NULL DEFAULT 'en-US',
    timezone     VARCHAR     NOT NULL DEFAULT 'UTC',
    status       VARCHAR     NOT NULL DEFAULT 'active',
    preferences  JSON        NOT NULL DEFAULT '{}',
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, id),
    FOREIGN KEY (tenant_id) REFERENCES core.tenant (id),
    CONSTRAINT user_status_known CHECK (status IN ('active', 'disabled'))
);


-- Operator identity. Reference data: FanDuel is FanDuel for everyone, so this
-- carries no ownership columns and uses a natural key.
--
-- The export capability columns are deliberately nullable, and NULL means "not
-- yet established" rather than "no". Which formats each operator actually
-- publishes is being determined by SB-689 against real specimens; the versioned
-- importer profile (SB-710) is what will ultimately declare supported formats.
-- Recording a guess here would look like a fact.
CREATE TABLE core.sportsbook (
    code         VARCHAR     NOT NULL PRIMARY KEY,
    name         VARCHAR     NOT NULL,
    is_supported BOOLEAN     NOT NULL DEFAULT TRUE,
    exports_csv  BOOLEAN,
    exports_xlsx BOOLEAN,
    exports_pdf  BOOLEAN,
    has_api      BOOLEAN,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO core.sportsbook (code, name) VALUES
    ('fanduel',      'FanDuel'),
    ('draftkings',   'DraftKings'),
    ('betmgm',       'BetMGM'),
    ('fanatics',     'Fanatics'),
    ('caesars',      'Caesars'),
    ('bally_bet',    'Bally Bet'),
    ('thescore_bet', 'theScore Bet');


-- One operator account belonging to one user. The first owned table.
--
-- The composite foreign key is the point: an account cannot claim a user that
-- does not exist within its own tenant, so a cross-tenant row is refused by the
-- database rather than by a convention someone has to remember.
--
-- external_account_ref is whatever the operator calls the account. It is
-- nullable because several operators never expose one, and it is not a natural
-- key: two users may hold accounts at the same book.
CREATE TABLE core.sportsbook_account (
    tenant_id            UUID        NOT NULL,
    user_id              UUID        NOT NULL,
    id                   UUID        NOT NULL DEFAULT uuidv7(),
    sportsbook_code      VARCHAR     NOT NULL,
    label                VARCHAR     NOT NULL,
    external_account_ref VARCHAR,
    is_active            BOOLEAN     NOT NULL DEFAULT TRUE,
    opened_at            TIMESTAMPTZ,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, user_id, id),
    FOREIGN KEY (tenant_id, user_id) REFERENCES core.user (tenant_id, id),
    FOREIGN KEY (sportsbook_code) REFERENCES core.sportsbook (code)
);
