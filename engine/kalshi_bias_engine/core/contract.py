"""Domain-agnostic contract representation.

A Contract is the engine's internal view of *any* tradable binary event —
crypto threshold, sports moneyline, cross-venue proposition. Domain-specific
attributes live in ``features``, which the domain module writes and the
BiasModel's registered features read.

The Kalshi-specific identifiers (ticker, event ticker, market ticker) live
here because Kalshi is the venue we execute on. If we later add another
venue for execution, this becomes VenueContract and Contract becomes the
underlying-event abstraction. For V1 the collapse is fine.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class ContractSide(str, Enum):
    """Which side of the Kalshi binary we are pricing/quoting.

    Kalshi lists YES and NO as independently quoted contracts on the same
    underlying event. Our oracle emits P(YES); NO probability is (1 - p).
    """

    YES = "YES"
    NO = "NO"


class SettlementSource(str, Enum):
    """How the contract settles. Feeds oracle confidence:
    Kraken spot is NOT the CF Benchmarks composite that Kalshi settles on,
    and the oracle must widen its CI to account for the basis."""

    CF_BENCHMARKS_RTI = "CF_BENCHMARKS_RTI"
    SPORTS_OFFICIAL = "SPORTS_OFFICIAL"
    OTHER_VENUE = "OTHER_VENUE"
    UNSPECIFIED = "UNSPECIFIED"


@dataclass(frozen=True)
class Contract:
    """Internal, domain-agnostic contract.

    Attributes
    ----------
    contract_id
        Stable internal id (typically the Kalshi market ticker).
    domain
        Domain module name registered in DomainRegistry. Selects the
        MarketMapper, Oracle, and domain-specific bias features.
    underlying
        Domain-defined symbol (``BTC``, ``NFL:KC@BUF``, ...). Opaque to core.
    side
        YES or NO. Oracle emits P(YES); ExecutionPolicy negates for NO.
    open_time
        First time the market accepts orders. None if unknown.
    close_time
        Trading close (may precede settlement).
    settlement_time
        When the outcome is determined.
    settlement_source
        Feeds oracle-confidence adjustments.
    features
        Domain-specific opaque payload. Crypto puts ``strike``, ``direction``,
        ``horizon_seconds`` here. Sports puts team ids, spread, etc. Core code
        MUST NOT introspect this dict; domain features do.
    """

    contract_id: str
    domain: str
    underlying: str
    side: ContractSide
    open_time: datetime | None
    close_time: datetime | None
    settlement_time: datetime | None
    settlement_source: SettlementSource
    features: dict[str, Any] = field(default_factory=dict)

    def feature(self, key: str, default: Any = None) -> Any:
        return self.features.get(key, default)
