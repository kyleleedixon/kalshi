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
FEAT_STRIKE_LO = "strike_lo"          # bracket lower bound
FEAT_STRIKE_HI = "strike_hi"          # bracket upper bound
FEAT_DIRECTION = "direction"          # 'above' | 'below' | 'above_or_touch'
FEAT_HORIZON_SEC = "horizon_seconds"  # time to expiry from decision moment
FEAT_UNDERLYING = "underlying_symbol"
FEAT_CONTRACT_TYPE = "contract_type"  # 'threshold' | 'bracket' | 'up_down_5m' | 'up_down_15m'


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
    max_spot_staleness_sec: float = 30.0
    min_tick_sample: int = 30
    # Log-return noise floor applied at settlement. Without this, σ√T → 0
    # as the contract nears close and the lognormal digital blows up to
    # p∈{0,1}. Real settlements have residual noise (CF Benchmarks is a
    # windowed composite, and there is minute-scale spot jitter besides).
    # 10 bps ≈ typical BTC 1-minute log-return stdev.
    settlement_noise_log: float = 0.001


class KrakenDigitalOptionOracle(Oracle):
    name = "kraken_digital"
    # v4: vol-horizon fallback + insufficient-window guard in RollingVol.
    #     v3 asked the 24h vol bucket for sigma when it had only 60s of
    #     ticks; annualizing that noise produced σ≈200 and priced every
    #     24h contract at the boundary. Now: shorter horizons fall back
    #     through, and the vol store refuses to emit sigma until the
    #     observed window covers ≥25% of the requested horizon.
    # v3: settlement noise floor (see OracleContext.settlement_noise_log)
    #     added so the digital does not blow up to p∈{0,1} as horizon → 0.
    # v2: bracket contracts priced as Φ(d2_lo) − Φ(d2_hi) instead of
    #     single-strike "above cap_strike".
    # v1: original — broken bracket pricing AND broken horizon (used
    #     expiration_time ≈ 7 days instead of close_time).
    version = "4"

    def __init__(self, ctx_provider) -> None:
        """``ctx_provider`` is a callable returning a fresh OracleContext."""
        self._ctx_provider = ctx_provider

    def supports(self, contract: Contract) -> bool:
        if contract.domain != "crypto":
            return False
        ctype = contract.feature(FEAT_CONTRACT_TYPE)
        return ctype in {"threshold", "bracket", "up_down_5m", "up_down_15m"}

    async def estimate(self, contract: Contract) -> ProbEstimate:
        ctx: OracleContext = self._ctx_provider()
        underlying = contract.feature(FEAT_UNDERLYING) or contract.underlying
        strike = contract.feature(FEAT_STRIKE)
        strike_lo = contract.feature(FEAT_STRIKE_LO)
        strike_hi = contract.feature(FEAT_STRIKE_HI)
        direction = contract.feature(FEAT_DIRECTION, "above")
        horizon = contract.feature(FEAT_HORIZON_SEC)
        contract_type = contract.feature(FEAT_CONTRACT_TYPE)
        is_bracket = contract_type == "bracket"

        prov: dict = {
            "underlying": underlying, "strike": strike,
            "strike_lo": strike_lo, "strike_hi": strike_hi,
            "direction": direction, "horizon_seconds": horizon,
            "contract_type": contract_type,
        }

        # --- Input validation ------------------------------------------------
        if horizon is None or horizon <= 0:
            return self._stale(StalenessReason.MODEL_OUT_OF_DOMAIN, prov)
        if is_bracket:
            if strike_lo is None or strike_hi is None or strike_hi <= strike_lo:
                return self._stale(StalenessReason.MODEL_OUT_OF_DOMAIN, prov)
        else:
            if strike is None:
                return self._stale(StalenessReason.MODEL_OUT_OF_DOMAIN, prov)

        spot = ctx.spot.get(underlying)
        if spot is None:
            return self._stale(StalenessReason.STALE_SPOT, prov)
        spot_age = ctx.now_epoch - spot.ts_epoch
        prov["spot_age_sec"] = spot_age
        if spot_age > ctx.max_spot_staleness_sec:
            return self._stale(StalenessReason.STALE_SPOT, prov)
        prov["spot"] = spot.price

        # Pick horizon-matched realized vol bucket (longest not exceeding
        # the contract horizon). If that bucket hasn't collected enough
        # window yet (vol store returns sigma=0), fall back through
        # progressively shorter horizons so a warm-up process still prices
        # 24h contracts off the 15m/1h bucket instead of waiting hours.
        vol_pt, vol_horizon = self._pick_vol_with_fallback(
            ctx.vol_store, underlying, horizon, ctx.min_tick_sample
        )
        if vol_pt is None:
            return self._stale(StalenessReason.THIN_SAMPLE, prov)
        prov["vol_horizon_seconds"] = vol_horizon
        prov["sigma_annualized"] = vol_pt.sigma_annualized
        prov["tick_count"] = vol_pt.tick_count

        # --- Pricing ---------------------------------------------------------
        # Lognormal short-horizon, drift = 0 (crypto short-horizon).
        # T is fraction of a year (matches sigma_annualized).
        T = horizon / (365.25 * 86_400.0)
        sigma = vol_pt.sigma_annualized
        S = spot.price

        # Log-return stdev over horizon T, floored by settlement noise so
        # the digital doesn't collapse to {0,1} as horizon → 0. See
        # ``OracleContext.settlement_noise_log``.
        eps = ctx.settlement_noise_log
        stdev_lr = math.sqrt(sigma * sigma * T + eps * eps)
        drift = 0.5 * sigma * sigma * T  # d2 mean-shift; leave as-is
        prov["stdev_log_return"] = stdev_lr
        prov["settlement_noise_log"] = eps

        if is_bracket:
            # P(K_lo <= S_T <= K_hi) = P(S_T > K_lo) - P(S_T > K_hi)
            #                        = Φ(d2_lo) - Φ(d2_hi)
            d2_lo = (math.log(S / strike_lo) - drift) / stdev_lr
            d2_hi = (math.log(S / strike_hi) - drift) / stdev_lr
            p_raw = float(norm.cdf(d2_lo) - norm.cdf(d2_hi))
            prov["d2_lo"] = d2_lo
            prov["d2_hi"] = d2_hi
            # For variance / tail-signal purposes use the midpoint d2.
            K_ref = 0.5 * (strike_lo + strike_hi)
            d2_ref = (math.log(S / K_ref) - drift) / stdev_lr
        else:
            K = strike
            d2 = (math.log(S / K) - drift) / stdev_lr
            if direction == "above" or direction == "above_or_touch":
                p_raw = float(norm.cdf(d2))
            elif direction == "below":
                p_raw = float(norm.cdf(-d2))
            else:
                return self._stale(StalenessReason.MODEL_OUT_OF_DOMAIN, prov)
            K_ref = K
            d2_ref = d2
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
        # Uses stdev_lr (floored by settlement noise) as the denominator so
        # derivatives don't blow up as T → 0.
        pdf = float(norm.pdf(d2_ref))
        dd2_dsigma = -(math.log(S / K_ref)) * (sigma * T) / (stdev_lr ** 3) - (sigma * T) / stdev_lr
        dp_dsigma = pdf * dd2_dsigma
        sigma_var = (sigma ** 2) / max(vol_pt.tick_count, 1)  # crude
        var_from_sigma = (dp_dsigma ** 2) * sigma_var

        # Basis contributes as if it were a noisy strike offset.
        basis_std = ctx.basis_std_bps.get(underlying, 0.0) / 1e4
        # dp/dK = -pdf / (K * stdev_lr)
        dp_dK = -pdf / (K_ref * stdev_lr)
        var_from_basis = (dp_dK * K_ref * basis_std) ** 2

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
    def _pick_vol_with_fallback(vol_store, underlying, target_seconds, min_ticks):
        # Try longest-fitting first (best matched to contract horizon),
        # fall back to shorter horizons if the longer bucket hasn't
        # accumulated enough window yet. If nothing at-or-below the
        # target has data, try longer horizons as a last resort.
        horizons = sorted(vol_store.horizons)
        at_or_below = [h for h in horizons if h <= target_seconds]
        above = [h for h in horizons if h > target_seconds]
        candidates = list(reversed(at_or_below)) + above
        for h in candidates:
            pt = vol_store.latest(underlying, h)
            if pt is None:
                continue
            if pt.sigma_annualized <= 0:
                continue
            if pt.tick_count < min_ticks:
                continue
            return pt, h
        return None, 0

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
