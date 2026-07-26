"""KrakenDigitalOptionOracle — V1.

Prices Kalshi crypto threshold and up/down contracts as digital options off
Kraken spot and horizon-matched realized vol. Emits a ProbEstimate carrying
point probability, parameter variance (delta-method through sigma), sample
size, and a staleness reason.

Model risk is asymmetric:
  * The lognormal short-horizon crypto assumption is known-wrong in the
    tails, and the longshot contracts where the behavioral bias lives ARE
    the tails.
  * The oracle carries a tail-inflation parameter fit from settled outcomes
    (how often did <15c contracts actually hit vs. lognormal prediction) so
    the bias measurement isn't contaminated by the model's own tail error.

Kraken spot is NOT the CF Benchmarks composite that Kalshi settles on. The
oracle folds an empirically-measured basis into its confidence — Kraken is
a proxy for the settlement index, not the index itself.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone

from scipy.stats import norm

from ..core.contract import Contract, ContractSide
from ..core.estimate import ProbEstimate, StalenessReason
from ..core.oracle import Oracle
from ..ingest.realized_vol import MultiHorizonVol


# Crypto feature keys the CryptoMarketMapper is expected to populate.
FEAT_STRIKE = "strike"
FEAT_DIRECTION = "direction"          # 'above' | 'below' | 'above_or_touch'
FEAT_HORIZON_SEC = "horizon_seconds"  # time to expiry from decision moment
FEAT_UNDERLYING = "underlying_symbol"
FEAT_CONTRACT_TYPE = "contract_type"  # 'threshold' | 'up_down_5m' | 'up_down_15m'


@dataclass
class SpotSnapshot:
    price: float
    ts_epoch: float


@dataclass
class OracleContext:
    """Everything the oracle needs from the outside world at estimate time.

    Kept as an explicit context object rather than global state so tests
    can construct estimates with pinned inputs.
    """

    spot: dict[str, SpotSnapshot]                        # underlying -> spot
    vol_store: MultiHorizonVol
    tail_inflation: dict[str, float]                     # underlying -> multiplier
    basis_bps: dict[str, float]                          # underlying -> mean basis
    basis_std_bps: dict[str, float]                      # underlying -> stdev of basis
    now_epoch: float
    max_spot_staleness_sec: float = 5.0
    min_tick_sample: int = 30


class KrakenDigitalOptionOracle(Oracle):
    name = "kraken_digital"
    version = "1"

    def __init__(self, ctx_provider) -> None:
        """``ctx_provider`` is a callable returning a fresh OracleContext."""
        self._ctx_provider = ctx_provider

    def supports(self, contract: Contract) -> bool:
        if contract.domain != "crypto":
            return False
        ctype = contract.feature(FEAT_CONTRACT_TYPE)
        return ctype in {"threshold", "up_down_5m", "up_down_15m"}

    async def estimate(self, contract: Contract) -> ProbEstimate:
        ctx: OracleContext = self._ctx_provider()
        underlying = contract.feature(FEAT_UNDERLYING) or contract.underlying
        strike = contract.feature(FEAT_STRIKE)
        direction = contract.feature(FEAT_DIRECTION, "above")
        horizon = contract.feature(FEAT_HORIZON_SEC)

        prov: dict = {
            "underlying": underlying, "strike": strike,
            "direction": direction, "horizon_seconds": horizon,
        }

        # --- Input validation ------------------------------------------------
        if strike is None or horizon is None or horizon <= 0:
            return self._stale(StalenessReason.MODEL_OUT_OF_DOMAIN, prov)

        spot = ctx.spot.get(underlying)
        if spot is None:
            return self._stale(StalenessReason.STALE_SPOT, prov)
        spot_age = ctx.now_epoch - spot.ts_epoch
        prov["spot_age_sec"] = spot_age
        if spot_age > ctx.max_spot_staleness_sec:
            return self._stale(StalenessReason.STALE_SPOT, prov)
        prov["spot"] = spot.price

        # Pick horizon-matched realized vol bucket (nearest not longer than horizon).
        vol_horizon = self._pick_vol_horizon(ctx.vol_store.horizons, horizon)
        vol_pt = ctx.vol_store.latest(underlying, vol_horizon)
        if vol_pt is None or vol_pt.sigma_annualized <= 0:
            return self._stale(StalenessReason.THIN_SAMPLE, prov)
        prov["vol_horizon_seconds"] = vol_horizon
        prov["sigma_annualized"] = vol_pt.sigma_annualized
        prov["tick_count"] = vol_pt.tick_count

        if vol_pt.tick_count < ctx.min_tick_sample:
            return self._stale(StalenessReason.THIN_SAMPLE, prov)

        # --- Pricing ---------------------------------------------------------
        # Lognormal short-horizon, drift = 0 (crypto short-horizon).
        # T is fraction of a year (matches sigma_annualized).
        T = horizon / (365.25 * 86_400.0)
        sigma = vol_pt.sigma_annualized
        S = spot.price
        K = strike

        # d2 for P(S_T > K) under GBM with mu=0
        d2 = (math.log(S / K) - 0.5 * sigma * sigma * T) / (sigma * math.sqrt(T))
        if direction == "above" or direction == "above_or_touch":
            p_raw = float(norm.cdf(d2))
        elif direction == "below":
            p_raw = float(norm.cdf(-d2))
        else:
            return self._stale(StalenessReason.MODEL_OUT_OF_DOMAIN, prov)
        prov["p_raw_lognormal"] = p_raw

        # --- Tail inflation --------------------------------------------------
        # Rescale P away from lognormal in the tails using an empirically-fit
        # multiplier ``lambda``. Applied in log-odds space so it can't drive
        # P out of [0,1]:
        #   logit(p_adj) = logit(p_raw) + lambda * tail_signal
        # where tail_signal is a signed "how far into the tail" measure using
        # the raw probability itself (deeper tail => stronger correction).
        lam = ctx.tail_inflation.get(underlying, 0.0)
        tail_signal = self._tail_signal(p_raw)
        prov["tail_inflation_lambda"] = lam
        prov["tail_signal"] = tail_signal
        p = _apply_logit_delta(p_raw, lam * tail_signal)

        # --- Variance --------------------------------------------------------
        # Delta-method through sigma: variance of p wrt sigma dominates for
        # short-horizon crypto. Also fold in the basis-uncertainty term:
        # a noisy Kraken->CF-Benchmarks basis widens our CI proportionally.
        sqrtT = math.sqrt(T)
        # dp/dsigma for lognormal digital call (approx)
        pdf = float(norm.pdf(d2))
        dd2_dsigma = -(math.log(S / K)) / (sigma * sigma * sqrtT) - 0.5 * sqrtT
        dp_dsigma = pdf * dd2_dsigma
        sigma_var = (sigma ** 2) / max(vol_pt.tick_count, 1)  # crude
        var_from_sigma = (dp_dsigma ** 2) * sigma_var

        # Basis contributes as if it were a noisy strike offset.
        basis_std = ctx.basis_std_bps.get(underlying, 0.0) / 1e4
        # dp/dK = -pdf / (K * sigma * sqrt(T))
        dp_dK = -pdf / (K * sigma * sqrtT)
        var_from_basis = (dp_dK * K * basis_std) ** 2

        variance = var_from_sigma + var_from_basis
        prov["variance_from_sigma"] = var_from_sigma
        prov["variance_from_basis"] = var_from_basis

        return ProbEstimate(
            p=_clip01(p),
            variance=float(variance),
            effective_sample_size=int(vol_pt.tick_count),
            data_timestamp=datetime.fromtimestamp(spot.ts_epoch, tz=timezone.utc),
            staleness=StalenessReason.FRESH,
            provenance=prov,
        )

    # ------------------------------------------------------------------ helpers

    def _stale(self, reason: StalenessReason, prov: dict) -> ProbEstimate:
        return ProbEstimate(
            p=0.5, variance=0.25, effective_sample_size=0,
            data_timestamp=datetime.now(timezone.utc),
            staleness=reason, provenance=prov,
        )

    @staticmethod
    def _pick_vol_horizon(horizons: tuple[int, ...], target_seconds: int) -> int:
        # Pick the LONGEST horizon that does not exceed the contract horizon;
        # if none fits, fall back to the shortest available.
        fits = [h for h in horizons if h <= target_seconds]
        return max(fits) if fits else min(horizons)

    @staticmethod
    def _tail_signal(p: float) -> float:
        """Signed distance from 0.5 in log-odds space. Deep tails => large |signal|."""
        eps = 1e-6
        return math.log(max(p, eps) / max(1.0 - p, eps))


def _clip01(x: float) -> float:
    if x < 0.0:
        return 0.0
    if x > 1.0:
        return 1.0
    return x


def _apply_logit_delta(p: float, delta: float) -> float:
    eps = 1e-9
    p = min(max(p, eps), 1.0 - eps)
    lo = math.log(p / (1.0 - p)) + delta
    return 1.0 / (1.0 + math.exp(-lo))
