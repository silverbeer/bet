-- Account balances and the cash that moves in and out of them.
--
-- SB-1333. Bets alone cannot say whether the ledger is complete: a bet that
-- was never captured, or captured with the wrong stake, is invisible to every
-- analytic built on core.bet. The sportsbook's own reported balance is an
-- independent check. Given the balance at two moments, the deposits and
-- withdrawals between them and the bets placed and settled between them, the
-- second balance is predictable to the cent; a gap means the record is wrong.
--
-- Two tables, both append-only. A balance is an observation at a moment, not
-- a running total that gets edited; a deposit is an event that happened. Each
-- row is a fact as captured, so neither table carries updated_at. A later
-- correction is a new row that names the one it replaces in supersedes_id,
-- reserved here and unused until a correction workflow exists (SB-703).
--
-- Keyed on sportsbook_account, not sportsbook: the user holds several
-- accounts, and a balance belongs to exactly one of them.
--
-- capture_method and source_file_id follow core.bet exactly (0004, 0007). A
-- balance read off a screenshot is a transcription, and must never be
-- indistinguishable from one typed while looking at the real number.
--
-- Bonus and free-bet credit is not cash and is never part of the cash
-- reconciliation. bonus_balance is recorded because operators display it next
-- to the cash figure and it is worth having, but nothing reconciles against
-- it: free-bet credit expires, is awarded without a cash event, and is
-- consumed without one, so no cash equation can account for it.


-- ------------------------------------------------------------ balance snapshot

CREATE TABLE core.balance_snapshot (
    tenant_id             UUID          NOT NULL,
    user_id               UUID          NOT NULL,
    id                    UUID          NOT NULL DEFAULT uuidv7(),
    sportsbook_account_id UUID          NOT NULL,

    observed_at           TIMESTAMPTZ   NOT NULL,
    currency              VARCHAR(3)    NOT NULL DEFAULT 'USD',
    -- Withdrawable cash as the operator reports it.
    cash_balance          DECIMAL(12,2) NOT NULL,
    -- Recorded, never reconciled. See the header.
    bonus_balance         DECIMAL(12,2),

    capture_method        VARCHAR       NOT NULL DEFAULT 'manual',
    -- No foreign key: control.source_file is in another schema (see 0007).
    source_file_id        UUID,

    supersedes_id         UUID,
    notes                 VARCHAR,
    created_at            TIMESTAMPTZ   NOT NULL DEFAULT now(),

    PRIMARY KEY (tenant_id, user_id, id),
    FOREIGN KEY (tenant_id, user_id, sportsbook_account_id)
        REFERENCES core.sportsbook_account (tenant_id, user_id, id),
    CONSTRAINT balance_snapshot_capture_method_known
        CHECK (capture_method IN ('export', 'api', 'statement', 'manual', 'pdf', 'screenshot')),
    CONSTRAINT balance_snapshot_money_is_not_negative
        CHECK (cash_balance >= 0 AND (bonus_balance IS NULL OR bonus_balance >= 0))
);


-- ------------------------------------------------------------------ cash flow

-- Deposits and withdrawals in one table: they are the same event in opposite
-- directions, and a reconciliation reads both over the same interval.
-- amount is always positive; kind carries the sign.
CREATE TABLE core.cash_flow (
    tenant_id             UUID          NOT NULL,
    user_id               UUID          NOT NULL,
    id                    UUID          NOT NULL DEFAULT uuidv7(),
    sportsbook_account_id UUID          NOT NULL,

    kind                  VARCHAR       NOT NULL,
    occurred_at           TIMESTAMPTZ   NOT NULL,
    currency              VARCHAR(3)    NOT NULL DEFAULT 'USD',
    amount                DECIMAL(12,2) NOT NULL,

    capture_method        VARCHAR       NOT NULL DEFAULT 'manual',
    source_file_id        UUID,

    supersedes_id         UUID,
    notes                 VARCHAR,
    created_at            TIMESTAMPTZ   NOT NULL DEFAULT now(),

    PRIMARY KEY (tenant_id, user_id, id),
    FOREIGN KEY (tenant_id, user_id, sportsbook_account_id)
        REFERENCES core.sportsbook_account (tenant_id, user_id, id),
    CONSTRAINT cash_flow_kind_known
        CHECK (kind IN ('deposit', 'withdrawal')),
    CONSTRAINT cash_flow_capture_method_known
        CHECK (capture_method IN ('export', 'api', 'statement', 'manual', 'pdf', 'screenshot')),
    CONSTRAINT cash_flow_amount_is_positive
        CHECK (amount > 0)
);
