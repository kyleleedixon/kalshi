from __future__ import annotations

import math

from kalshi_bias_engine.ingest.realized_vol import RollingVol


def test_realized_vol_tracks_constant_sigma() -> None:
    # Simulate 1000 log-returns at fixed sigma_per_sec, 1s spacing.
    rv = RollingVol(underlying="BTC", horizon_seconds=1_000)
    sigma_per_sec = 1e-4
    price = 100.0
    ts = 0.0
    rng = _seeded_rng()
    for _ in range(1000):
        r = sigma_per_sec * next(rng)
        price *= math.exp(r)
        ts += 1.0
        pt = rv.update(ts, price)
    # Annualized sigma ~ sigma_per_sec * sqrt(seconds_per_year).
    expected = sigma_per_sec * math.sqrt(365.25 * 86_400.0)
    # Loose bound: within 30% of expected under 1k samples.
    assert 0.7 * expected < pt.sigma_annualized < 1.3 * expected
    assert pt.tick_count > 500


def _seeded_rng():
    import random
    r = random.Random(42)
    while True:
        yield r.gauss(0.0, 1.0)


def test_short_window_relative_to_horizon_returns_zero_sigma() -> None:
    # 60 seconds of ticks projected to a 24h horizon must NOT annualize
    # into a wild sigma — before the guard a single 3% tick inflated
    # sigma_annualized to >200.
    rv = RollingVol(underlying="BTC", horizon_seconds=86_400)
    price = 50_000.0
    for i in range(60):
        rv.update(float(i), price * (1.0005 if i == 30 else 1.0))
    pt = rv.snapshot()
    assert pt is not None
    assert pt.tick_count == 60
    assert pt.sigma_annualized == 0.0  # marked stale by insufficient window


def test_snapshot_is_side_effect_free() -> None:
    rv = RollingVol(underlying="BTC", horizon_seconds=1_000)
    for i in range(10):
        rv.update(float(i), 100.0 + i * 0.01)
    before = len(rv._points)  # noqa: SLF001
    rv.snapshot()
    rv.snapshot()
    rv.snapshot()
    assert len(rv._points) == before  # noqa: SLF001
