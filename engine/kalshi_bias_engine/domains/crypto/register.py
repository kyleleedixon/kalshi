"""Register the crypto domain into the DomainRegistry.

Imported (once) at engine startup — never from core code.
"""

from __future__ import annotations

from ...core.registry import DomainEntry, register_domain
from ...oracles.kraken_digital import KrakenDigitalOptionOracle
from .bias_features import RoundNumberDistanceFeature
from .mapper import CryptoMarketMapper


def install(oracle_ctx_provider) -> None:
    register_domain(DomainEntry(
        name="crypto",
        mapper=CryptoMarketMapper(),
        oracle_factory=lambda: KrakenDigitalOptionOracle(oracle_ctx_provider),
        bias_features=(RoundNumberDistanceFeature(),),
    ))
