# Engine state

Auto-published by the box (`ops/publish.py`). Do not edit by hand —
it is overwritten on every run. Cloud agents read this because they
cannot reach the server or the database directly.

Generated: 2026-09-23 19:30 UTC

## Trading

- **Status: HALTED**
- Halt reason: H-001 refuted 2026-09-16 at 576 contracts, EV -$0.0462 vs +$0.02 threshold; pre-committed kill rule
- Equity: $43.29 (+0.91 over 24h)
- Active slice gates: 1
- Loop cadence: 254-526s over last 5 cycles

## Hypothesis progress

Only fills carrying a strategy tag count. Verdicts are only valid
within one tag; see `research/REGISTRY.md` for the kill rules.

| tag | contracts | settled | PnL | EV/contract |
|---|---|---|---|---|
| `edge007_spread003_fresh2s_v1` | 109 | 36 | $+2.05 | $+0.0188 |
| `edge007_spread003_maker_v2` | 470 | 216 | $-29.01 | $-0.0617 |
| **pooled** | **579** | | **$-26.96** | **$-0.0466** |

## Monitors

- ok `order_rejection` — 0 rejected / 0 attempts (0%) in recent log
- ok `fill_drought` — halted on purpose - H-001 refuted 2026-09-16 at 576 contracts, E
- ok `gate_ratchet` — n/a while halted
- ok `horizon_ratchet` — all cutoffs <=900s
- **FIRING** `book_staleness` — median book age 31508 ms (target <2000)
- ok `pnl_breach` — 8 settlement-days in last 14d

## Notes for a reviewing agent

- You are READ-ONLY with respect to the engine. You cannot reach the
  server. Propose changes; do not assume any were applied.
- Detection and the pre-registered kill rule are already automated on
  the box. Do not recommend re-implementing them.
- Never treat a single number as fact without a second, independent
  source. A sign error once reported -$88.36 on an account that had
  moved $3.53, and nearly triggered a false shutdown.
- Early live results are near-worthless here: H-001 read +$0.046/ctr
  at 99 contracts and -$0.046 at 576. Do not call a trend before the
  registered sample size.
