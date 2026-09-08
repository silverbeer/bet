-- The first analytics view: settled bets, joined to the ROI/win-rate
-- semantics reference.bet_result carries.
--
-- Exists so a bet settled by `bet settle` (SB-812) is visible to analytics
-- immediately, and so later dimension views (SB-742) join to this rather than
-- repeating `WHERE status = 'settled' AND is_current` plus the
-- reference.bet_result join in every one of them -- exactly the reasoning
-- 0002_reference_taxonomies.sql gives for putting ROI semantics in a table.

CREATE VIEW analytics.v_settled_bets AS
SELECT
    b.*,
    r.counts_in_roi_denominator,
    r.win_rate_treatment
FROM core.bet b
JOIN reference.bet_result r ON r.code = b.result
WHERE b.status = 'settled' AND b.is_current;
