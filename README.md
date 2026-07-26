# kalshi-bias-engine

Automated trading system exploiting behavioral mispricing on Kalshi by comparing
quoted contract prices against an independently-modeled fair value, executing
entries and exits when calibrated edge clears fees and risk checks.

V1 trades crypto contracts only. Architecture is domain-agnostic from day one —
sports, cross-venue event arb, and other categories get added later as plug-in
modules, not rewrites.

## Layout

- `engine/` — Python trading engine (long-running process, Docker linux/arm64).
- `dashboard/` — Next.js dashboard on Vercel, reads Neon Postgres.
- `ops/` — deploy notes and infra scripts.

## Topology

- **Engine** — Phase 1: local. Phase 2: Oracle Cloud Always Free ARM VM.
- **Neon Postgres** — source of truth AND control plane (kill-switch, phase
  gates, heartbeat). Vercel-Neon integration.
- **Dashboard** — read-mostly, only writes control-plane rows. Never touches
  the Kalshi order API.

## Build phase

Phase 1 (paper). The execution/live module is import-gated and intentionally
inert. Order placement unlocks per-domain when out-of-sample calibration passes
the phase-gate checks in the calibration store — not by flipping a config flag.

## Engine runtime loops

The engine process runs the following concurrently. All writes flow through
the local SQLite spool, so Neon transience never stalls the trading loop.

- **Trading loop** (`_discover_and_trade_once`) — every `loop_interval_sec`
  discovers open Kalshi markets, upserts contracts, snapshots quotes,
  generates signals via oracle + bias model, and asks the
  `PaperExecutionPolicy` to decide. On a FILLED paper open an
  `attached_fill` piggybacks on the `paper_order` write so order + fill
  land in the same Neon transaction.
- **Kraken ingest** (WebSocket) — streams trades into the multi-horizon
  realized-vol store the oracle reads.
- **Settlement ingest** (`settlement_poll_interval_sec`, default 60s) —
  finds contracts past `settlement_time` without a settlements row and
  spools `settlement` writes from Kalshi's per-market endpoint.
- **Refit loop** (`calibration_refit_interval_sec`, default 15m) — pulls
  settled records, rebuilds the per-band per-domain calibration report,
  refits each bias feature with a 70/30 time-based in-sample/OOS split,
  spools `calibration_snapshot` + `bias_params`, and hot-loads the fresh
  params into the in-memory bias model. `evidence_ok` (min OOS sample AND
  min Brier improvement) is what actually toggles a feature on at runtime.
- **Heartbeat** (`heartbeat_interval_sec`) — dashboard reads staleness
  for the dead-man's-switch.
