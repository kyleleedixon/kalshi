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
