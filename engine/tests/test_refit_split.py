"""Refit-loop split picker: sanity checks on the time cutoff."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from kalshi_bias_engine.runtime.refit_loop import _pick_split


def _row(ts: datetime) -> dict:
    return {"t_decision": ts}


def test_returns_none_for_empty() -> None:
    assert _pick_split([], 0.7) is None


def test_returns_none_for_single_row() -> None:
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert _pick_split([_row(now)], 0.7) is None


def test_returns_none_when_span_is_zero() -> None:
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert _pick_split([_row(now), _row(now)], 0.7) is None


def test_split_at_seventy_percent() -> None:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    end = start + timedelta(days=10)
    rows = [_row(start), _row(start + timedelta(days=5)), _row(end)]
    split = _pick_split(rows, 0.7)
    assert split is not None
    assert split.in_sample_end == start + timedelta(days=7)
    assert split.oos_end == end
