-- Widen generosity_pct so a real FanDuel boost fits.
--
-- SB-1084. DECIMAL(8,4) allows four integer digits, capping a promotion at
-- 9999.9999%. A captured FanDuel ticket carries `PROFIT BOOST 10000%` -- a
-- customer special turning a -10000 base price into a displayed +101 -- which
-- overflows the column and fails the write outright:
--
--   Conversion Error: Casting value "10000" to type DECIMAL(8,4) failed:
--   value is out of range!
--
-- The old width was not a considered limit, just an assumption that a boost is
-- a modest percentage. Operator promotions on long-odds specials are not: the
-- boost is large precisely because the base price is tiny. Refusing to store
-- one would push the user back to recording the displayed boosted price, which
-- DATA_DICTIONARY 9.2 says is rounded and must not be computed from.
--
-- DECIMAL(10,4) allows six integer digits, to 999999.9999%. Scale is unchanged,
-- so no stored value is altered or reinterpreted: this widens range and nothing
-- else.
--
-- core.bet_promotion is rebuilt rather than altered in place. Its FK to
-- core.promotion makes DuckDB refuse to alter the parent --
--
--   Dependency Error: Cannot alter entry "promotion" because there are entries
--   that depend on it.
--
-- -- so the child is staged into an FK-free copy, dropped, the parent altered,
-- and the child recreated with the wider column and its constraints intact.
-- Same rebuild shape as 0006, for the same reason.

CREATE TABLE core.bet_promotion_staging AS SELECT * FROM core.bet_promotion;

DROP TABLE core.bet_promotion;

ALTER TABLE core.promotion ALTER COLUMN generosity_pct TYPE DECIMAL(10,4);

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
    generosity_pct DECIMAL(10,4),

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

INSERT INTO core.bet_promotion (
    tenant_id, user_id, id, bet_id, bet_leg_id, promotion_id,
    promotion_type, scope, label, apply_order, generosity_pct,
    triggered, value_delivered, created_at
)
SELECT
    tenant_id, user_id, id, bet_id, bet_leg_id, promotion_id,
    promotion_type, scope, label, apply_order, generosity_pct,
    triggered, value_delivered, created_at
FROM core.bet_promotion_staging;

DROP TABLE core.bet_promotion_staging;
