"""Position sizing + risk limits.

Sizing is fractional-Kelly on CALIBRATED edge, not theoretical edge, and is
further scaled by per-band calibration confidence. A domain with insufficient
settled sample gets zero size regardless of apparent edge — see
CalibrationReport.band_is_gated.

Kelly for a binary at price ``q`` with true probability ``p``:
    f = (p - q) / (1 - q)   # fraction of bankroll to bet on YES
We cap to ``kelly_fraction`` (fractional Kelly) and to hard contract caps.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class RiskLimits:
    bankroll_dollars: float = 1_000.0
    kelly_fraction: float = 0.25        # fractional Kelly multiplier
    max_contracts_per_market: int = 100
    max_contracts_per_underlying: int = 500
    max_contracts_per_domain: int = 2_000
    max_aggregate_contracts: int = 5_000
    daily_loss_limit_dollars: float = 100.0


def kelly_size_contracts(
    *,
    adjusted_p: float,
    take_price: float,      # dollars we pay per contract for the side we take
    limits: RiskLimits,
    calibration_confidence: float,
    current_market_position: int,
    current_underlying_position: int,
    current_domain_position: int,
    current_aggregate_position: int,
) -> int:
    if take_price <= 0.0 or take_price >= 1.0:
        return 0
    if calibration_confidence <= 0:
        return 0

    f_kelly = (adjusted_p - take_price) / (1.0 - take_price)
    if f_kelly <= 0:
        return 0

    f_capped = min(f_kelly, 1.0) * limits.kelly_fraction * calibration_confidence
    dollars = f_capped * limits.bankroll_dollars
    contracts = int(dollars / take_price)

    contracts = min(
        contracts,
        limits.max_contracts_per_market - current_market_position,
        limits.max_contracts_per_underlying - current_underlying_position,
        limits.max_contracts_per_domain - current_domain_position,
        limits.max_aggregate_contracts - current_aggregate_position,
    )
    return max(contracts, 0)
