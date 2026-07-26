import { neon } from "@neondatabase/serverless";

// Fallback URL is a well-formed but unreachable placeholder so `neon()` does
// not throw at module-import time when the env var is absent (e.g. during a
// build that runs before Neon is attached to the Vercel project). Actual
// queries against the placeholder will fail at request time — that's the
// intended signal that env is misconfigured. Pages using this module MUST
// opt into dynamic rendering so no query fires during prerender.
const PLACEHOLDER = "postgresql://build:build@127.0.0.1/build";

const READ_URL = process.env.DATABASE_URL ?? PLACEHOLDER;
const WRITE_URL =
  process.env.CONTROL_WRITE_URL ?? process.env.DATABASE_URL ?? PLACEHOLDER;

// Read connection — used by all pages / API routes for SELECT queries.
export const sqlRead = neon(READ_URL);

// Write connection — used ONLY by control-plane writes (kill switch etc.).
// If CONTROL_WRITE_URL is unset, fall back to DATABASE_URL; production
// should configure a distinct role with UPDATE on `control` only.
export const sqlWrite = neon(WRITE_URL);
