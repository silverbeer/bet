-- Drops the foreign keys that reference core.bet (from bet_leg, bet_leg_group,
-- bet_promotion) and the one from bet_promotion to bet_leg.
--
-- DuckDB 1.5.5 enforces FK constraints by checking, on any UPDATE or DELETE of
-- a referenced row, whether another table's FK still points at it -- and that
-- check is documented as statement-level rather than transaction-level. In
-- practice this means a bare `UPDATE core.bet SET status = 'settled' ...`
-- fails with "Violates foreign key constraint... is still referenced by a
-- foreign key in a different table" the moment the bet has even one leg,
-- which is every real bet. It reproduces the same way inside a single
-- transaction that deletes the referencing leg first, updates/recreates the
-- parent, then reinserts the leg -- so there is no query-shape workaround
-- available at the SQL layer for `bet settle` (SB-812).
--
-- The fix is to stop asking the FK to guarantee what the repository layer
-- already guarantees by construction: ScopedRepository only ever writes
-- within one OwnerScope (docs/OWNERSHIP.md section 6), and every write to
-- these tables goes through core.bet_leg/core.bet_promotion's own repository,
-- which always carries a real bet_id from a row it just read or is writing in
-- the same transaction. This is the same app-layer-enforcement trade ADR 0001
-- already made for provenance over triggers.
--
-- DuckDB has no ALTER TABLE DROP CONSTRAINT (verified: "No support for that
-- ALTER TABLE option yet!"), so removing a named FK means recreating the
-- table. bet_leg_group and bet_promotion have no writer anywhere in the
-- codebase yet, so they are dropped and recreated empty. bet_leg holds real
-- data from `bet add` (SB-811), so it is recreated via copy-rename, and its
-- own outgoing FK to bet_leg_group is dropped in the same pass: bet_leg_group
-- must be dropped and recreated to lose its own FK to bet, which is
-- impossible while bet_leg's FK still points at it, so recreating bet_leg
-- first without that FK is what makes the ordering possible at all.
--
-- The unique/primary-key constraints, and every CHECK, are unchanged.


-- ------------------------------------------------------- bet_promotion first
-- No writer exists yet (SB-775/776). Referencing core.bet and core.bet_leg,
-- so it must go before either of those can be touched.

DROP TABLE core.bet_promotion;


-- --------------------------------------------------------------- bet_leg

CREATE TABLE core.bet_leg_new (
    tenant_id        UUID          NOT NULL,
    user_id          UUID          NOT NULL,
    id               UUID          NOT NULL DEFAULT uuidv7(),
    bet_id           UUID          NOT NULL,
    leg_order        INTEGER       NOT NULL,
    group_id         UUID,

    sport            VARCHAR,
    league           VARCHAR,
    event_ref        VARCHAR,
    event_label      VARCHAR,
    event_starts_at  TIMESTAMPTZ,

    market_family    VARCHAR,
    market_name      VARCHAR,
    selection_name   VARCHAR,
    side             VARCHAR,
    line_value       DECIMAL(12,4),

    target_team      VARCHAR,
    target_player    VARCHAR,
    is_home          BOOLEAN,

    odds_american    INTEGER,
    odds_decimal     DECIMAL(12,4),

    result           VARCHAR,
    result_value     DECIMAL(12,4),

    created_at       TIMESTAMPTZ   NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ   NOT NULL DEFAULT now(),

    PRIMARY KEY (tenant_id, user_id, id),
    UNIQUE (tenant_id, user_id, bet_id, leg_order),
    CONSTRAINT bet_leg_result_known
        CHECK (result IN ('won', 'lost', 'push', 'void', 'partial', 'cashed_out')),
    CONSTRAINT bet_leg_order_is_positive CHECK (leg_order >= 1)
);

INSERT INTO core.bet_leg_new
SELECT
    tenant_id, user_id, id, bet_id, leg_order, group_id,
    sport, league, event_ref, event_label, event_starts_at,
    market_family, market_name, selection_name, side, line_value,
    target_team, target_player, is_home,
    odds_american, odds_decimal,
    result, result_value,
    created_at, updated_at
FROM core.bet_leg;

DROP TABLE core.bet_leg;
ALTER TABLE core.bet_leg_new RENAME TO bet_leg;


-- ----------------------------------------------------------- bet_leg_group
-- Safe to drop and recreate empty now: bet_leg no longer holds an FK to it.

DROP TABLE core.bet_leg_group;

CREATE TABLE core.bet_leg_group (
    tenant_id      UUID          NOT NULL,
    user_id        UUID          NOT NULL,
    id             UUID          NOT NULL DEFAULT uuidv7(),
    bet_id         UUID          NOT NULL,

    external_ref   VARCHAR,
    category       VARCHAR,

    odds_american  INTEGER,
    odds_decimal   DECIMAL(12,4),

    created_at     TIMESTAMPTZ   NOT NULL DEFAULT now(),

    PRIMARY KEY (tenant_id, user_id, id)
);


-- ------------------------------------------------------------- bet_promotion
-- Recreated empty. Its FK to core.promotion is kept: nothing in this
-- migration writes or updates core.promotion, so it does not hit the same
-- limitation, and it is real integrity worth keeping while it's free.

CREATE TABLE core.bet_promotion (
    tenant_id      UUID          NOT NULL,
    user_id        UUID          NOT NULL,
    id             UUID          NOT NULL DEFAULT uuidv7(),
    bet_id         UUID          NOT NULL,
    bet_leg_id     UUID,
    promotion_id   UUID,

    promotion_type VARCHAR       NOT NULL,
    scope          VARCHAR       NOT NULL DEFAULT 'ticket',
    label          VARCHAR,

    apply_order    INTEGER       NOT NULL DEFAULT 1,
    generosity_pct DECIMAL(8,4),

    triggered      BOOLEAN,
    value_delivered DECIMAL(12,2),

    created_at     TIMESTAMPTZ   NOT NULL DEFAULT now(),

    PRIMARY KEY (tenant_id, user_id, id),
    UNIQUE (tenant_id, user_id, bet_id, promotion_type, apply_order),
    FOREIGN KEY (tenant_id, user_id, promotion_id)
        REFERENCES core.promotion (tenant_id, user_id, id),
    CONSTRAINT bet_promotion_type_known
        CHECK (promotion_type IN ('profit_boost', 'bonus_bet', 'insurance',
                                  'rewards_currency', 'other')),
    CONSTRAINT bet_promotion_scope_matches_leg
        CHECK ((scope = 'leg') = (bet_leg_id IS NOT NULL)),
    CONSTRAINT bet_promotion_scope_known
        CHECK (scope IN ('ticket', 'leg')),
    CONSTRAINT bet_promotion_apply_order_is_positive CHECK (apply_order >= 1)
);
