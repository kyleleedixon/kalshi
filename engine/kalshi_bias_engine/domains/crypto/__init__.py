"""Crypto domain module.

Registers itself with the DomainRegistry on import. The engine's runtime
imports this module during startup; it must never be imported by anything
in ``core/``.
"""

from .mapper import CryptoMarketMapper
from .bias_features import RoundNumberDistanceFeature

__all__ = ["CryptoMarketMapper", "RoundNumberDistanceFeature"]
