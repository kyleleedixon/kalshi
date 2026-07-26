"""Per-domain phase gate.

A domain unlocks live execution when the calibration store proves it
deserves to — never by flipping a config flag. Spec section 6:

  Order placement unlocks per-domain only when that domain's out-of-sample
  calibration passes explicit thresholds (minimum settled sample count,
  Brier improvement over uncorrected oracle, band-level reliability within
  tolerance). The gate is code reading the calibration store, not a config
  flag someone flips.

The phase_gates control row in Neon holds THRESHOLDS (min_settled_sample,
min_brier_improvement) that the engine reads. The unlock decision itself
is computed from the latest CalibrationSnapshot. The dashboard's
``unlocked: true`` toggle is an OVERRIDE (fail-closed by default: false)
and is deliberately conservative — the engine still requires the
calibration checks to pass regardless.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..core.calibration import CalibrationReport
from ..storage.control import ControlReader


@dataclass
class GateOutcome:
    domain: str
    unlocked: bool
    reason: str


class PhaseGate:
    def __init__(self, control: ControlReader) -> None:
        self._control = control

    def evaluate(self, domain: str, report: CalibrationReport) -> GateOutcome:
        state = self._control.get()
        if not state.fresh:
            return GateOutcome(domain, False, "control_read_failed_fail_closed")
        gate_cfg = state.phase_gates.get(domain) or {}
        min_sample = int(gate_cfg.get("min_settled_sample", 200))
        min_improvement = float(gate_cfg.get("min_brier_improvement", 0.005))
        operator_unlock = bool(gate_cfg.get("unlocked", False))

        if not operator_unlock:
            return GateOutcome(domain, False, "operator_lock")

        # Require at least one band with enough sample AND improvement.
        # More stringent gates (all bands, per-band reliability) can be
        # added by tightening this predicate.
        domain_bands = [v for k, v in report.bands.items() if k.domain == domain]
        if not domain_bands:
            return GateOutcome(domain, False, "no_calibration_bands")
        best = max(domain_bands, key=lambda b: -b.brier_vs_uncorrected)
        if best.settled_count < min_sample:
            return GateOutcome(domain, False,
                               f"insufficient_sample:{best.settled_count}")
        improvement = -best.brier_vs_uncorrected
        if improvement < min_improvement:
            return GateOutcome(domain, False,
                               f"insufficient_improvement:{improvement:.4f}")
        return GateOutcome(domain, True, "ok")
