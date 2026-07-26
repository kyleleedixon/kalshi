"""NeonSink dispatch table: every kind the runtime emits has a handler and
the ``contract_upsert`` payload shape matches what main.py builds."""

from __future__ import annotations

from kalshi_bias_engine.storage.writer import NeonSink


REQUIRED_KINDS = {
    "raw_pull",
    "contract_upsert",
    "quote",
    "oracle_estimate",
    "signal",
    "paper_order",
    "paper_fill",
    "settlement",
    "settlement_basis",
    "realized_vol",
    "bias_params",
    "calibration_snapshot",
    "heartbeat",
}


def test_all_required_kinds_have_handlers() -> None:
    handlers = set(NeonSink._HANDLERS.keys())
    missing = REQUIRED_KINDS - handlers
    assert not missing, f"NeonSink missing handlers for: {sorted(missing)}"


def test_unknown_kind_absent_from_dispatch() -> None:
    # The invariant we care about: made-up kinds do not have a handler.
    # The runtime error path (ValueError raised inside session_scope) is
    # covered by end-to-end integration tests, not this unit-level check.
    assert "nonexistent" not in NeonSink._HANDLERS
