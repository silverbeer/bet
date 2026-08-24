# BET Data Dictionary — Money Model

**Status:** Frozen v1
**Date:** 2026-08-23
**Ticket:** SB-683
**Blocks:** warehouse (SB-702), importers (SB-711), analytics (SB-739, SB-740)

This document freezes the canonical money model. Every financial field on `Bet`
and `BetLeg` is defined here, along with the settlement vocabulary, promotion
economics, and the ROI formulas. Downstream work implements this; it does not
reinterpret it.

Every formula below is verified against real sportsbook specimens from four
operators (FanDuel, DraftKings, Fanatics, and a FanDuel annual statement and
bet-history API capture). Worked examples at the end reproduce those figures
exactly.

---

## 1. Governing principle

> **The money is the fact. The operator's status word is display text.**

Sportsbooks disagree about vocabulary and each rounds odds in its own direction,
but the amount that moved is unambiguous. Every canonical financial value is
derived from amounts. Odds are retained as *metadata about a decision*, never as
an input to reconstructing money.

This principle has one important qualification — see §4.3, where it turns out no
single global rule works across operators.

---

## 2. The naming collision, resolved

`BET_IMPLEMENTATION_PLAN.md` used three names for one quantity: `net_profit`,
`net_cash_profit`, and `realized_cash_return - realized_cash_cost`.

**The canonical name is `net_profit`.** The other two names are retired and must
not appear in code, schema, or documentation.

```
net_profit = cash_returned - cash_staked
```

It is always cash. Bonus stake is not cash (§5.1) and rewards currency is not
cash (§5.4); neither appears in this formula.

---

## 3. Core financial fields

### 3.1 On `Bet`

| Field | Type | Definition |
|---|---|---|
| `cash_staked` | `DECIMAL(12,2)` | Real money risked. Excludes bonus/free-bet stake. |
| `bonus_staked` | `DECIMAL(12,2)` | Promotional stake risked. Never returned on a win (§5.1). |
| `total_risk` | `DECIMAL(12,2)` | `cash_staked + bonus_staked`. Display convenience; not an ROI denominator. |
| `cash_returned` | `DECIMAL(12,2)` | Total cash paid back by the operator, **including** the returned stake on a win or push. Gross, not net. `0.00` on a loss. |
| `net_profit` | `DECIMAL(12,2)` | `cash_returned - cash_staked`. Signed. The single canonical P&L figure. |
| `rewards_earned` | `DECIMAL(12,4)` | Loyalty currency accrued by placing this bet, in cash-equivalent terms (§5.4). Earned regardless of outcome. |
| `currency` | `CHAR(3)` | ISO 4217. `USD` only at present; the column exists so it need not be retrofitted. |
| `odds_american_placed` | `INTEGER` | Ticket price at acceptance — the price the decision was made at. |
| `odds_decimal_placed` | `DECIMAL(10,4)` | Same, normalized. |
| `odds_american_settled` | `INTEGER` | Ticket price after voided legs are removed (§4.4). Equals placed when nothing voided. |
| `odds_decimal_settled` | `DECIMAL(10,4)` | Same, normalized. |
| `status` | enum | Lifecycle. See §4.1. |
| `result` | enum | Settled outcome. Null while pending. See §4.2. |
| `settlement_source` | enum | Which operator field the settlement was derived from: `amount`, `status_word`, or `both_agree`. See §4.3. |

### 3.2 On `BetLeg`

Legs carry `result`, `odds_american_placed` / `odds_decimal_placed`, and the
market/selection identity. **Legs carry no money.** Stake and return are
properties of the ticket, not of a selection within it. Attributing money to
legs would require inventing an allocation rule the operator never applied.

The one exception is a leg-scoped promotion, which is modelled on the
`bet_promotion` join rather than on the leg itself (SB-775).

---

## 4. Settlement vocabulary

### 4.1 `status` — lifecycle

| Value | Meaning |
|---|---|
| `pending` | Accepted, not yet resolved. `result` is null. |
| `settled` | Resolved. `result` is populated and money is final. |

Two values, deliberately. Anything richer belongs in `result`.

### 4.2 `result` — settled outcome

| Value | Money relationship | In ROI denominator? | In win rate? |
|---|---|---|---|
| `won` | `cash_returned > cash_staked` | Yes | Win |
| `lost` | `cash_returned = 0` | Yes | Loss |
| `push` | `cash_returned = cash_staked` | **Yes** | Excluded |
| `void` | `cash_returned = cash_staked` | **No** | Excluded |
| `partial` | `0 < cash_returned < cash_staked`, or a reduced win | Yes | By sign of `net_profit` |
| `cashed_out` | Settled early at an agreed amount | Yes | By sign of `net_profit` |

`partial` covers dead heats, Asian-handicap half-wins and half-losses, and
partial cash-outs. These are distinguished from one another by `net_profit` and
by the promotion/settlement detail, not by proliferating enum values that
importers would have to guess between.

**`push` and `void` are numerically identical and semantically opposite.** Both
return the stake. A push is a bet that happened and tied; a void is a bet that
never happened. They cannot be collapsed, because they belong on opposite sides
of every denominator.

### 4.3 Push is in the denominator; void is not

ROI is return on capital actually at risk.

- A **push** committed capital. The event was played, the outcome was neutral.
  It belongs in the denominator, contributing `0` to the numerator and dragging
  ROI toward zero. Excluding it shrinks the denominator and flatters the result.
- A **void** never put capital at risk. The bet is expunged. It belongs in no
  denominator and no count.

Voided bets are excluded by default from every report. The `--include-void` flag
already specified in the plan's CLI conventions surfaces them when needed.

> **Correction.** An earlier informal recommendation in this project said to
> exclude pushes from the ROI denominator. That was wrong and is superseded
> here. It would have inflated reported ROI on any book where pushes are common.

### 4.4 Deriving settlement is a per-profile decision, not a global rule

The governing principle in §1 says the money is authoritative. Real specimens
show that is correct for FanDuel and **wrong as a universal rule**:

| | FanDuel | DraftKings |
|---|---|---|
| Status word | `RETURNED` — identical for a loss and a refund | `Lost` — explicit |
| Returned amount | `$0.00` — authoritative | not displayed at all |

FanDuel shows the money and an unusable word. DraftKings shows the word and
withholds the money. Neither reliably supplies both.

**Rule:** each importer profile declares which field is authoritative for
settlement — `amount`, `status_word`, or `both_agree`. The recorded choice goes
in `settlement_source`. Where both are present, the validator asserts they agree
and raises a **blocking** issue when they conflict. This belongs to the profile
contract (SB-710), not to a shared constant.

Mapping FanDuel's `RETURNED` to void or push would refund the stake, drop the
bet out of the ROI and win-rate denominators, and **silently inflate ROI while
hiding losses**. This is the single most dangerous misreading available in the
data, and it is why `settlement_source` is stored rather than assumed.

### 4.5 A voided leg reprices the ticket

When a leg of a parlay voids, that leg drops out and the ticket reprices to the
surviving legs. A 2-leg SGP priced `+377` with one leg voided settles at the
surviving leg's price alone.

**The displayed ticket odds then describe a bet that no longer exists.**

Hence two odds pairs (§3.1):

- **Odds-band analytics use `odds_placed`.** The question is "which prices do I
  bet well at", and the price you decided at is the placed one.
- **Payout validation uses `odds_settled`**, and only as a soft check (§4.6).

### 4.6 Never reconstruct money from odds

Displayed odds are rounded, and the rounding is operator-specific and
directional:

```
Fanatics:  -180 +10%  -> true -163.6, displayed -164   (worse for the bettor)
Fanatics:  +145 +10%  -> true +159.5, displayed +159   (worse for the bettor)
FanDuel:   +126 +30%  -> true +163.8, displayed +164   (nearest)
FanDuel:   a price displayed +101 was truly ~+100.64   (payout differed by 2c)
```

Fanatics rounds systematically against the bettor; FanDuel rounds to nearest.
Any payout derived from displayed odds is therefore wrong by cents in an
operator-dependent direction.

Amounts are stored as reported. Odds-derived payout is a **warning-level**
validation check with a tolerance, never a source of stored value.

---

## 5. Promotion economics

Promotions are structured data, never parsed prose. The FanDuel API supplies
exactly this shape, confirming the model:

```json
"rewardUsed": {"type": "PROFIT_BOOST", "generosity": 30}
```

### 5.1 Bonus bets / free bets — stake is not returned

A free bet returns **profit only**. The stake is consumed.

Confirmed in a real annual statement: every bonus row carried
`Cash Wagered = 0.00`. No hybrid cash+bonus wagers were observed, though the
schema permits them.

```
bonus_staked 25.00, cash_staked 0.00, cash_returned 21.50  ->  net_profit +21.50
```

**A free bet is excluded from `cash_roi` entirely** — numerator and denominator
both. Its `cash_staked` is zero, so including it would divide by zero or, worse,
add winnings to a numerator whose denominator never grew. It appears in
`economic_roi` only.

### 5.2 Profit boosts

Confirmed on four operators and on both positive and negative base prices:

```
boosted_profit = base_profit x (1 + generosity/100)
```

Applied to **profit**, not to stake, and not to the return.

Observed multipliers from the API (`potentialWin` vs `originalPotentialWin`,
both of which are *total returns*, not profits): `1.299, 1.301, 1.300, 1.302,
1.314, 1.501` — cent-rounding accounts for the drift.

Store `base_profit` and `boosted_profit` separately. The difference is
promotional value delivered, and it is the whole point of `economic_roi`.

### 5.3 Stacking

Multiple promotions can apply to one ticket — one specimen carried a 30% profit
boost and a "Super Sub" together.

**Rule:** promotions apply sequentially in `apply_order` on the `bet_promotion`
join, each to the running profit. `apply_order` is assigned by the importer
profile, which is the only layer that knows the operator's composition rules.
Where the order cannot be determined, the importer raises a review-required
issue rather than guessing — two boosts composed in the wrong order produce a
different number, and no downstream layer can detect the error.

Implementation: SB-775.

### 5.4 Rewards currency — value earned *from* a bet

Every Fanatics specimen — **winners and losers alike** — accrued loyalty
currency worth roughly 0.4–0.8% of stake, independent of outcome.

The model had promotions applied *to* a bet but nothing for value earned *from*
one. Across high volumes of small stakes this is a material offset, and it
accrues on losing bets, so ignoring it understates true ROI.

**Decisions:**

- Modelled as `rewards_earned` on `Bet`, in **cash-equivalent** terms, at
  `DECIMAL(12,4)` — per-bet accruals are sub-cent and must not round to zero.
- **Recognized on accrual**, when the bet is placed, not on redemption.
  Redemption is not observable per-bet, and deferring recognition would strand
  the value indefinitely.
- Included in `economic_roi`. Excluded from `cash_roi`.
- The cash-equivalent rate is a per-operator configuration value, defaulting to
  1:1, and is recorded with the bet so historical figures do not move when the
  rate is revised.

Implementation: SB-776.

### 5.5 Promotion lifecycle and expiry

A real Player Activity Statement reports three states, of which the third had
no representation in the model (figures below illustrative):

```
awarded  $200.00
played   $190.00
expired   $10.00
```

**Expired promotional value is real value forgone and had no representation in
the schema.** The lifecycle is:

```
awarded -> played | expired
```

Expiry is **recorded**, never inferred from the gap between awarded and played —
an inferred figure silently absorbs every reconciliation error in the promotion
ledger.

---

## 6. ROI formulas

Two ratios, sharing a denominator, answering different questions.

### 6.1 `cash_roi` — "am I good at picking?"

```
cash_roi = sum(net_profit) / sum(cash_staked)
```

Over bets where `cash_staked > 0` and `result != 'void'`. Free bets are excluded
by that first condition. Promotional value is invisible here by design.

### 6.2 `economic_roi` — "am I good, or am I being paid to look good?"

The plan stated `economic_roi = economic_net_value / economic_risk` and defined
neither term. Both are now defined:

```
economic_risk     = cash_staked                     -- only cash is ever at risk
economic_net_value = net_profit                     -- cash P&L, all bets
                   + free_bet_winnings              -- from zero-cash-stake bets
                   + rewards_earned                 -- accrued loyalty value

economic_roi = sum(economic_net_value) / sum(economic_risk)
```

`economic_risk` is `cash_staked`, not `total_risk`: **bonus stake costs nothing
to lose**, so counting it as risk understates performance on exactly the bets
promotions were meant to subsidise.

### 6.3 Why both are needed

A real statement period showed promotional awards running at roughly **5% of
handle** alongside a near-break-even cash result of about **−0.7%**. Illustrative
shape:

```
promotions awarded   200.00
handle             4,000.00      -> subsidy 5.0% of turnover
cash result          -30.00      -> -0.75%
```

A near-flat cash result while being subsidised at ~5% of turnover implies the
underlying betting is meaningfully negative and the promotions are carrying it.
That is the product's central question, and it is answerable only if promotional
value is tracked separately from cash throughout.

### 6.4 Weighting

Weighted ROI (`sum(profit) / sum(stake)`) is the only ROI. **Never average
per-bet ROI values.** An unweighted mean lets a $2 bet move the number as much
as a $200 one.

---

## 7. Operator field traps

Confirmed hazards. Importers must encode these; the names actively invite bugs.

| Field | Trap | Correct handling |
|---|---|---|
| `pandl` (FanDuel API) | **Not profit and loss.** On winners it equals `potentialWin`, the *gross return* — e.g. `34.00` on a `10.00` stake. On losers it is `0`. | `net_profit = pandl - currentSize`, i.e. `34.00 - 10.00 = 24.00`. Mapping `pandl` directly overstates every winning bet by the stake, and nothing in the data flags it. |
| `originalPotentialWin` | Unreliable for boost detection — `0.0` on some genuinely boosted bets, equal to `potentialWin` on another. | Detect boosts from `rewardUsed`, or from part-level `originalAmericanPrice` vs `americanPrice` (present on 43/43 parts observed). |
| `RETURNED` (FanDuel) | Neutral label covering both loss and refund. | See §4.4. |
| Displayed boosted odds | Rounded, operator-specific direction. | See §4.6. |

---

## 8. Open question referred onward

The Player Activity Statement's asterisk excludes bonus funds *used as stake*,
but does not say whether winnings *from* bonus bets are included in "amount
won". That ambiguity materially changes any statement-derived figure.

Similarly, the annual statement reviewed contains **no push or void
vocabulary** — outcomes are only `win`/`lose`, and no row has returned equal to
staked. Either the period had no voids, or voids are silently excluded. A silently excluded void is
P&L-neutral but changes bet counts and win-rate denominators.

**Both must be resolved before any statement-derived win rate is trusted.**
Neither blocks this dictionary: both concern one import source's completeness,
not the canonical model. Referred to SB-689 (source cataloguing).

---

## 9. Worked examples

**All amounts below are synthetic.** They demonstrate the arithmetic without
placing any real stake, balance, or period total in this public repository. The
formulas were verified against real specimens from four operators; those
specimens and their reconciliations live in SB-683 and the private capture
record, never here.

These become the fixtures for SB-741.

Rounding is half-up at two decimal places; `DECIMAL(12,2)` for money,
`DECIMAL(10,4)` for decimal odds. Confirmed by SB-684.

### 9.1 Straight win

```
stake $10.00 cash @ -150
  base_profit    = 10.00 x (100/150)     = 6.6667
  cash_returned  = 10.00 + 6.67          = 16.67
  net_profit     = 16.67 - 10.00         = +6.67
  cash_roi       = 6.67 / 10.00          = +66.7%
  economic_roi   = same (no promotion)   = +66.7%
  result = won
```

### 9.2 Boosted win

```
stake $20.00 cash @ +150, PROFIT BOOST 25%
  base_profit    = 20.00 x 1.50          = 30.00
  boosted_profit = 30.00 x 1.25          = 37.50
  cash_returned  = 20.00 + 37.50         = 57.50
  net_profit                             = +37.50
  cash_roi       = 37.50 / 20.00         = +187.5%
  promotional value delivered = 37.50 - 30.00 = 7.50
  result = won
```

The displayed boosted price would be rounded to a whole American number and is
not used for any calculation (§4.6).

### 9.3 Free-bet win

```
bonus_staked $25.00, cash_staked $0.00, cash_returned $21.50
  net_profit     = 21.50 - 0.00          = +21.50
  cash_roi       EXCLUDED — cash_staked is zero
  economic_roi   numerator +21.50, denominator 0
  result = won
```

Stake is not returned: the bet paid profit alone.

### 9.4 Voided leg in a parlay

```
2-leg SGP, stake $10.00 cash, placed @ +377
  leg A voids, leg B loses
  odds_american_placed  = +377          <- used for odds-band analytics
  odds_american_settled = leg B alone   <- ticket repriced
  cash_returned = 0.00
  net_profit    = -10.00
  result = lost      (NOT void — the surviving leg lost)
```

The ticket is a **loss**, not a void. Only the leg voided. FanDuel displays
`$0.00 RETURNED` here — the exact wording it would use for a full refund, at a
different amount. See §4.4.

### 9.5 Parlay with a push leg

```
3-leg parlay, stake $10.00 cash, one leg pushes, two legs win
  the pushed leg drops out; ticket reprices to the two winners
  cash_returned = 24.50
  net_profit    = 24.50 - 10.00          = +14.50
  result = won
```

The ticket result is `won`. `push` at ticket level applies only when the whole
ticket returns exactly the stake.

### 9.6 Cash-out

```
stake $40.00 cash, cashed out for $52.00 before settlement
  cash_returned = 52.00
  net_profit    = 52.00 - 40.00          = +12.00
  cash_roi      = 12.00 / 40.00          = +30.0%
  result = cashed_out
  odds_settled: not meaningful — the bet did not run to term
```

Counts in the ROI denominator. Payout-vs-odds validation is skipped for
`cashed_out`, since the price was never realised.

### 9.7 Full-period reconciliation

The case for keeping the two ratios separate. A real annual statement was
reconciled against these definitions and matched to the cent; the shape is
reproduced here with synthetic totals:

```
cash_staked        1,200.00
cash_returned      1,050.00      (excludes free-bet winnings)
net_profit          -150.00
cash_roi             -12.50%

free_bet_winnings     +40.00
economic_net_value   -110.00
economic_roi          -9.17%
```

Both numbers are correct. They answer different questions, and reporting only
one of them would misrepresent the period.
