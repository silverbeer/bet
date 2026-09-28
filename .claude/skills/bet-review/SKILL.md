---
name: bet-review
description: >-
  Review a bet the user is *considering* before they place it. The user sends a
  screenshot of a bet slip (or describes the bet); Claude reads it, checks the
  user's own settled history with `bet similar`, checks recent form with cited
  web sources, and gives short feedback. Writes nothing. Use when the user says
  "review this bet", "should I place this", "what do you think of this", "thoughts
  on this slip", or sends a slip that has not been placed yet (no BET ID /
  PLACED line, usually a "Place bet" button). For a slip that already shows a
  BET ID or a result, use bet-capture instead.
---

# Reviewing a proposed bet

<!-- Every figure in this file is invented for illustration.
     bet-guard: synthetic-amounts -->

The user decides; this gives them their own record and the recent facts, in
under a minute of reading. **The user never types CLI flags.** They send a
screenshot or a sentence. Translating it into `bet similar` is Claude's job.

## The loop

### 1. Read the slip

Extract the same fields bet-capture would: sportsbook, each leg (sport, league,
market, selection, line, side, team, player, odds), ticket price, wager kind,
promotions, stake. Use the vocabulary already in the warehouse so history
matches:

- sport/league: `NFL/NFL`, `NCAAF/NCAAF`, `WNBA/WNBA`, `MLB/MLB`,
  `SOCCER/EPL|UCL|MLS`
- markets: `anytime_touchdown_scorer`, `receiving_yds`, `rushing_yds`,
  `receptions`, `alt_receiving_yds`, `alt_rushing_yds`, `alt_passing_yds`,
  `alt_passing_tds`, `alt_receptions`, `alt_rushing_receiving_yds`, `moneyline`,
  `spread`, `alternate_spread`, `alt_points`, `alt_threes`, `alt_assists`,
  `alt_rebounds`, `to_score_or_assist`
- A boosted price is recorded as the **base** price plus
  `--promo 'type=profit_boost,generosity_pct=N'`, exactly as capture does.
- Team names in full (`Las Vegas Raiders`, not `LV`).

Nothing is written, so a misread costs a wrong comparison, not a wrong record.
State what was read in one line and **proceed without waiting**; mark any field
you are unsure of so the user can correct it.

### 2. History: run `bet similar`

```bash
bet --format json similar \
  --leg 'sport=NFL,league=NFL,market=receiving_yds,selection=Example Player Over 55.5,side=over,line=55.5,team=Example Team,player=Example Player,odds=-115'
```

Repeat `--leg` per leg; add `--odds` (ticket price, base) and
`--wager-kind same_game_parlay` for an SGP; add `--promo` for a boost.

Read the rows by their `flag`:

- `no history` — say so plainly. No record is not a bad record.
- `insufficient` (n < 30) — report the numbers **as anecdote**: "3 of 3 on
  Bowers TDs, too few to mean anything". Never call it an edge.
- `exploratory` (n < 100) — may be called a lean, with n stated.
- blank — a finding. Rare at this stage.

Leg rows count legs; their money columns are singles only. Do not quote a leg
row's ROI as if it covered parlays.

**Wager-shape policy.** From 2026-09-18 the user bets singles and 2-leg only.
A 3+-leg ticket gets one line saying so, with the `shape` row's numbers beside
the singles numbers. State it once; it is their rule, not a lecture.

### 3. Recent form: search, cite, don't recall

Web search for what decides this bet, and cite every fact with its source:

- the player's or team's last ~5 games **on the stat the bet is about**, against
  the line (e.g. receiving yards per game vs 55.5)
- injury report / inactives / questionable tags for the player and key
  teammates, and the opponent's relevant absences
- anything obviously material: weather for an outdoor total, a backup QB,
  a rest spot

**Never state a stat, injury or result from memory.** Recalled sports facts
are stale by default. If a search comes back empty or contradictory, say that
rather than filling the gap.

Keep it to the stat and the availability news. This is not a game preview.

### 4. Price

- Implied probability of the ticket price (and of the boosted price, if any).
- For a boost: what it adds in profit on this stake.
- For an SGP with priced legs: the group price vs the product of the legs.
  The gap is the book's correlation adjustment; say which way it cuts.

### 5. Feedback

Short. This shape, no longer:

```
**Read:** <one line: the bet as understood; flagged fields marked (?)>

**Your history:** <1–3 lines from bet similar, n stated, flags respected>
**Recent form:** <1–3 lines, each with a source link>
**Price:** <one line>
**Watch-outs:** <policy breach / injury uncertainty / thin sample — only if real>

**Net:** <one sentence. What the evidence leans, and how strongly.>
```

The **Net** line may say the evidence leans for or against. It must not
guarantee an outcome or tell the user to raise a stake. When history and form
disagree, say they disagree.

### 6. If they place it

Offer once: the placed slip (with its BET ID) can go straight through
bet-capture.

## Boundaries

- **Writes nothing.** No `bet add`, no `bet settle`, no moving inbox files.
- **Does not place bets** and never suggests a stake size beyond noting what
  the slip says.
- **Arguments from the user's own data outrank recalled facts.** Lead with
  `bet similar`; every outside fact needs a live source.
- If `bet` is not on PATH, `uv run bet ...` from the repository works.
