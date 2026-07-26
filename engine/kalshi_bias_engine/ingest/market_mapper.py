"""MarketMapper base — translates raw Kalshi market metadata into internal
Contract objects.

Each domain owns one MarketMapper. The engine's discovery step walks all
registered mappers per raw market and takes the first that ``matches``. If
none matches, the market is skipped (logged, not raised): unknown Kalshi
categories should be silently ignorable, not fatal.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from ..core.contract import Contract


class MarketMapperBase(ABC):
    domain: str = "abstract"

    @abstractmethod
    def matches(self, raw_market: dict[str, Any]) -> bool: ...

    @abstractmethod
    def to_contract(self, raw_market: dict[str, Any]) -> Contract: ...
