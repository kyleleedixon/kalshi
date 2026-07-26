"""Domain registry.

Each domain module (crypto, sports, cross-venue) registers:
  * its MarketMapper (Kalshi metadata -> internal Contract)
  * its default Oracle
  * any domain-specific BiasFeatures

Core code looks domains up by name only. Nothing in core imports a domain
module directly, and domain modules never import each other.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Protocol

from .oracle import Oracle
from .bias import BiasFeature


class MarketMapper(Protocol):
    """Translates raw Kalshi market metadata into internal Contract objects.

    Kept structural (Protocol) so domain modules don't need to import a
    base class from core. Implementations live under their domain module.
    """

    def domain(self) -> str: ...
    def matches(self, raw_market: dict) -> bool: ...
    def to_contract(self, raw_market: dict) -> "object": ...  # returns Contract


@dataclass
class DomainEntry:
    name: str
    mapper: MarketMapper
    oracle_factory: Callable[[], Oracle]
    bias_features: tuple[BiasFeature, ...] = field(default_factory=tuple)


class DomainRegistry:
    _entries: dict[str, DomainEntry] = {}

    @classmethod
    def register(cls, entry: DomainEntry) -> None:
        if entry.name in cls._entries:
            raise ValueError(f"domain already registered: {entry.name}")
        cls._entries[entry.name] = entry

    @classmethod
    def get(cls, name: str) -> DomainEntry:
        try:
            return cls._entries[name]
        except KeyError as e:
            raise KeyError(f"unknown domain: {name}") from e

    @classmethod
    def names(cls) -> tuple[str, ...]:
        return tuple(cls._entries)


def register_domain(entry: DomainEntry) -> None:
    DomainRegistry.register(entry)


def get_domain(name: str) -> DomainEntry:
    return DomainRegistry.get(name)
