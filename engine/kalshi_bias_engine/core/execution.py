"""ExecutionPolicy interface.

Maps (adjusted probability, quoted price, calibration state, risk state) to
an Action.

Consumes the CalibrationReport, not raw model output. Position size scales
with demonstrated calibration in that price band and domain, not theoretical
edge. A band with insufficient settled sample gets zero size regardless of
apparent edge — see BandKey/CalibrationReport for the guard.

Two implementations:
  * PaperExecutionPolicy (Phase 1) — logs hypothetical fills against the
    real book. Does not import anything under execution.live.
  * LiveExecutionPolicy (Phase 2) — under execution/live/, import-gated.
    Not built in this session.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum

from .bias import BiasAdjustment
from .calibration import CalibrationReport
from .contract import Contract
from .estimate import ProbEstimate


class ActionType(str, Enum):
    OPEN = "OPEN"
    CLOSE = "CLOSE"          # explicit early close (edge gone / staleness)
    HOLD = "HOLD"            # existing position, do nothing this cycle
    NOOP = "NOOP"            # no signal
    REJECT = "REJECT"        # gated (calibration insufficient, kill-switch, etc.)


@dataclass(frozen=True)
class Action:
    """The output of ExecutionPolicy.decide.

    ``size_contracts`` is the desired position in contracts (positive = long
    YES side of the Contract; negation for NO happens at the Kalshi API
    boundary, not here).
    """

    type: ActionType
    contract: Contract
    size_contracts: int
    limit_price: float | None
    reason: str
    provenance: dict = field(default_factory=dict)


class ExecutionPolicy(ABC):
    @abstractmethod
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
        """Return the next action for this contract.

        Contract with ExecutionPolicy implementations:

        * If ``kill_switch_active`` -> REJECT (no new opens) but MAY still
          emit CLOSE for existing positions.
        * If ``not raw.is_fresh`` -> if position open, CLOSE; else REJECT.
          Stale oracle data is an EXIT condition, not a HOLD.
        * If ``calibration.band_is_gated(domain, kalshi_mid)`` -> REJECT
          for new opens. Position holding through a band drop should CLOSE
          rather than get stuck.
        * Fee-adjusted edge must beat the crossing cost from the live book
          (do not assume fills at mid).
        * No P&L-based take-profit / stop-loss. Exits are edge-driven or
          data-driven — never level-driven. That's the retail behavior this
          system trades against.
        """
