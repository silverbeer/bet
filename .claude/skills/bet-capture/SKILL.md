---
name: bet-capture
description: >-
  Turn sportsbook bet-slip screenshots into recorded bets via the `bet` CLI.
  Reads images from ~/bet-inbox, extracts the ticket, confirms every value with
  the user, then writes it with `bet add --capture-method screenshot
  --source-file`. Also settles already-decided slips. Use when the user drops a
  bet-slip screenshot, says "log my bets", "process the inbox", "add this bet",
  "capture these", or points at an image of a wager. Do NOT use for sportsbook
  CSV/PDF exports — those are the import path (SB-689, SB-715), not this one.
---

# Capturing bets from screenshots

The daily loop. Screenshots of bet slips land in `~/bet-inbox`; this turns them
into rows in the warehouse without the user retyping anything.

**The thing that makes this loop legitimate is that a transcribed bet is
labelled as transcribed.** A screenshot read by a model is a transcription, and
transcriptions are wrong occasionally and silently. Every bet written here is
tagged `capture_method='screenshot'` and cites the archived image, so a figure
that looks wrong in six months can be checked against the picture instead of
being trusted or discarded on a hunch. Never write a screenshot-derived bet
without those flags.

## The loop

```bash
ls ~/bet-inbox/*.{png,PNG,jpg,JPG,jpeg,heic,HEIC} 2>/dev/null   # what is waiting
```

For each image, in order:

1. **Read it.** The Read tool renders images. Extract one ticket per slip —
   sportsbook, each leg, stake, and (if settled) the outcome.
2. **Confirm with the user before writing.** See below. This step is not
   optional and not a formality.
3. **Write it**, with the source file cited.
4. **Move the file** to `~/bet-inbox/done/`. Never delete it.

### Writing a bet

<!-- Every figure in this file is invented for illustration.
     bet-guard: synthetic-amounts -->

```bash
bet add --non-interactive \
  --sportsbook fanduel \
  --leg 'sport=NFL,league=NFL,market=spread,selection=Chiefs -3.5,odds=-110,team=Chiefs' \
  --stake 25.00 \
  --capture-method screenshot \
  --source-file ~/bet-inbox/IMG_4417.PNG
```

Repeat `--leg` once per leg of a parlay, in the order they appear on the slip —
leg order is what `--leg-result` refers to later, so it has to match the image.

- **Sportsbook codes:** `fanduel`, `draftkings`, `betmgm`, `fanatics`,
  `caesars`, `bally_bet`, `thescore_bet`. Anything else is rejected.
- **`--leg` keys:** `sport`, `league`, `market`, `selection`, `odds`
  (required), `line`, `side`, `team`, `player`. Unknown keys are rejected.
- **Odds are American**, and `-100 < odds < 100` is refused as a dead zone.
- **Free bets** use `--bonus-stake` instead of `--stake`. They are not cash and
  never enter cash ROI, so putting a free bet in `--stake` is a real error, not
  a rounding one.
- **`--placed-at`** takes ISO 8601. Pass it when the slip shows a placement
  time; the default is now, which is wrong for anything captured later.
- `--source-file` requires a non-manual `--capture-method`. Passing it with
  `manual` is refused as a contradiction.

The archive is content-addressed, so **re-running a slip is safe**: the same
image archives once, and two bets read off one screenshot cite one file.

### Settling a slip that already shows its outcome

```bash
bet settle <bet_id> --result won --leg-result 1=won --leg-result 2=lost
```

`--result` is one of `won|lost|push|void|partial|cashed_out`. Use
`--return` only when the slip states an amount returned that differs from the
computed payout — a cash-out is the usual reason.

## Confirming: what to say and what not to

SB-759 has not shipped, so there is **nowhere in the warehouse to record that a
field was a guess**. Until it does, the confirmation message is the only place
uncertainty can exist. That makes it load-bearing.

Present the extracted ticket, and **mark the fields you are unsure of** rather
than listing everything at one level of confidence. Then wait for the user.

- **Never infer a stake.** If it is blurred, cropped or ambiguous, ask. A wrong
  stake corrupts every ROI figure downstream and is indistinguishable from a
  right one afterwards.
- **Leg status is encoded in icon colour, not text** — green tick won, red
  cross lost, orange exclamation `Void` (the only one carrying a word). This
  holds on both FanDuel and DraftKings and appears to be the industry
  convention. It is the field most likely to be misread, so say explicitly that
  it came from a colour rather than presenting it like a transcribed number.
- **A cropped slip is a partial slip.** If the image cuts off legs, say how
  many you can see and ask rather than recording a shorter parlay — a
  three-leg parlay recorded as two legs is a different bet with different odds.
- If the user corrects a value, use the correction as given. Do not re-derive
  it from the image.

## Boundaries

- **Exports are not this path.** A CSV or PDF from a sportsbook goes through
  the import path (SB-689, SB-715), which is higher-trust and not yet built.
  Do not screenshot an export to get it in faster.
- **This does not place bets.** BET analyses what already happened.
- **Do not batch-write without confirmation.** Processing ten slips means ten
  confirmations, or one confirmation listing all ten with per-slip
  uncertainties called out — not a silent loop.
- If `bet` is not on PATH, `uv run bet ...` from the repository works.
