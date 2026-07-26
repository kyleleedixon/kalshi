"""LIVE EXECUTION MODULE — INTENTIONALLY UNAVAILABLE (Phase 1).

This package physically raises ImportError on import. That is the point.
Phase 1 is paper only, and this gate makes accidental imports of the
live order module fail LOUDLY — including from tests, notebooks, and any
future code that forgets the phase check.

To enable Phase 2:

  1. Prove each domain's calibration passes its phase gate. See
     ``calibration.phase_gate.PhaseGate.evaluate`` — a domain unlocks
     when the calibration STORE says it should, not when a config flag
     is flipped.
  2. Add the live client implementation under this package
     (``kalshi_order_client.py`` etc.).
  3. Remove the raise below and replace it with the real exports.
  4. Wire the runtime to construct LiveExecutionPolicy ONLY when the
     phase gate for that domain returns ``unlocked=True``. The gate is
     the sole authority.

Do not defeat this gate by importing individual submodules or by wrapping
the import in a try/except. If you find yourself wanting to, the answer
is either "you're in the wrong phase" or "add the phase check where you
actually needed it."
"""

raise ImportError(
    "kalshi_bias_engine.execution.live is import-gated (Phase 1: paper only). "
    "See the module docstring for how to enable Phase 2 — the gate lives in "
    "the calibration store, not in code flags."
)
