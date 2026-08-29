-- Controlled taxonomies.
--
-- These are reference tables rather than DuckDB ENUM types on purpose. An ENUM
-- carries a name and nothing else, and several of these vocabularies carry
-- *semantics* that would otherwise be scattered through query code — most
-- importantly which settlement results belong in an ROI denominator.
--
-- Putting those rules in a table means analytics joins to them instead of
-- repeating `WHERE result <> 'void'` in a dozen views, and it means `bet` can
-- show a user the rule it applied rather than asserting a number.
--
-- Every vocabulary here is frozen by docs/DATA_DICTIONARY.md (SB-683) or by
-- BET_IMPLEMENTATION_PLAN.md section 3. Market families and selection types are
-- deliberately NOT created here: they belong to SB-768, and inventing them now
-- would freeze a decision that ticket has not made.
--
-- Adding a value later is a new forward migration, never an edit to this file.


-- Lifecycle of a bet. Two values, deliberately; anything richer is a result.
CREATE TABLE reference.bet_status (
    code        VARCHAR NOT NULL PRIMARY KEY,
    label       VARCHAR NOT NULL,
    description VARCHAR NOT NULL,
    sort_order  INTEGER NOT NULL
);

INSERT INTO reference.bet_status (code, label, description, sort_order) VALUES
    ('pending', 'Pending', 'Accepted by the sportsbook, not yet resolved.', 1),
    ('settled', 'Settled', 'Resolved; money is final and result is populated.', 2);


-- Settled outcome, and the analytics semantics of each.
--
-- counts_in_roi_denominator encodes the rule from DATA_DICTIONARY section 4.3:
-- a push committed capital and the outcome was neutral, so it belongs in the
-- denominator and drags ROI toward zero. A void never put capital at risk, so
-- it belongs in no denominator and no count.
CREATE TABLE reference.bet_result (
    code                      VARCHAR NOT NULL PRIMARY KEY,
    label                     VARCHAR NOT NULL,
    counts_in_roi_denominator BOOLEAN NOT NULL,
    win_rate_treatment        VARCHAR NOT NULL,
    description               VARCHAR NOT NULL,
    sort_order                INTEGER NOT NULL,
    CONSTRAINT bet_result_win_rate_treatment_known
        CHECK (win_rate_treatment IN ('win', 'loss', 'excluded', 'by_profit_sign'))
);

INSERT INTO reference.bet_result
    (code, label, counts_in_roi_denominator, win_rate_treatment, description, sort_order)
VALUES
    ('won',        'Won',        TRUE,  'win',
     'Returned more than the cash staked.', 1),
    ('lost',       'Lost',       TRUE,  'loss',
     'Returned nothing.', 2),
    ('push',       'Push',       TRUE,  'excluded',
     'Tied; stake returned. Capital was at risk, so it counts in the ROI denominator.', 3),
    ('void',       'Void',       FALSE, 'excluded',
     'Nullified; stake returned. Capital was never at risk, so it counts in nothing.', 4),
    ('partial',    'Partial',    TRUE,  'by_profit_sign',
     'Dead heat, half-win, half-loss, or partial cash-out. Net profit gives the sign.', 5),
    ('cashed_out', 'Cashed out', TRUE,  'by_profit_sign',
     'Settled early at an agreed amount; the price was never realised.', 6);


-- Ticket shape. From BET_IMPLEMENTATION_PLAN.md section 3.
CREATE TABLE reference.wager_kind (
    code        VARCHAR NOT NULL PRIMARY KEY,
    label       VARCHAR NOT NULL,
    is_multi_leg BOOLEAN NOT NULL,
    description VARCHAR NOT NULL,
    sort_order  INTEGER NOT NULL
);

INSERT INTO reference.wager_kind (code, label, is_multi_leg, description, sort_order) VALUES
    ('straight',          'Straight',          FALSE, 'A single selection.', 1),
    ('parlay',            'Parlay',            TRUE,  'Several selections; all must win.', 2),
    ('same_game_parlay',  'Same game parlay',  TRUE,  'A parlay whose legs share one event.', 3),
    ('teaser',            'Teaser',            TRUE,  'A parlay with the lines moved in the bettor''s favour.', 4),
    ('round_robin',       'Round robin',       TRUE,  'A set of smaller parlays across the same selections.', 5),
    ('future',            'Future',            FALSE, 'An outcome settled at the end of a season or event.', 6),
    ('system',            'System',            TRUE,  'A combination bet settling on a subset of legs.', 7),
    ('unknown',           'Unknown',           FALSE, 'Shape not determinable from the source. Preserved rather than guessed.', 8);


-- Promotion economics. See DATA_DICTIONARY section 5.
--
-- applies_to records what the promotion acts on, because getting this wrong is
-- a silent money bug: a profit boost multiplies profit, never stake.
CREATE TABLE reference.promotion_type (
    code            VARCHAR NOT NULL PRIMARY KEY,
    label           VARCHAR NOT NULL,
    applies_to      VARCHAR NOT NULL,
    in_cash_roi     BOOLEAN NOT NULL,
    in_economic_roi BOOLEAN NOT NULL,
    description     VARCHAR NOT NULL,
    sort_order      INTEGER NOT NULL,
    CONSTRAINT promotion_type_applies_to_known
        CHECK (applies_to IN ('profit', 'stake', 'loss', 'none'))
);

INSERT INTO reference.promotion_type
    (code, label, applies_to, in_cash_roi, in_economic_roi, description, sort_order)
VALUES
    ('profit_boost', 'Profit boost', 'profit', TRUE, TRUE,
     'Multiplies profit by (1 + generosity). Confirmed on four operators.', 1),
    ('bonus_bet', 'Bonus bet', 'stake', FALSE, TRUE,
     'Stake is promotional and is not returned on a win; profit only.', 2),
    ('insurance', 'Insurance', 'loss', FALSE, TRUE,
     'Refunds a losing bet, usually as bonus funds and usually capped.', 3),
    ('rewards_currency', 'Rewards currency', 'none', FALSE, TRUE,
     'Loyalty value earned by placing the bet, accruing on losers as well as winners.', 4),
    ('other', 'Other', 'none', FALSE, TRUE,
     'A promotion whose economics are not yet modelled. Recorded, never silently dropped.', 5);


-- Promotion lifecycle. Expiry is recorded, never inferred from the gap between
-- awarded and played: an inferred figure absorbs every reconciliation error in
-- the promotion ledger.
CREATE TABLE reference.promotion_state (
    code        VARCHAR NOT NULL PRIMARY KEY,
    label       VARCHAR NOT NULL,
    description VARCHAR NOT NULL,
    sort_order  INTEGER NOT NULL
);

INSERT INTO reference.promotion_state (code, label, description, sort_order) VALUES
    ('awarded', 'Awarded', 'Granted to the account and not yet used.', 1),
    ('played',  'Played',  'Applied to a bet.', 2),
    ('expired', 'Expired', 'Never used before it lapsed. Real value forgone.', 3);


-- Which operator field a profile treats as authoritative for settlement.
-- See DATA_DICTIONARY section 4.4: no single global rule works, because
-- FanDuel supplies a usable amount with an ambiguous word and DraftKings
-- supplies a usable word with no amount.
CREATE TABLE reference.settlement_source (
    code        VARCHAR NOT NULL PRIMARY KEY,
    label       VARCHAR NOT NULL,
    description VARCHAR NOT NULL,
    sort_order  INTEGER NOT NULL
);

INSERT INTO reference.settlement_source (code, label, description, sort_order) VALUES
    ('amount',      'Returned amount', 'Derived from the money returned. The operator''s status word is unreliable.', 1),
    ('status_word', 'Status word',     'Derived from the operator''s label. No returned amount is published.', 2),
    ('both_agree',  'Both agree',      'Amount and status word were both present and consistent.', 3);


-- Odds notation as supplied by the source. Normalised decimal odds are stored
-- alongside, but the original format is retained so a value can always be
-- traced back to what the operator actually showed.
CREATE TABLE reference.odds_format (
    code        VARCHAR NOT NULL PRIMARY KEY,
    label       VARCHAR NOT NULL,
    description VARCHAR NOT NULL,
    sort_order  INTEGER NOT NULL
);

INSERT INTO reference.odds_format (code, label, description, sort_order) VALUES
    ('american',   'American',   'Positive is profit on 100 staked; negative is stake needed to win 100.', 1),
    ('decimal',    'Decimal',    'Total return per unit staked, stake included.', 2),
    ('fractional', 'Fractional', 'Profit relative to stake, as a fraction.', 3);


-- How a record entered BET, and how much the record can be trusted.
--
-- Trust level exists so analytics can be segregated by it (SB-761): a bet typed
-- in by hand and a bet parsed from a screenshot are not equally reliable, and
-- averaging them without saying so would hide that.
CREATE TABLE reference.capture_method (
    code        VARCHAR NOT NULL PRIMARY KEY,
    label       VARCHAR NOT NULL,
    trust_level VARCHAR NOT NULL,
    description VARCHAR NOT NULL,
    sort_order  INTEGER NOT NULL,
    CONSTRAINT capture_method_trust_level_known
        CHECK (trust_level IN ('authoritative', 'high', 'medium', 'low'))
);

INSERT INTO reference.capture_method (code, label, trust_level, description, sort_order) VALUES
    ('export',     'Sportsbook export', 'authoritative', 'A file published by the operator.', 1),
    ('api',        'Sportsbook API',    'authoritative', 'A response captured from the operator''s own API.', 2),
    ('statement',  'Statement PDF',     'high',          'An operator statement; totals reconcile but detail may be summarised.', 3),
    ('manual',     'Manual entry',      'high',          'Typed in by the user at the time of placing the bet.', 4),
    ('pdf',        'Parsed PDF',        'medium',        'Extracted from a PDF that was not a formal statement.', 5),
    ('screenshot', 'Screenshot / OCR',  'low',           'Extracted from an image. Requires review before it is trusted.', 6);
