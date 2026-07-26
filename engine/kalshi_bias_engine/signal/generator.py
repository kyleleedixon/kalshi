"""Signal generation.

Computes bias-adjusted probability, subtracts Kalshi's implied probability
plus fees plus expected crossing cost from the live book, ranks candidates
by fee-adjusted edge weighted by calibration confidence, and hands off to
the PaperExecutionPolicy.

Every emitted signal logs full provenance to Neon (oracle inputs, per-
feature adjustments, fees). Every live decision must be reconstructable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable

from ..core.bias import BiasAdjustment, BiasModel
from ..core.calibration import CalibrationReport
from ..core.contract import Contract
from ..core.estimate import ProbEstimate
from ..core.oracle import Oracle
from .fees import KalshiFeeSchedule


@dataclass
class BookSnapshot:
    """Top-of-book for a Kalshi contract at decision time.

    Prices are in dollars in [0, 1].
    """

    bid: float | None
    ask: float | None
    bid_size: int | None
    ask_size: int | None
    data_ts: datetime


@dataclass
class GeneratedSignal:
    contract: Contract
    raw: ProbEstimate
    adjusted_p: float
    adjustments: list[BiasAdjustment]
    book: BookSnapshot
    fee_per_contract: float
    edge_net: float
    calibration_confidence: float
    rank: int | None = None
    provenance: dict = field(default_factory=dict)


class SignalGenerator:
    def __init__(self, oracle: Oracle, bias_model: BiasModel, fees: KalshiFeeSchedule) -> None:
        self._oracle = oracle
        self._bias = bias_model
        self._fees = fees

    async def generate(
        self,
        contracts_with_books: Iterable[tuple[Contract, BookSnapshot]],
        calibration: CalibrationReport,
    ) -> list[GeneratedSignal]:
        signals: list[GeneratedSignal] = []
        for contract, book in contracts_with_books:
            if not self._oracle.supports(contract):
                continue
            raw = await self._oracle.estimate(contract)
            if not raw.is_fresh:
                # Stale-oracle contracts still emit a "signal" record with
                # zero edge so ledger provenance is complete, but they are
                # not passed to sizing.
                mid = _mid(book)
                signals.append(GeneratedSignal(
                    contract=contract, raw=raw,
                    adjusted_p=raw.p, adjustments=[],
                    book=book, fee_per_contract=0.0,
                    edge_net=0.0, calibration_confidence=0.0,
                    provenance={"skipped": "stale_oracle",
                                "staleness": raw.staleness.value},
                ))
                continue

            mid = _mid(book)
            if mid is None:
                continue

            adjusted_p, adjustments = self._bias.adjust(contract, raw, mid)

            # Crossing cost: if we open by buying YES at ask, our effective
            # entry price is ``ask`` not mid. If we buy NO, entry is
            # (1 - bid). Compute edge for the side we would actually take.
            edge_yes_buy = 0.0
            edge_no_buy = 0.0
            fee_yes = self._fees.per_contract(book.ask) if book.ask is not None else 0.0
            fee_no = self._fees.per_contract(1.0 - book.bid) if book.bid is not None else 0.0
            if book.ask is not None:
                edge_yes_buy = adjusted_p - book.ask - fee_yes
            if book.bid is not None:
                edge_no_buy = (1.0 - adjusted_p) - (1.0 - book.bid) - fee_no

            if edge_yes_buy >= edge_no_buy:
                side_edge = edge_yes_buy
                side_fee = fee_yes
                take_side = "YES"
            else:
                side_edge = edge_no_buy
                side_fee = fee_no
                take_side = "NO"

            band = calibration.stats_for(contract.domain, mid)
            gated = calibration.band_is_gated(contract.domain, mid)
            calibration_confidence = 0.0 if gated or band is None else _confidence_from_band(band)

            signals.append(GeneratedSignal(
                contract=contract, raw=raw,
                adjusted_p=adjusted_p, adjustments=adjustments,
                book=book, fee_per_contract=side_fee,
                edge_net=side_edge, calibration_confidence=calibration_confidence,
                provenance={"take_side": take_side, "band_gated": gated,
                            "mid": mid, "generated_at":
                                datetime.now(timezone.utc).isoformat()},
            ))

        # Rank by fee-adjusted edge weighted by calibration confidence.
        signals.sort(key=lambda s: s.edge_net * s.calibration_confidence, reverse=True)
        for i, s in enumerate(signals):
            s.rank = i
        return signals


def _mid(book: BookSnapshot) -> float | None:
    if book.bid is None or book.ask is None:
        return None
    return 0.5 * (book.bid + book.ask)


def _confidence_from_band(band) -> float:
    """Confidence in [0, 1] from BandStats.

    Uses Brier improvement over uncorrected oracle as the signal, clipped
    to [0, 1]. A band with no measurable improvement gets 0 (feature is
    effectively off for that band).
    """
    imp = -band.brier_vs_uncorrected  # negative brier_vs_uncorrected == improvement
    if imp <= 0:
        return 0.0
    # Squash: gentle sigmoid so a small consistent improvement doesn't blow
    # size up. 0.02 improvement -> ~0.5 confidence.
    import math
    return 1.0 / (1.0 + math.exp(-100.0 * (imp - 0.02)))
