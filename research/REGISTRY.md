# Hypothesis registry

Every hypothesis is written here **before** its evaluation window opens, and
is judged only on data that arrives after its `registered` timestamp. Nothing
is added retroactively, and no entry is edited after registration except to
append a verdict.

This exists because in-sample search on this dataset has repeatedly produced
results that did not replicate: day-level t-statistics of **4.02** and **4.77**
both collapsed to ≈**-0.05** out of sample, and two separate analyses
(July backfill, first September pass) produced 96%- and 99%-win-rate findings
that were scoring against empty order books. Pre-registration is the only
cheap defence.

## Rules

1. **Register before the window opens.** An entry with no future data is not
   a hypothesis, it is a description.
2. **Record the search size `N`** — how many variants were considered before
   this one was written down. The expected best t-stat under pure noise is
   about `sqrt(2*ln(N))`: N=30 → 2.6, N=10,000 → 4.3. A result that does not
   clear the bar for its own search size is noise.
3. **Fix the kill rule in advance.** If you decide when to quit after seeing
   results, you will not quit.
4. **One strategy live at a time.** Verification takes ~500-750 fills; the
   bankroll cannot fund parallel verification, and overlapping positions
   destroy attribution.
5. **Backtests falsify, never confirm.** A surviving backtest promotes a
   hypothesis to paper-forward or small-size live. It never establishes it.

## Status key

`REGISTERED` → `EVALUATING` → `REFUTED` | `SURVIVED` | `INCONCLUSIVE`

---

## H-001 — high-conviction tail with cost control

- **registered:** 2026-09-11T23:00Z
- **strategy tag:** `edge007_spread003_fresh2s_v1` (+ `_maker_v2`, pooled)
- **status:** **REFUTED 2026-09-16**
- **search size N:** ~30 hypotheses (2026-09-04 study)

### VERDICT — REFUTED

Pooled across both tags (legitimate: the maker arm filled **0** contracts, so
every realized fill under either tag was a taker fill of the same entry rules):

| | contracts | settled | PnL | EV/contract |
|---|---|---|---|---|
| v1 | 109 | 36 | +$2.05 | +$0.0188 |
| v2 | 467 | 215 | -$28.65 | -$0.0613 |
| **pooled** | **576** | **249** | **-$26.60** | **-$0.0462** |

Kill rule was: *after 500 contracts, EV < +$0.02 -> REFUTED*. At 576 contracts
EV is **-$0.0462** — not merely short of the bar, but negative. The rule fires.

**The early read was small-sample noise.** At 99 contracts this showed
+$0.046 and looked like it was tracking the backtest's +$0.065. It decayed to
+$0.0188 by 109 contracts and went negative as the sample grew. This is the
same decay pattern as the in-sample t-stats of 4.02 and 4.77 that became
~-0.05 — and the exact reason the kill rule was fixed in advance.

**The backtest was wrong by ~11 cents/contract** (+$0.065 predicted vs
-$0.046 realized). Adverse selection is ruled out as the cause (measured at
~0, see [[project-adverse-selection-measured]]). Remaining candidates: the
idealized fill assumption, the ~$0.02 crossing cost, and selection effects in
which contracts actually fill.

**Claim.** Entries restricted to post-fee model edge ≥ $0.07, spread ≤ 3c,
and a book re-fetched within 2s of the decision earn positive realized EV
per contract.

**Origin.** 2026-09-04 study over 19 usable days / 10,504 tradeable records.
Measured +$0.065/contract at edge ≥ 0.10 (day-level t=3.12, 78.9% positive
days). Survived jackknife (dropping the two best days: +$0.050, t=2.75),
walk-forward thirds (+0.049 / +0.058 / +0.096), and showed monotone
dose-response across nine nested thresholds — which is the main reason it is
being traded rather than discarded.

**Prediction.** Realized EV ≥ +$0.02/contract over ≥ 500 fills carrying this
tag, with the daily PnL series not dominated by a single day.

**Kill rule (fixed 2026-09-11, before results):**
- After **500 tagged fills**, if EV/contract < **+$0.02** → REFUTED, turn off.
- If at any point cumulative tagged PnL < **-$40** → REFUTED early, turn off.
- If EV ≥ +$0.02 but dropping the best day takes it below **+$0.01** →
  INCONCLUSIVE, keep running, do not scale size.
- Only fills with tag `edge007_spread003_fresh2s_v1` count. Any change to an
  entry rule bumps the tag and resets the count to zero.

**Clean window starts 2026-09-11T23:00Z.** All earlier live evidence is
discarded for this test: between 09-06 and 09-11 the loop ran 130-1412s
cycles against books up to 221s stale, so the -$25.99/232-contract result on
BTC/threshold/15-60m measures a broken engine, not this hypothesis.

**How it could still be an artifact.** The backtest assumed fills at the
touch, in full, every time. It cannot see queue position, market impact, or
adverse selection — and adverse selection alone could consume the entire
6.5c edge, since you are filled precisely when someone better informed wants
the other side.

---

## H-003 — maker-first execution (taker vs maker, same entry rules)

- **registered:** 2026-09-12T14:45Z
- **strategy tag:** `edge007_spread003_maker_v2`
- **status:** **REFUTED 2026-09-14** (disabled same day)
- **search size N:** 1 (mechanism-first, not selected from a search)

### VERDICT — REFUTED on the fill-rate guard

| path | orders | contracts requested | filled | fill rate |
|---|---|---|---|---|
| maker | 12 | 24 | **0** | **0%** |
| taker | 261 | 398 | 254 | 64% |

Zero fills on 24 maker contracts. Against a 64% taker rate that is not
variance. Posting 1c inside the spread is not hit within the 10s window in
these markets, and `compute_maker_limit` returns None on 1c spreads — which
are ~50% of candidates — so the mechanism only ever applied to ~4% of orders
(12 of 273).

Cost was real: the loop degraded from 33s to 53-80s and median book age went
2.8s -> 26.8s. Disabled 2026-09-14 03:03 UTC; loop recovered to 34s.

The $0.0197/contract crossing cost is therefore **still unrecovered**, and
maker-first at this offset is not the way to get it.

**Claim.** Posting inside the spread and crossing only if unfilled recovers
a meaningful share of the $0.0197/contract crossing cost, without giving it
back to adverse selection.

**Origin.** Markout on 1,310 of our own OPEN fills (2026-09-12). Raw markout
was -$0.019 at 60s (t=-14.87), but decomposing against the entry mid shows
that is almost entirely the mechanical cost of crossing: true markout vs
entry mid is +0.0055 at 30s, -0.0022 at 60s, +0.0007 at 300s — adverse
selection is indistinguishable from zero. So resting an order should not be
punished by informed flow, and the $0.0197 is recoverable.

That cost is ~43% of H-001's measured +$0.046/contract, which makes it a
larger lever than any model change discussed so far.

**Design — this is an A/B, not a replacement.** Entry rules are unchanged
from H-001 (edge >= 0.07, spread <= 3c, book < 2s). The ONLY difference is
execution. H-001's 99 contracts at **+$0.046/contract** stand as the taker
baseline arm; H-003 is the maker arm under the same conditions.

**Prediction.** EV/contract for H-003 exceeds H-001's +$0.046 over >= 300
tagged contracts. Mechanism says up to +$0.02 of the crossing cost is
recoverable, so the target is roughly +$0.05 to +$0.066.

**Kill rule (fixed 2026-09-12, before results):**
- After **300 tagged contracts**, if EV/contract < **+$0.02** -> REFUTED,
  revert to taker.
- If EV/contract < H-001's **+$0.046** at 300 contracts -> maker-first is
  not helping; revert and keep taker.
- If cumulative tagged PnL < **-$40** -> REFUTED early.
- **Fill-rate guard:** if tagged fills per day drop below ~50% of H-001's
  rate, maker-first is winning the cheap trades and missing the ones that
  matter. That is a refutation even if EV looks fine, because the edge
  measured on a biased subset does not generalise.

**How it could still be an artifact.** Resting orders fill selectively: you
get filled when the market comes to you, which is disproportionately when
you are wrong. The markout evidence argues against that here, but it was
measured on *taker* fills and may not transfer to resting ones. The
fill-rate guard is the tripwire for it.

---

## H-002 — cross-strike no-arbitrage violations

- **evaluated:** 2026-09-19 (agent scan, full writeup in `findings/H-002.md`)
- **status:** **REFUTED** — monotonicity leg only; two identities remain open
- **search size N:** 1 (mechanism-first)

### VERDICT — REFUTED

Corpus: 42 days (08-05 → 09-15), 18.3M threshold observations, 659k instants
with >=2 strikes quoted. Tested `ask(X1) < bid(X2)` for `X1 < X2`.

**Killed by arithmetic, not by sample size.** The Kalshi fee has a 1c-per-leg
MINIMUM, so a round trip costs >=2c against a 1c tick. Any real opportunity
must therefore be >=3c gross — a dislocation big enough to be visible even at
32s sampling. Observed gross is median $0.01 (p90 $0.01); net median is
**-$0.01**. Only 5 of 363 book-clean violations net positive, 3 of those at
exactly $0.00.

The entire population clearing fees *with size on both legs* is **21
instances worth $0.79 across 42 days**. Twenty of the 21 have a stale leg
(the three largest sit on 26-second-old books). The single instance with both
legs clean and fresh nets **exactly $0.00**.

**All three artifact mechanisms are present:**
- *Stale leg* — 73.0% of raw candidates (980 of 1,343) discarded on book
  freshness alone.
- *Size unavailable* — median min-leg size is **0**; 80% of survivors have a
  zero-size leg. `ask_size == 0` on the buy leg appears at 42.7% vs a 1.6%
  base rate, a **26x enrichment**. The "arbitrage" is buying where nothing is
  offered.
- *Mis-joined event* — the largest violation in the corpus (89% of raw daily
  EV on 09-12) is one scan cycle containing two interleaved, mutually
  inconsistent ETH strike ladders under a single event ticker, bids grossly
  non-monotone (0.20 at 2419.99 vs 0.91 at 2429.99). Not a market
  dislocation; a join error.

**Persistence argues AGAINST, not for.** 88.8% of violations survive to t+1
(median 33s) and 79.8% to t+4. A genuine riskless arb sitting untouched for
two minutes on a live exchange is not credible — persistence is the signature
of a quote nobody can trade against. Of 356 persisting violations, exactly
**1** still cleared costs.

**Robustness:** day-level jackknife on the tradeable series runs
$0.79 → $0.29 → $0.19 → $0.12 dropping the best 1/2/3 days. The t-stat *rises*
under jackknife (1.55 → 2.18), which the agent correctly flagged as an
artifact: only 8 of 42 days are nonzero, so dropping the large day shrinks
variance faster than the mean.

**Stated limit:** the archive is a *signal log, not a book tape* — a strike
appears only when the engine emitted for it (~32-35s), so sub-30s violations
are structurally invisible. Settling that needs a real websocket tape. The
fee-floor arithmetic is what makes the verdict robust to this anyway.

### STILL OPEN (not tested)

The registry's other two identities were **not** evaluated:
- non-overlapping brackets over the same range summing to ~1
- `P(above X) - P(above Y) = P(bracket X-Y)`

Both involve `bracket` contracts, which the monotonicity scan excluded. Note
the same 2c fee floor applies, and brackets trade wider — so the bar for a
tradeable dislocation is higher, not lower.

**Claim.** Kalshi prices thresholds and brackets on the same underlying and
expiry that violate arithmetic identities — `P(above X)` monotone decreasing
in `X`; non-overlapping brackets summing to ~1;
`P(above X) - P(above Y) = P(bracket X-Y)` — by more than fees plus spread.

**Why it is worth a slot.** It requires no forecast. The edge does not depend
on the volatility model being right, so it is orthogonal to H-001 and fails
for entirely different reasons. Mechanism-first, not data-mined.

**Before registering:** measure how often violations exceed cost, and how
long they persist. A violation that closes in under a second is not tradeable
at this stack's latency.
