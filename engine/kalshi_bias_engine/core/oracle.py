"""Oracle interface.

An Oracle produces an independent probability estimate for a Kalshi contract.
The interface intentionally speaks only in probabilities + confidence — not
spot + vol — so that future oracles that source probability directly (de-vigged
sportsbook lines, another prediction-market venue) plug in as new files, not
as a refactor.

Oracles self-report their reliability. If the oracle cannot produce a usable
estimate, it MUST emit a ProbEstimate with a non-FRESH staleness reason
rather than raising or returning None. Callers should never guess why.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from .contract import Contract
from .estimate import ProbEstimate


class Oracle(ABC):
    """Base for any probability source.

    Implementations:
      * KrakenDigitalOptionOracle (V1) — prices threshold/up-down crypto
        contracts as digital options off Kraken spot + realized vol.
      * (later) SportsbookNoVigOracle — de-vigs sportsbook lines directly.
      * (later) CrossVenueOracle — reads another prediction-market venue's
        mid on the same event.
    """

    #: Unique name; recorded in ledger provenance so we can attribute edge
    #: to a specific oracle version.
    name: str = "abstract"
    version: str = "0"

    @abstractmethod
    def supports(self, contract: Contract) -> bool:
        """True if this oracle can price ``contract`` at all.

        Implementations should be cheap and side-effect-free — this is called
        during signal generation to filter candidates before ``estimate``.
        """

    @abstractmethod
    async def estimate(self, contract: Contract) -> ProbEstimate:
        """Return a probability estimate WITH confidence for ``contract``.

        MUST NOT raise for expected data-quality problems. Return a
        ProbEstimate with an appropriate ``StalenessReason`` instead.
        The ExecutionPolicy relies on this contract to distinguish
        "I don't know" from "I errored."
        """
