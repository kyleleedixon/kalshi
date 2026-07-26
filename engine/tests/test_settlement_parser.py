"""Settlement parser tolerates Kalshi field-name drift."""

from __future__ import annotations

from kalshi_bias_engine.runtime.settlement_loop import _parse_settled


def test_returns_none_when_not_settled() -> None:
    assert _parse_settled({"market": {"status": "open"}}) is None


def test_extracts_yes_and_value() -> None:
    parsed = _parse_settled({
        "market": {
            "status": "settled",
            "result": "yes",
            "settlement_value": "60250.5",
            "settled_time": "2026-07-26T12:00:00Z",
        }
    })
    assert parsed is not None
    outcome, value, settled_at = parsed
    assert outcome == "YES"
    assert value == 60250.5
    assert settled_at is not None


def test_extracts_no_without_value() -> None:
    parsed = _parse_settled({"market": {
        "status": "finalized", "settled_result": "NO",
    }})
    assert parsed is not None
    outcome, value, _ = parsed
    assert outcome == "NO"
    assert value is None


def test_unwrapped_market_dict_works() -> None:
    parsed = _parse_settled({"status": "settled", "result": "yes"})
    assert parsed is not None
    assert parsed[0] == "YES"


def test_unknown_outcome_returns_none() -> None:
    assert _parse_settled({"market": {"status": "settled",
                                       "result": "maybe"}}) is None
