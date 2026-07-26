"""Kalshi REST + WS client (read-only).

Persists every raw response to the spool as ``raw_pull`` before returning
parsed data. Post-mortems and recalibration need originals — never trust
the parsed layer to be complete.

READ-ONLY: no order-placement methods live here. The live-execution module
under ``execution/live/`` is import-gated and not built in this session.

Endpoint paths are collected in ``PATHS`` at the top of the module so they
are trivial to update against current Kalshi docs. Do not scatter path
strings through method bodies.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, AsyncIterator

import httpx
import orjson
import structlog
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from .kalshi_auth import KalshiSigner
from ..storage.spool import SpooledWriter

log = structlog.get_logger(__name__)


# --- Endpoint catalog -------------------------------------------------------
# Verify these against current Kalshi docs before Phase 2.
PATHS = {
    "markets": "/markets",
    "market": "/markets/{ticker}",
    "orderbook": "/markets/{ticker}/orderbook",
    "events": "/events",
    "event": "/events/{event_ticker}",
    "series": "/series",
    "exchange_status": "/exchange/status",
    "exchange_schedule": "/exchange/schedule",
}


@dataclass
class KalshiConfig:
    api_base: str
    signer: KalshiSigner


class KalshiClient:
    def __init__(self, config: KalshiConfig, spool: SpooledWriter,
                 http_client: httpx.AsyncClient | None = None) -> None:
        self._cfg = config
        self._spool = spool
        self._http = http_client or httpx.AsyncClient(
            base_url=config.api_base, timeout=15.0
        )

    async def close(self) -> None:
        await self._http.aclose()

    async def _record(
        self, endpoint: str, params: dict | None, resp: httpx.Response
    ) -> None:
        try:
            body = resp.json()
        except Exception:
            body = {"_raw_text": resp.text}
        await self._spool.enqueue("raw_pull", {
            "source": "kalshi_rest",
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
        headers = self._cfg.signer.headers("GET", path)
        resp = await self._http.get(path, params=params, headers=headers)
        await self._record(path, params, resp)
        resp.raise_for_status()
        return resp.json()

    # --- Public read-only surface -----------------------------------------

    async def exchange_status(self) -> dict[str, Any]:
        return await self._get(PATHS["exchange_status"])

    async def list_markets(
        self,
        *,
        limit: int = 100,
        cursor: str | None = None,
        event_ticker: str | None = None,
        series_ticker: str | None = None,
        status: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"limit": limit}
        if cursor:
            params["cursor"] = cursor
        if event_ticker:
            params["event_ticker"] = event_ticker
        if series_ticker:
            params["series_ticker"] = series_ticker
        if status:
            params["status"] = status
        return await self._get(PATHS["markets"], params=params)

    async def iter_markets(self, **kwargs) -> AsyncIterator[dict[str, Any]]:
        cursor: str | None = None
        while True:
            page = await self.list_markets(cursor=cursor, **kwargs)
            for m in page.get("markets", []):
                yield m
            cursor = page.get("cursor")
            if not cursor:
                return

    async def get_market(self, ticker: str) -> dict[str, Any]:
        return await self._get(PATHS["market"].format(ticker=ticker))

    async def get_orderbook(self, ticker: str) -> dict[str, Any]:
        return await self._get(PATHS["orderbook"].format(ticker=ticker))

    async def list_events(self, **kwargs) -> dict[str, Any]:
        return await self._get(PATHS["events"], params=kwargs or None)


# --- WebSocket streaming ----------------------------------------------------
# The trade-api WS lives at wss://api.elections.kalshi.com/trade-api/ws/v2 and
# accepts JSON commands like {"id": 1, "cmd": "subscribe",
#   "params": {"channels": ["ticker_v2"], "market_tickers": [...]}}.
# Verify against current docs.

@dataclass
class KalshiWsConfig:
    ws_url: str = "wss://api.elections.kalshi.com/trade-api/ws/v2"
    signer: KalshiSigner | None = None


class KalshiWebSocket:
    def __init__(self, config: KalshiWsConfig, spool: SpooledWriter) -> None:
        self._cfg = config
        self._spool = spool

    async def stream(
        self,
        market_tickers: list[str],
        channels: tuple[str, ...] = ("orderbook_delta", "ticker_v2"),
    ) -> AsyncIterator[dict[str, Any]]:
        import websockets  # local import so client is usable without WS deps

        headers = {}
        if self._cfg.signer and self._cfg.signer.enabled:
            headers.update(self._cfg.signer.headers("GET", "/trade-api/ws/v2"))

        backoff = 1.0
        cmd_id = 0
        while True:
            try:
                async with websockets.connect(
                    self._cfg.ws_url,
                    additional_headers=headers or None,
                    ping_interval=20,
                    ping_timeout=20,
                ) as ws:
                    backoff = 1.0
                    for channel in channels:
                        cmd_id += 1
                        await ws.send(orjson.dumps({
                            "id": cmd_id,
                            "cmd": "subscribe",
                            "params": {
                                "channels": [channel],
                                "market_tickers": market_tickers,
                            },
                        }).decode())
                    async for msg in ws:
                        payload = orjson.loads(msg)
                        await self._spool.enqueue("raw_pull", {
                            "source": "kalshi_ws",
                            "endpoint": ",".join(channels),
                            "request_params": {"market_tickers": market_tickers},
                            "response": payload,
                            "http_status": None,
                            "ingest_ts": datetime.now(timezone.utc).isoformat(),
                        })
                        yield payload
            except Exception as e:
                log.warning("kalshi_ws.reconnect", error=str(e), backoff=backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)
