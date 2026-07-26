"""Smoke tests: ensure core interfaces are importable without triggering the
live-execution import gate, and ensure the gate itself raises loudly."""

from __future__ import annotations

import pytest


def test_core_imports_do_not_touch_live() -> None:
    import kalshi_bias_engine.core  # noqa: F401
    import kalshi_bias_engine.bias  # noqa: F401
    import kalshi_bias_engine.signal  # noqa: F401
    import kalshi_bias_engine.ledger  # noqa: F401
    import kalshi_bias_engine.calibration  # noqa: F401


def test_live_execution_import_gate_raises() -> None:
    with pytest.raises(ImportError, match="import-gated"):
        import kalshi_bias_engine.execution.live  # noqa: F401


def test_prob_estimate_validates() -> None:
    from datetime import datetime, timezone

    from kalshi_bias_engine.core.estimate import ProbEstimate, StalenessReason

    ok = ProbEstimate(
        p=0.5, variance=0.01, effective_sample_size=100,
        data_timestamp=datetime.now(timezone.utc),
        staleness=StalenessReason.FRESH,
    )
    assert ok.is_fresh

    import pytest
    with pytest.raises(ValueError):
        ProbEstimate(p=1.5, variance=0.0, effective_sample_size=0,
                     data_timestamp=datetime.now(timezone.utc))
    with pytest.raises(ValueError):
        ProbEstimate(p=0.5, variance=-1.0, effective_sample_size=0,
                     data_timestamp=datetime.now(timezone.utc))


def test_calibration_gate_defaults_closed() -> None:
    from datetime import datetime, timezone

    from kalshi_bias_engine.core.calibration import CalibrationReport

    r = CalibrationReport(
        generated_at=datetime.now(timezone.utc),
        bands={}, phase_gate_min_sample=200,
    )
    # Unknown (domain, price) => gated (fail-closed).
    assert r.band_is_gated("crypto", 0.5) is True
