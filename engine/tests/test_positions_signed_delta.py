"""Position accumulator: sign convention for YES/NO OPEN/CLOSE."""

from __future__ import annotations

from kalshi_bias_engine.ledger.positions import _signed_delta


def test_yes_open_is_positive() -> None:
    assert _signed_delta("YES", "OPEN", 5) == 5


def test_yes_close_is_negative() -> None:
    assert _signed_delta("YES", "CLOSE", 5) == -5


def test_no_open_is_negative_yes() -> None:
    """Buying NO is short-YES; stored as negative YES-equivalent so the
    symmetric per-market risk limits compare uniformly."""
    assert _signed_delta("NO", "OPEN", 5) == -5


def test_no_close_is_positive_yes() -> None:
    assert _signed_delta("NO", "CLOSE", 5) == 5


def test_case_insensitive() -> None:
    assert _signed_delta("yes", "open", 3) == 3
    assert _signed_delta("no", "close", 3) == 3
