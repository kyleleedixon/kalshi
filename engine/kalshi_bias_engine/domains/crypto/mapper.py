"""CryptoMarketMapper — translates Kalshi crypto market metadata into
internal Contract objects.

Kalshi crypto markets come in a few families:
  * Threshold markets ("BTC above $X on date Y")
  * Short-horizon up/down ("BTC higher at :15 than at :00")

The mapper is intentionally best-effort with respect to Kalshi's exact
ticker conventions — those change. Any market it can't parse cleanly is
skipped (returned as non-matching), never mis-mapped, so the oracle never
sees a contract with wrong strike/direction/horizon.

Verify current ticker conventions against Kalshi docs before Phase 2.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from ...core.contract import Contract, ContractSide, SettlementSource
from ...ingest.market_mapper import MarketMapperBase
from ...oracles.kraken_digital import (
    FEAT_CONTRACT_TYPE,
    FEAT_DIRECTION,
    FEAT_HORIZON_SEC,
    FEAT_STRIKE,
    FEAT_STRIKE_HI,
    FEAT_STRIKE_LO,
    FEAT_UNDERLYING,
)

_UNDERLYINGS = {"BTC", "ETH", "SOL", "XRP"}

# Loose Kalshi crypto ticker patterns. Kalshi uses tickers like
# "KXBTC-...", "KXBTCD-...", "KXETHU-...". Prefer the ``category`` /
# ``event_ticker`` fields when present rather than regex-parsing tickers.
_TICKER_UNDERLYING_RE = re.compile(r"KX(?P<u>BTC|ETH|SOL|XRP)")

# yes_sub_title shapes observed on live Kalshi crypto markets:
#   "$64,750 to 64,999.99"   → bracket (1332 contracts in corpus)
#   "$1,855 or above"        → threshold above (2687)
#   "$54,199.99 or below"    → threshold below (9)
#   "Target Price: $76.5338" → up_down (45)
# The raw ``strike`` field usually carries just the UPPER bound of the
# bracket, so the sub_title is the only reliable signal for the shape.
_NUM = r"[\d,]+(?:\.\d+)?"
_RE_BRACKET  = re.compile(rf"\${_NUM}\s*to\s*{_NUM}", re.IGNORECASE)
_RE_ABOVE    = re.compile(rf"\${_NUM}\s*(?:or\s+above|and\s+above|\+)", re.IGNORECASE)
_RE_BELOW    = re.compile(rf"\${_NUM}\s*(?:or\s+below|and\s+below|-)", re.IGNORECASE)
_RE_TARGET   = re.compile(r"Target\s+Price", re.IGNORECASE)
_RE_NUMBERS  = re.compile(_NUM)


def _parse_money(s: str) -> float | None:
    try:
        return float(s.replace(",", ""))
    except (TypeError, ValueError):
        return None


def _parse_iso(v: Any) -> datetime | None:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.astimezone(timezone.utc)
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


_CRYPTO_SERIES_TICKERS = [
    # Bitcoin
    "KXBTC", "KXBTCD", "KXBTC15M",
    # Ethereum
    "KXETH", "KXETHD", "KXETH15M",
    # Solana
    "KXSOL", "KXSOLD", "KXSOL15M",
    # XRP
    "KXXRP", "KXXRPD", "KXXRP15M",
]


class CryptoMarketMapper(MarketMapperBase):
    domain = "crypto"

    def discovery_series_tickers(self) -> list[str]:
        return list(_CRYPTO_SERIES_TICKERS)

    def matches(self, raw_market: dict[str, Any]) -> bool:
        category = (raw_market.get("category") or "").lower()
        if "crypto" in category:
            return True
        ticker = raw_market.get("ticker") or ""
        return bool(_TICKER_UNDERLYING_RE.search(ticker))

    def to_contract(self, raw_market: dict[str, Any]) -> Contract | None:
        ticker = raw_market.get("ticker")
        if not ticker:
            return None

        underlying = self._extract_underlying(raw_market)
        if underlying is None:
            return None

        # The reliable signal for market shape is ``yes_sub_title``:
        # Kalshi's ``strike`` field only holds the upper bound for brackets
        # and doesn't distinguish above/below, so relying on it produced
        # the 1332-contract mis-labeling that dominated calibration error.
        sub = str(raw_market.get("yes_sub_title") or "")
        raw_strike = raw_market.get("strike")
        raw_cap = raw_market.get("cap_strike")
        raw_floor = raw_market.get("floor_strike")

        strike: float | None = None
        strike_lo: float | None = None
        strike_hi: float | None = None
        is_bracket = False
        direction: str | None = None

        # Sub-title takes priority when present.
        if _RE_BRACKET.search(sub):
            nums = _RE_NUMBERS.findall(sub)
            if len(nums) >= 2:
                a, b = _parse_money(nums[0]), _parse_money(nums[1])
                if a is not None and b is not None:
                    strike_lo, strike_hi = (a, b) if a <= b else (b, a)
                    is_bracket = True
                    strike = 0.5 * (strike_lo + strike_hi)
        elif _RE_ABOVE.search(sub):
            nums = _RE_NUMBERS.findall(sub)
            if nums:
                v = _parse_money(nums[0])
                if v is not None:
                    strike = v
                    direction = "above"
        elif _RE_BELOW.search(sub):
            nums = _RE_NUMBERS.findall(sub)
            if nums:
                v = _parse_money(nums[0])
                if v is not None:
                    strike = v
                    direction = "below"

        # Fall back to explicit strike/cap/floor fields if sub-title didn't
        # yield a strike (e.g. Target-Price up_down markets).
        if strike is None and strike_lo is None:
            if raw_cap is not None and raw_floor is not None:
                try:
                    strike_hi = float(raw_cap)
                    strike_lo = float(raw_floor)
                except (TypeError, ValueError):
                    return None
                if strike_hi < strike_lo:
                    strike_hi, strike_lo = strike_lo, strike_hi
                is_bracket = True
                strike = 0.5 * (strike_lo + strike_hi)
            elif raw_strike is not None:
                try:
                    strike = float(raw_strike)
                except (TypeError, ValueError):
                    return None
            elif raw_cap is not None:
                try:
                    strike = float(raw_cap)
                except (TypeError, ValueError):
                    return None
            elif raw_floor is not None:
                try:
                    strike = float(raw_floor)
                except (TypeError, ValueError):
                    return None
            else:
                return None

        close_time = _parse_iso(raw_market.get("close_time"))
        open_time = _parse_iso(raw_market.get("open_time"))
        expiration = _parse_iso(
            raw_market.get("expiration_time") or raw_market.get("settlement_time")
        )

        if is_bracket:
            direction = "between"
            contract_type = "bracket"
        else:
            if direction is None:
                direction = self._infer_direction(raw_market)
            contract_type = self._infer_contract_type(raw_market, close_time, expiration)

        horizon_seconds = self._horizon_seconds(open_time, close_time, expiration)

        features = {
            FEAT_UNDERLYING: underlying,
            FEAT_STRIKE: strike,
            FEAT_DIRECTION: direction,
            FEAT_HORIZON_SEC: horizon_seconds,
            FEAT_CONTRACT_TYPE: contract_type,
            "raw_ticker": ticker,
            "raw_event_ticker": raw_market.get("event_ticker"),
            "raw_yes_sub_title": raw_market.get("yes_sub_title"),
        }
        if is_bracket:
            features[FEAT_STRIKE_LO] = strike_lo
            features[FEAT_STRIKE_HI] = strike_hi

        return Contract(
            contract_id=str(ticker),
            domain=self.domain,
            underlying=underlying,
            side=ContractSide.YES,   # We price YES; NO is (1-p) at policy layer.
            open_time=open_time,
            close_time=close_time,
            settlement_time=close_time or expiration,
            settlement_source=SettlementSource.CF_BENCHMARKS_RTI,
            features=features,
        )

    # ---------------------------------------------------------- extractors

    def _extract_underlying(self, raw_market: dict[str, Any]) -> str | None:
        for key in ("underlying", "series_ticker", "event_ticker", "ticker"):
            val = raw_market.get(key)
            if not val:
                continue
            m = _TICKER_UNDERLYING_RE.search(str(val))
            if m:
                return m.group("u")
        title = (raw_market.get("title") or "").upper()
        for u in _UNDERLYINGS:
            if u in title:
                return u
        return None

    def _infer_direction(self, raw_market: dict[str, Any]) -> str:
        # "yes_sub_title" is typically like "above 60000" / "below 60000".
        sub = (raw_market.get("yes_sub_title") or "").lower()
        if "below" in sub or "under" in sub:
            return "below"
        if "touch" in sub or "reach" in sub:
            return "above_or_touch"
        return "above"

    def _infer_contract_type(
        self,
        raw_market: dict[str, Any],
        close_time: datetime | None,
        expiration: datetime | None,
    ) -> str:
        ticker = str(raw_market.get("ticker") or "")
        # Very short expiries (<= 20 min) are treated as up/down products.
        if close_time and expiration:
            dt = (expiration - close_time).total_seconds()
            if dt <= 60:
                secs = (expiration - close_time).total_seconds()
                if secs <= 5 * 60:
                    return "up_down_5m"
                return "up_down_15m"
        if "5M" in ticker.upper():
            return "up_down_5m"
        if "15M" in ticker.upper() or "HOURLY" in ticker.upper():
            return "up_down_15m"
        return "threshold"

    def _horizon_seconds(
        self,
        open_time: datetime | None,
        close_time: datetime | None,
        expiration: datetime | None,
    ) -> int | None:
        now = datetime.now(timezone.utc)
        target = close_time or expiration
        if target is None:
            return None
        secs = (target - now).total_seconds()
        return max(int(secs), 1)
