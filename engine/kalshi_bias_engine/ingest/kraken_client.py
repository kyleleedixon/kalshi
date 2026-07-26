"""Kraken public REST + WebSocket client (no auth needed for market data).

REST covers cold-start snapshots (last trades, current spot). WS carries
live trades used for realized-vol updates. Everything raw goes to the
spool for post-mortem.

Verify endpoint shapes against current Kraken docs before Phase 2.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, AsyncIterator

import httpx
import orjson
import structlog
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from ..storage.spool import SpooledWriter

log = structlog.get_logger(__name__)


# V1 crypto tickers on Kalshi map to Kraken pair symbols:
KRAKEN_PAIRS = {
    "BTC": "BTC/USD",
    "ETH": "ETH/USD",
    "SOL": "SOL/USD",
    "XRP": "XRP/USD",
}


@dataclass
class KrakenConfig:
    rest_base: str = "https://api.kraken.com"
    ws_url: str = "wss://ws.kraken.com/v2"


class KrakenRest:
    def __init__(self, config: KrakenConfig, spool: SpooledWriter,
                 http_client: httpx.AsyncClient | None = None) -> None:
        self._cfg = config
        self._spool = spool
        self._http = http_client or httpx.AsyncClient(
            base_url=config.rest_base, timeout=15.0
        )

    async def close(self) -> None:
        await self._http.aclose()

    async def _record(self, endpoint: str, params: dict | None,
                      resp: httpx.Response) -> None:
        try:
            body = resp.json()
        except Exception:
            body = {"_raw_text": resp.text}
        await self._spool.enqueue("raw_pull", {
            "source": "kraken_rest",
            "endpoint": endpoint,
            "request_params": params,
            "response": body,
            "http_status": resp.status_code,
            "ingest_ts": datetime.now(timezone.utc).isoformat(),
        })

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=0.5, min=0.5, max=4.0),
        retry=retry_if_exception_type((httpx.HTTPError,)),
        reraise=True,
    )
    async def _get(self, path: str, params: dict | None = None) -> dict[str, Any]:
        resp = await self._http.get(path, params=params)
        await self._record(path, params, resp)
        resp.raise_for_status()
        return resp.json()

    async def ticker(self, pair: str) -> dict[str, Any]:
        return await self._get("/0/public/Ticker", params={"pair": pair})

    async def recent_trades(self, pair: str, since_ns: int | None = None
                            ) -> dict[str, Any]:
        params: dict[str, Any] = {"pair": pair}
        if since_ns is not None:
            params["since"] = str(since_ns)
        return await self._get("/0/public/Trades", params=params)


class KrakenWs:
    """Kraken v2 WS. Subscribes to ``trade`` channel for the given symbols."""

    def __init__(self, config: KrakenConfig, spool: SpooledWriter) -> None:
        self._cfg = config
        self._spool = spool

    async def stream_trades(self, symbols: list[str]) -> AsyncIterator[dict[str, Any]]:
        import websockets

        backoff = 1.0
        while True:
            try:
                async with websockets.connect(
                    self._cfg.ws_url,
                    ping_interval=20,
                    ping_timeout=20,
                ) as ws:
                    backoff = 1.0
                    await ws.send(orjson.dumps({
                        "method": "subscribe",
                        "params": {"channel": "trade", "symbol": symbols, "snapshot": True},
                    }).decode())
                    async for msg in ws:
                        payload = orjson.loads(msg)
                        await self._spool.enqueue("raw_pull", {
                            "source": "kraken_ws",
                            "endpoint": "trade",
                            "request_params": {"symbol": symbols},
                            "response": payload,
                            "http_status": None,
                            "ingest_ts": datetime.now(timezone.utc).isoformat(),
                        })
                        yield payload
            except Exception as e:
                log.warning("kraken_ws.reconnect", error=str(e), backoff=backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)
