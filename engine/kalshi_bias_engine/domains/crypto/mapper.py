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
    FEAT_UNDERLYING,
)

_UNDERLYINGS = {"BTC", "ETH", "SOL", "XRP"}

# Loose Kalshi crypto ticker patterns. Kalshi uses tickers like
# "KXBTC-...", "KXBTCD-...", "KXETHU-...". Prefer the ``category`` /
# ``event_ticker`` fields when present rather than regex-parsing tickers.
_TICKER_UNDERLYING_RE = re.compile(r"KX(?P<u>BTC|ETH|SOL|XRP)")


def _parse_iso(v: Any) -> datetime | None:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.astimezone(timezone.utc)
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


class CryptoMarketMapper(MarketMapperBase):
    domain = "crypto"

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

        # Kalshi represents strike as either ``cap_strike`` / ``floor_strike``
        # or a single ``strike`` on some product families. We prefer the
        # explicit ``strike`` field, then fall back.
        strike = (
            raw_market.get("strike")
            or raw_market.get("cap_strike")
            or raw_market.get("floor_strike")
        )
        if strike is None:
            return None
        try:
            strike = float(strike)
        except (TypeError, ValueError):
            return None

        direction = self._infer_direction(raw_market)
        close_time = _parse_iso(raw_market.get("close_time"))
        open_time = _parse_iso(raw_market.get("open_time"))
        expiration = _parse_iso(
            raw_market.get("expiration_time") or raw_market.get("settlement_time")
        )

        contract_type = self._infer_contract_type(raw_market, close_time, expiration)

        horizon_seconds = self._horizon_seconds(open_time, close_time, expiration)

        return Contract(
            contract_id=str(ticker),
            domain=self.domain,
            underlying=underlying,
            side=ContractSide.YES,   # We price YES; NO is (1-p) at policy layer.
            open_time=open_time,
            close_time=close_time,
            settlement_time=expiration,
            settlement_source=SettlementSource.CF_BENCHMARKS_RTI,
            features={
                FEAT_UNDERLYING: underlying,
                FEAT_STRIKE: strike,
                FEAT_DIRECTION: direction,
                FEAT_HORIZON_SEC: horizon_seconds,
                FEAT_CONTRACT_TYPE: contract_type,
                "raw_ticker": ticker,
                "raw_event_ticker": raw_market.get("event_ticker"),
                "raw_yes_sub_title": raw_market.get("yes_sub_title"),
            },
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
        target = expiration or close_time
        if target is None:
            return None
        secs = (target - now).total_seconds()
        return max(int(secs), 1)
