-- The canonical bet model.
--
-- Bet is the ticket aggregate; BetLeg is the atomic selection. A flat table
-- would distort every parlay, so the split is not negotiable.
--
-- Money follows docs/DATA_DICTIONARY.md exactly. DECIMAL throughout, never
-- DOUBLE. net_profit and total_risk are GENERATED columns: DuckDB computes them
-- and refuses inserts into them, so the canonical formulas cannot drift from the
-- dictionary no matter what any importer does.
--
-- Two structural findings from real specimens drive the shape here:
--
--   * Per-leg odds and per-leg result values are OPERATOR-DEPENDENT, not merely
--     optional. Fanatics publishes leg odds, the FanDuel API publishes them for
--     SGPs, DraftKings publishes none. FanDuel renders an achieved quantity per
--     leg, DraftKings renders only a tick. Both are therefore nullable, and any
--     analytic built on them covers part of the book — views must disclose that.
--
-- DuckDB does not support foreign keys across schemas, so membership of the
-- reference vocabularies is enforced with CHECK constraints listing the values
-- rather than by FK. The reference tables remain authoritative for semantics and
-- are still joined by analytics; a test asserts each CHECK list matches its
-- table exactly, so the duplication fails loudly rather than drifting.
--
--   * Same-game groups are an ORTHOGONAL layer, not a third level of hierarchy.
--     The FanDuel API defines groups separately from legs and gives each group
--     its own price, which a leg column could not hold without duplicating it
--     across every member. core.bet_leg_group carries it once.


-- ---------------------------------------------------------------- the ticket

CREATE TABLE core.bet (
    tenant_id             UUID          NOT NULL,
    user_id               UUID          NOT NULL,
    id                    UUID          NOT NULL DEFAULT uuidv7(),
    sportsbook_account_id UUID          NOT NULL,

    -- Operators supply two identifiers and both are needed: FanDuel publishes a
    -- betId and a betReceiptId together, in two different generations.
    external_bet_id       VARCHAR,
    external_receipt_id   VARCHAR,

    -- Provenance. These carry no foreign keys yet because control.import_run
    -- and raw.source_record do not exist until SB-703, and DuckDB has no
    -- ALTER TABLE ADD FOREIGN KEY — adding them later requires rebuilding this
    -- table, which SB-703 must do deliberately rather than discover.
    import_run_id         UUID,
    source_record_id      UUID,
    profile_version       VARCHAR,
    capture_method        VARCHAR       NOT NULL DEFAULT 'manual',

    placed_at             TIMESTAMPTZ   NOT NULL,
    accepted_at           TIMESTAMPTZ,
    settled_at            TIMESTAMPTZ,

    status                VARCHAR       NOT NULL DEFAULT 'pending',
    result                VARCHAR,
    wager_kind            VARCHAR       NOT NULL DEFAULT 'straight',
    settlement_source     VARCHAR,

    -- Money. Bonus stake is never folded into cash stake (DATA_DICTIONARY 5.1).
    currency              VARCHAR(3)    NOT NULL DEFAULT 'USD',
    cash_staked           DECIMAL(12,2) NOT NULL DEFAULT 0,
    bonus_staked          DECIMAL(12,2) NOT NULL DEFAULT 0,
    total_risk            DECIMAL(12,2) GENERATED ALWAYS AS (cash_staked + bonus_staked),
    cash_returned         DECIMAL(12,2) NOT NULL DEFAULT 0,
    cashout_amount        DECIMAL(12,2),
    net_profit            DECIMAL(12,2) GENERATED ALWAYS AS (cash_returned - cash_staked),

    -- Value earned FROM the bet, not applied to it. Accrues on losers too, so it
    -- belongs to economic_roi only. Four decimal places because per-bet accruals
    -- are sub-cent and would round to zero (SB-776).
    rewards_earned        DECIMAL(12,4) NOT NULL DEFAULT 0,

    -- A voided leg reprices the ticket, so the price it was struck at and the
    -- price it settled at are different facts. Odds-band analytics use placed.
    odds_american_placed  INTEGER,
    odds_decimal_placed   DECIMAL(12,4),
    odds_american_settled INTEGER,
    odds_decimal_settled  DECIMAL(12,4),

    -- Corrections create a new version; they never overwrite. is_current is what
    -- every analytic filters on.
    version               INTEGER       NOT NULL DEFAULT 1,
    is_current            BOOLEAN       NOT NULL DEFAULT TRUE,
    supersedes_id         UUID,

    notes                 VARCHAR,
    created_at            TIMESTAMPTZ   NOT NULL DEFAULT now(),
    updated_at            TIMESTAMPTZ   NOT NULL DEFAULT now(),

    PRIMARY KEY (tenant_id, user_id, id),
    FOREIGN KEY (tenant_id, user_id, sportsbook_account_id)
        REFERENCES core.sportsbook_account (tenant_id, user_id, id),
    CONSTRAINT bet_status_known
        CHECK (status IN ('pending', 'settled')),
    CONSTRAINT bet_result_known
        CHECK (result IN ('won', 'lost', 'push', 'void', 'partial', 'cashed_out')),
    CONSTRAINT bet_wager_kind_known
        CHECK (wager_kind IN ('straight', 'parlay', 'same_game_parlay', 'teaser',
                              'round_robin', 'future', 'system', 'unknown')),
    CONSTRAINT bet_settlement_source_known
        CHECK (settlement_source IN ('amount', 'status_word', 'both_agree')),
    CONSTRAINT bet_capture_method_known
        CHECK (capture_method IN ('export', 'api', 'statement', 'manual', 'pdf', 'screenshot')),

    -- A settled bet has a result and a pending one does not. Without this a
    -- half-settled row would silently drop out of every result-based analytic.
    CONSTRAINT bet_result_matches_status
        CHECK ((status = 'settled') = (result IS NOT NULL)),
    CONSTRAINT bet_settled_at_matches_status
        CHECK ((status = 'settled') OR (settled_at IS NULL)),
    CONSTRAINT bet_money_is_not_negative
        CHECK (cash_staked >= 0 AND bonus_staked >= 0 AND cash_returned >= 0),
    CONSTRAINT bet_something_was_risked
        CHECK (cash_staked > 0 OR bonus_staked > 0),
    CONSTRAINT bet_version_is_positive CHECK (version >= 1)
);


-- ------------------------------------------------- same-game groups (orthogonal)

CREATE TABLE core.bet_leg_group (
    tenant_id      UUID          NOT NULL,
    user_id        UUID          NOT NULL,
    id             UUID          NOT NULL DEFAULT uuidv7(),
    bet_id         UUID          NOT NULL,

    -- The operator's own reference for the group, e.g. FanDuel's groupRef.
    external_ref   VARCHAR,
    category       VARCHAR,

    -- The group's own price. Comparing it with the product of its legs' prices
    -- makes the SGP correlation adjustment measurable, which is the only reason
    -- to model groups separately at all.
    odds_american  INTEGER,
    odds_decimal   DECIMAL(12,4),

    created_at     TIMESTAMPTZ   NOT NULL DEFAULT now(),

    PRIMARY KEY (tenant_id, user_id, id),
    FOREIGN KEY (tenant_id, user_id, bet_id) REFERENCES core.bet (tenant_id, user_id, id)
);


-- ------------------------------------------------------------------- the legs

CREATE TABLE core.bet_leg (
    tenant_id        UUID          NOT NULL,
    user_id          UUID          NOT NULL,
    id               UUID          NOT NULL DEFAULT uuidv7(),
    bet_id           UUID          NOT NULL,
    leg_order        INTEGER       NOT NULL,
    group_id         UUID,

    -- Retained as source text until sports identity resolution exists (SB-747).
    -- An event timestamp is deliberately not part of any key: the same event
    -- carries different times on different legs of one real ticket.
    sport            VARCHAR,
    league           VARCHAR,
    event_ref        VARCHAR,
    event_label      VARCHAR,
    event_starts_at  TIMESTAMPTZ,

    -- SB-768 introduces the controlled market taxonomy; until then these hold
    -- the operator's own labels rather than a vocabulary nobody has agreed.
    market_family    VARCHAR,
    market_name      VARCHAR,
    selection_name   VARCHAR,
    side             VARCHAR,
    line_value       DECIMAL(12,4),

    target_team      VARCHAR,
    target_player    VARCHAR,
    is_home          BOOLEAN,

    -- Operator-dependent, not merely optional. See the header.
    odds_american    INTEGER,
    odds_decimal     DECIMAL(12,4),

    result           VARCHAR,
    -- The achieved quantity, where the operator publishes one. Enables
    -- near-miss analysis ("my player-points props miss by an average of 1.4").
    -- Null on lost and voided legs, and null for every operator that renders
    -- only a tick.
    result_value     DECIMAL(12,4),

    created_at       TIMESTAMPTZ   NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ   NOT NULL DEFAULT now(),

    PRIMARY KEY (tenant_id, user_id, id),
    UNIQUE (tenant_id, user_id, bet_id, leg_order),
    FOREIGN KEY (tenant_id, user_id, bet_id) REFERENCES core.bet (tenant_id, user_id, id),
    FOREIGN KEY (tenant_id, user_id, group_id)
        REFERENCES core.bet_leg_group (tenant_id, user_id, id),
    CONSTRAINT bet_leg_result_known
        CHECK (result IN ('won', 'lost', 'push', 'void', 'partial', 'cashed_out')),
    CONSTRAINT bet_leg_order_is_positive CHECK (leg_order >= 1)
);


-- ------------------------------------------------------------- promotions held

-- A promotion the user holds or was awarded: a specific boost token, a specific
-- free bet. The market-wide offer that generated it is market.offered_promotion,
-- which belongs to a later epic.
CREATE TABLE core.promotion (
    tenant_id       UUID          NOT NULL,
    user_id         UUID          NOT NULL,
    id              UUID          NOT NULL DEFAULT uuidv7(),
    sportsbook_code VARCHAR       NOT NULL,
    promotion_type  VARCHAR       NOT NULL,
    state           VARCHAR       NOT NULL DEFAULT 'awarded',

    external_ref    VARCHAR,
    label           VARCHAR,
    face_value      DECIMAL(12,2),
    generosity_pct  DECIMAL(8,4),

    awarded_at      TIMESTAMPTZ,
    expires_at      TIMESTAMPTZ,
    -- Recorded, never inferred from the gap between awarded and played: an
    -- inferred figure absorbs every reconciliation error in the ledger.
    expired_at      TIMESTAMPTZ,

    created_at      TIMESTAMPTZ   NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ   NOT NULL DEFAULT now(),

    PRIMARY KEY (tenant_id, user_id, id),
    FOREIGN KEY (sportsbook_code) REFERENCES core.sportsbook (code),
    CONSTRAINT promotion_type_known
        CHECK (promotion_type IN ('profit_boost', 'bonus_bet', 'insurance',
                                  'rewards_currency', 'other')),
    CONSTRAINT promotion_state_known
        CHECK (state IN ('awarded', 'played', 'expired')),
    CONSTRAINT promotion_expired_state_has_a_date
        CHECK ((state = 'expired') = (expired_at IS NOT NULL))
);


-- ------------------------------------------- promotions applied to a bet or leg

-- One table, not two. bet_leg_id carries the scope: a real ticket was observed
-- carrying a ticket-scoped profit boost and a leg-scoped Super Sub at the same
-- time, so both must coexist on one bet (SB-775).
CREATE TABLE core.bet_promotion (
    tenant_id      UUID          NOT NULL,
    user_id        UUID          NOT NULL,
    id             UUID          NOT NULL DEFAULT uuidv7(),
    bet_id         UUID          NOT NULL,
    bet_leg_id     UUID,
    promotion_id   UUID,

    -- Denormalized because an imported promotion often has no core.promotion
    -- row: the operator shows what was applied without ever exposing the token.
    promotion_type VARCHAR       NOT NULL,
    scope          VARCHAR       NOT NULL DEFAULT 'ticket',
    label          VARCHAR,

    -- Promotions stack. They apply in this order, each to the running profit,
    -- and the order is assigned by the importer profile because that is the only
    -- layer that knows the operator's composition rules (DATA_DICTIONARY 5.3).
    apply_order    INTEGER       NOT NULL DEFAULT 1,
    generosity_pct DECIMAL(8,4),

    -- A promotion attached but never paid out has a different economic value
    -- from one that fired. Conflating them distorts promotion performance.
    triggered      BOOLEAN,
    value_delivered DECIMAL(12,2),

    created_at     TIMESTAMPTZ   NOT NULL DEFAULT now(),

    PRIMARY KEY (tenant_id, user_id, id),
    UNIQUE (tenant_id, user_id, bet_id, promotion_type, apply_order),
    FOREIGN KEY (tenant_id, user_id, bet_id) REFERENCES core.bet (tenant_id, user_id, id),
    FOREIGN KEY (tenant_id, user_id, bet_leg_id)
        REFERENCES core.bet_leg (tenant_id, user_id, id),
    FOREIGN KEY (tenant_id, user_id, promotion_id)
        REFERENCES core.promotion (tenant_id, user_id, id),
    CONSTRAINT bet_promotion_type_known
        CHECK (promotion_type IN ('profit_boost', 'bonus_bet', 'insurance',
                                  'rewards_currency', 'other')),

    -- Scope cannot lie about itself.
    CONSTRAINT bet_promotion_scope_matches_leg
        CHECK ((scope = 'leg') = (bet_leg_id IS NOT NULL)),
    CONSTRAINT bet_promotion_scope_known
        CHECK (scope IN ('ticket', 'leg')),
    CONSTRAINT bet_promotion_apply_order_is_positive CHECK (apply_order >= 1)
);
