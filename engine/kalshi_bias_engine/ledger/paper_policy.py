"""PaperExecutionPolicy — Phase 1 hypothetical fills against the real book.

Implements the ExecutionPolicy contract exactly as LiveExecutionPolicy will:
same decision surface, same guards. The only difference is where the fill
comes from — here we simulate against the top of book with a conservative
slippage model, and we write to ``paper_orders`` / ``paper_fills`` instead
of hitting the Kalshi order API.

Kill-switch, staleness, and calibration-gating semantics are enforced here
so switching to LiveExecutionPolicy in Phase 2 does not change behavior.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..core.bias import BiasAdjustment
from ..core.calibration import CalibrationReport
from ..core.contract import Contract
from ..core.estimate import ProbEstimate
from ..core.execution import Action, ActionType, ExecutionPolicy
from .risk import RiskLimits, kelly_size_contracts


@dataclass
class PositionSnapshot:
    per_market: dict[str, int] = field(default_factory=dict)
    per_underlying: dict[str, int] = field(default_factory=dict)
    per_domain: dict[str, int] = field(default_factory=dict)
    aggregate: int = 0

    def get_market(self, contract_id: str) -> int:
        return self.per_market.get(contract_id, 0)


class PaperExecutionPolicy(ExecutionPolicy):
    def __init__(self, limits: RiskLimits, positions_provider) -> None:
        """``positions_provider`` returns a fresh PositionSnapshot per call
        so the policy can inspect live paper-book state at decide time."""
        self._limits = limits
        self._positions = positions_provider

    def decide(
        self,
        *,
        contract: Contract,
        raw: ProbEstimate,
        adjusted_p: float,
        adjustments: list[BiasAdjustment],
        kalshi_bid: float,
        kalshi_ask: float,
        current_position: int,
        calibration: CalibrationReport,
        kill_switch_active: bool,
    ) -> Action:
        prov: dict = {
            "adjusted_p": adjusted_p,
            "bid": kalshi_bid,
            "ask": kalshi_ask,
            "position": current_position,
            "adjustments": [
                {
                    "name": a.feature_name, "delta": a.delta,
                    "evidence_ok": a.evidence_ok, "params": a.params_snapshot,
                }
                for a in adjustments
            ],
        }

        # Stale oracle: EXIT existing, REJECT new.
        if not raw.is_fresh:
            prov["reason"] = f"stale:{raw.staleness.value}"
            if current_position != 0:
                return Action(
                    type=ActionType.CLOSE, contract=contract,
                    size_contracts=abs(current_position),
                    limit_price=None, reason=prov["reason"], provenance=prov,
                )
            return Action(ActionType.REJECT, contract, 0, None,
                          prov["reason"], prov)

        # Kill switch: EXIT existing, REJECT new.
        if kill_switch_active:
            prov["reason"] = "kill_switch_active"
            if current_position != 0:
                return Action(ActionType.CLOSE, contract,
                              abs(current_position), None,
                              prov["reason"], prov)
            return Action(ActionType.REJECT, contract, 0, None,
                          prov["reason"], prov)

        mid = 0.5 * (kalshi_bid + kalshi_ask)
        prov["mid"] = mid

        # Calibration gate — no size on unproven bands.
        if calibration.band_is_gated(contract.domain, mid):
            prov["reason"] = "band_gated"
            if current_position != 0:
                # Existing position through a band that dropped below the
                # gate: exit rather than get stuck.
                return Action(ActionType.CLOSE, contract,
                              abs(current_position), None,
                              prov["reason"], prov)
            return Action(ActionType.REJECT, contract, 0, None,
                          prov["reason"], prov)

        band = calibration.stats_for(contract.domain, mid)
        # brier_vs_uncorrected < 0 means adjustment helped.
        confidence = (
            0.0 if band is None else max(0.0, -band.brier_vs_uncorrected * 50.0)
        )
        confidence = min(confidence, 1.0)
        prov["calibration_confidence"] = confidence

        # Pick side.
        take_yes = adjusted_p >= mid
        take_price = kalshi_ask if take_yes else (1.0 - kalshi_bid)
        prov["take_side"] = "YES" if take_yes else "NO"
        prov["take_price"] = take_price

        # Edge (fees already baked at SignalGenerator; policy uses raw here
        # because we know current book more precisely). Kept simple: if
        # after-crossing edge is non-positive, no open.
        edge = (adjusted_p - take_price) if take_yes else ((1.0 - adjusted_p) - take_price)
        prov["edge"] = edge
        if edge <= 0:
            prov["reason"] = "no_edge"
            return Action(ActionType.NOOP, contract, 0, None,
                          prov["reason"], prov)

        # Size against calibrated edge, not theoretical edge.
        pos = self._positions()
        size = kelly_size_contracts(
            adjusted_p=(adjusted_p if take_yes else (1.0 - adjusted_p)),
            take_price=take_price,
            limits=self._limits,
            calibration_confidence=confidence,
            current_market_position=pos.get_market(contract.contract_id),
            current_underlying_position=pos.per_underlying.get(contract.underlying, 0),
            current_domain_position=pos.per_domain.get(contract.domain, 0),
            current_aggregate_position=pos.aggregate,
        )
        prov["size"] = size
        if size <= 0:
            prov["reason"] = "size_zero_after_limits"
            return Action(ActionType.NOOP, contract, 0, None,
                          prov["reason"], prov)

        return Action(
            type=ActionType.OPEN,
            contract=contract,
            size_contracts=size,
            limit_price=take_price,
            reason="edge_positive_calibration_ok",
            provenance=prov,
        )
