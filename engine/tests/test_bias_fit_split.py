from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from kalshi_bias_engine.bias.fit import Record, TimeSplit, fit_feature


def test_split_rejects_overlap() -> None:
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    with pytest.raises(ValueError):
        TimeSplit(
            in_sample_end=now + timedelta(days=1),
            oos_start=now,  # earlier than in_sample_end -> overlap
            oos_end=now + timedelta(days=2),
        )


def test_longshot_fit_returns_finite_and_records_evidence() -> None:
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    split = TimeSplit(
        in_sample_end=now + timedelta(days=1),
        oos_start=now + timedelta(days=1, seconds=1),
        oos_end=now + timedelta(days=2),
    )
    records = []
    # In-sample: y correlated with kalshi_price (retail favorite-longshot)
    for i in range(300):
        p = 0.05 + (i % 20) * 0.045   # spans [0.05, 0.9]
        y = 1 if (i % 3 == 0) else 0  # arbitrary but stable pattern
        records.append(Record(
            t_decision=now + timedelta(hours=i % 24),
            domain="crypto", kalshi_price=p, raw_p=p, y=y,
        ))
    # OOS: same pattern
    for i in range(200):
        p = 0.05 + (i % 20) * 0.045
        y = 1 if (i % 3 == 0) else 0
        records.append(Record(
            t_decision=now + timedelta(days=1, hours=i % 12),
            domain="crypto", kalshi_price=p, raw_p=p, y=y,
        ))
    r = fit_feature("longshot_curve", records, split, min_oos_sample=100,
                   min_brier_improvement=-1.0)  # very permissive gate
    assert isinstance(r.params["alpha_pool"], float)
    assert r.oos_sample >= 100
