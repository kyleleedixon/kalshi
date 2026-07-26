# kalshi-bias-engine dashboard

Next.js 16 App Router. Read-mostly view over the engine's Neon Postgres.
The **only** writes it performs are control-plane rows (kill switch); order
placement never routes through Vercel functions — that path lives in the
engine.

## Setup

1. Provision Neon via the Vercel–Neon integration on your Vercel project.
2. Copy `.env.example` to `.env.local` and fill:
   - `DATABASE_URL` — read role (SELECT on all engine tables).
   - `CONTROL_WRITE_URL` — separate role with UPDATE/INSERT on `control`
     only (falls back to `DATABASE_URL` if unset — do not do this in prod).
   - `KILL_SWITCH_TOKEN` — optional shared secret required on the toggle
     POST for defense-in-depth.
3. `npm install && npm run dev`.

## Pages

- `/` — overview: kill-switch state, recent signal/order counts, engine list.
- `/signals` — live edges from the last 10 minutes.
- `/calibration` — per-band per-domain reliability + gated flags.
- `/ledger` — paper orders and fills.
- `/heartbeat` — engine dead-man's-switch status.
- `/control` — toggle the kill switch.
