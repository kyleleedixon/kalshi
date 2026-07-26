"""Engine entrypoint (Phase 1 paper).

Wires together the crypto domain, Kraken vol pipeline, Kalshi read-only
client, oracle, bias model, signal generator, and PaperExecutionPolicy.

Does NOT import ``execution.live`` — that package raises ImportError by
design in Phase 1.
"""

from __future__ import annotations

import asyncio
import signal
import uuid
from datetime import datetime, timezone
from typing import Any

import structlog

from .. import __version__
from ..bias.model import ComposedBiasModel
from ..bias.features import (
    LongshotCurveFeature,
    RecencyMomentumFeature,
    SessionLiquidityFeature,
)
from ..calibration.report import load_latest_report
from ..core.registry import DomainRegistry
from ..domains.crypto import register as crypto_register
from ..domains.crypto.bias_features import RoundNumberDistanceFeature
from ..ingest.kalshi_auth import KalshiSigner
from ..ingest.kalshi_client import KalshiClient, KalshiConfig
from ..ingest.kraken_client import KRAKEN_PAIRS, KrakenConfig, KrakenWs
from ..ingest.realized_vol import MultiHorizonVol
from ..ledger.paper_policy import PaperExecutionPolicy
from ..ledger.positions import CachingPositionsProvider
from ..ledger.risk import RiskLimits
from ..signal.fees import KalshiFeeSchedule
from ..signal.generator import BookSnapshot, SignalGenerator
from ..storage.control import ControlReader
from ..storage.heartbeat import Heartbeater
from ..storage.writer import build_writer
from .oracle_context import OracleCtxHub
from .refit_loop import RefitScheduler
from .settlement_loop import SettlementIngestor
from .settings import Phase, get_settings

log = structlog.get_logger(__name__)


async def _kraken_ingest(ws: KrakenWs, hub: OracleCtxHub, symbols: list[str]) -> None:
    """Consume Kraken trade stream into the vol hub. Parses Kraken v2
    ``trade`` messages — verify shape against current docs.

    Message shape (v2)::
        {"channel": "trade", "type": "update",
         "data": [{"symbol": "BTC/USD", "price": "60000.0",
                    "qty": "0.001", "timestamp": "2026-07-26T12:00:00Z", ...}]}
    """
    async for msg in ws.stream_trades(symbols):
        if not isinstance(msg, dict):
            continue
        if msg.get("channel") != "trade":
            continue
        for row in msg.get("data") or []:
            sym = row.get("symbol")
            price = row.get("price")
            ts = row.get("timestamp")
            if sym is None or price is None or ts is None:
                continue
            underlying = _pair_to_underlying(sym)
            if underlying is None:
                continue
            try:
                p = float(price)
                t = _parse_epoch(ts)
            except (ValueError, TypeError):
                continue
            hub.record_trade(underlying, t, p)


def _pair_to_underlying(pair: str) -> str | None:
    for u, p in KRAKEN_PAIRS.items():
        if p == pair:
            return u
    return None


def _parse_epoch(ts: str | float | int) -> float:
    if isinstance(ts, (int, float)):
        return float(ts)
    return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()


async def _discover_and_trade_once(
    kalshi: KalshiClient,
    signal_gen: SignalGenerator,
    policy: PaperExecutionPolicy,
    control: ControlReader,
    writer,
    oracle,
    positions_provider,
) -> None:
    """One pass: discover crypto markets, fetch books, generate signals,
    ask PaperExecutionPolicy to decide, spool orders & fills.

    Spool ordering matters: for each contract we enqueue
    ``contract_upsert`` → ``quote`` → ``oracle_estimate`` → ``signal``
    (→ ``paper_order``) so FK resolution on the drain side always finds
    its target. The drainer processes strictly in insertion order and
    breaks on the first failure to preserve that invariant.
    """
    report = load_latest_report(default_min_sample=get_settings().phase_gate_min_sample)
    kill_switch = control.get().kill_switch_active

    mapper = DomainRegistry.get("crypto").mapper
    picks: list[tuple[Any, BookSnapshot]] = []

    async for m in kalshi.iter_markets(status="open"):
        if not mapper.matches(m):
            continue
        contract = mapper.to_contract(m)
        if contract is None:
            continue
        try:
            ob = await kalshi.get_orderbook(contract.contract_id)
        except Exception as e:
            log.warning("orderbook.fetch_failed",
                        contract=contract.contract_id, error=str(e))
            continue
        book = _parse_book(ob)
        if book is None:
            continue

        await writer.enqueue("contract_upsert", _contract_upsert_payload(contract))
        await writer.enqueue("quote", {
            "contract_id": contract.contract_id,
            "bid": book.bid,
            "ask": book.ask,
            "bid_size": book.bid_size,
            "ask_size": book.ask_size,
            "last_trade_price": None,
            "data_ts": book.data_ts.isoformat(),
            "ingest_ts": datetime.now(timezone.utc).isoformat(),
        })

        picks.append((contract, book))

    signals = await signal_gen.generate(picks, report)

    for s in signals:
        # One oracle estimate per signal (SignalGenerator already called the
        # oracle inside). Persist it, then reference its external_id from
        # the signal row so full provenance survives the ledger.
        estimate_external_id = str(uuid.uuid4())
        signal_external_id = str(uuid.uuid4())

        await writer.enqueue("oracle_estimate", {
            "external_id": estimate_external_id,
            "contract_id": s.contract.contract_id,
            "oracle_name": oracle.name,
            "oracle_version": oracle.version,
            "p": s.raw.p,
            "variance": s.raw.variance,
            "effective_sample_size": s.raw.effective_sample_size,
            "staleness": s.raw.staleness.value,
            "data_ts": s.raw.data_timestamp.isoformat(),
            "provenance": s.raw.provenance,
            "ingest_ts": datetime.now(timezone.utc).isoformat(),
        })

        await writer.enqueue("signal", {
            "external_id": signal_external_id,
            "contract_id": s.contract.contract_id,
            "oracle_estimate_external_id": estimate_external_id,
            "adjusted_p": s.adjusted_p,
            "kalshi_bid": s.book.bid,
            "kalshi_ask": s.book.ask,
            "fee_bps": s.fee_per_contract * 10_000.0,
            "edge_net": s.edge_net,
            "calibration_confidence": s.calibration_confidence,
            "bias_adjustments": [
                {"feature": a.feature_name, "delta": a.delta,
                 "evidence_ok": a.evidence_ok, "params": a.params_snapshot}
                for a in s.adjustments
            ],
            "rank": s.rank,
            "created_at": datetime.now(timezone.utc).isoformat(),
        })

        # Only decide (paper) on positive-edge signals; NOOP others.
        if s.edge_net <= 0 or s.calibration_confidence <= 0:
            continue

        current_pos = positions_provider().get_market(s.contract.contract_id)
        action = policy.decide(
            contract=s.contract,
            raw=s.raw,
            adjusted_p=s.adjusted_p,
            adjustments=s.adjustments,
            kalshi_bid=s.book.bid or 0.0,
            kalshi_ask=s.book.ask or 1.0,
            current_position=current_pos,
            calibration=report,
            kill_switch_active=kill_switch,
        )

        now = datetime.now(timezone.utc)
        is_filled = action.type.value == "OPEN"
        order_payload: dict[str, Any] = {
            "signal_external_id": signal_external_id,
            "contract_id": s.contract.contract_id,
            "side": action.provenance.get("take_side", "YES"),
            "action": action.type.value,
            "size_contracts": action.size_contracts,
            "limit_price": action.limit_price,
            "hypothetical_fill_price": action.limit_price,
            "hypothetical_fill_size": action.size_contracts,
            "status": "FILLED" if is_filled else "REJECTED",
            "reason": action.reason,
            "created_at": now.isoformat(),
        }
        if is_filled and action.limit_price is not None and action.size_contracts > 0:
            order_payload["attached_fill"] = {
                "price": action.limit_price,
                "size_contracts": action.size_contracts,
                "fee": s.fee_per_contract * action.size_contracts,
                "fill_ts": now.isoformat(),
            }
        await writer.enqueue("paper_order", order_payload)


def _contract_upsert_payload(contract) -> dict[str, Any]:
    return {
        "contract_id": contract.contract_id,
        "domain": contract.domain,
        "underlying": contract.underlying,
        "side": contract.side.value,
        "open_time": contract.open_time.isoformat() if contract.open_time else None,
        "close_time": contract.close_time.isoformat() if contract.close_time else None,
        "settlement_time": (
            contract.settlement_time.isoformat() if contract.settlement_time else None
        ),
        "settlement_source": contract.settlement_source.value,
        "features": contract.features,
    }


def _parse_book(ob: dict[str, Any]) -> BookSnapshot | None:
    # Kalshi orderbook response: {"orderbook": {"yes": [[price_cents, size], ...],
    #                                            "no":  [[price_cents, size], ...]}}
    # We convert to dollars in [0, 1] and take the best (highest) bid on YES
    # and the best (lowest) ask derived from the NO side: ask_yes = 1 - best_no_bid.
    ob_root = ob.get("orderbook") or {}
    yes = ob_root.get("yes") or []
    no = ob_root.get("no") or []
    best_yes_bid = _best_price(yes, side="bid")
    best_no_bid = _best_price(no, side="bid")
    bid = best_yes_bid
    ask = None if best_no_bid is None else (1.0 - best_no_bid)
    if bid is None and ask is None:
        return None
    return BookSnapshot(
        bid=bid, ask=ask,
        bid_size=None, ask_size=None,
        data_ts=datetime.now(timezone.utc),
    )


def _best_price(levels: list, side: str) -> float | None:
    prices = []
    for row in levels or []:
        try:
            price_cents = float(row[0])
            prices.append(price_cents / 100.0)
        except (TypeError, ValueError, IndexError):
            continue
    if not prices:
        return None
    return max(prices) if side == "bid" else min(prices)


async def run() -> None:
    settings = get_settings()
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ]
    )
    log.info("engine.start", version=__version__, phase=settings.phase.value,
             engine_id=settings.engine_id)

    # Refuse to start if operator misconfigured to LIVE — Phase 2 requires
    # explicit build-time enablement plus calibration-gate approval, and
    # neither is set up in this session.
    if settings.phase is Phase.LIVE:
        raise RuntimeError(
            "KBE_PHASE=LIVE is not supported in this build. The live-execution "
            "module is import-gated; unlock per-domain via the calibration store "
            "and the phase-gate module, then re-enable in a follow-up build."
        )

    writer = build_writer(settings.spool_path)
    writer.start()

    control = ControlReader(ttl_seconds=3.0)
    heart = Heartbeater(
        writer=writer,
        engine_id=settings.engine_id,
        phase=settings.phase.value,
        interval_sec=settings.heartbeat_interval_sec,
    )
    heart.start()

    # Domain install: crypto only for V1.
    vol_store = MultiHorizonVol()
    hub = OracleCtxHub(vol_store=vol_store)
    crypto_register.install(oracle_ctx_provider=hub.snapshot)

    entry = DomainRegistry.get("crypto")
    oracle = entry.oracle_factory()

    bias = ComposedBiasModel()
    # Venue-level features first (order affects provenance readability only).
    bias.register(LongshotCurveFeature())
    bias.register(RecencyMomentumFeature())
    bias.register(SessionLiquidityFeature())
    # Domain-specific features.
    for f in entry.bias_features:
        bias.register(f)

    kalshi = KalshiClient(
        KalshiConfig(
            api_base=settings.kalshi_api_base,
            signer=KalshiSigner(
                key_id=settings.kalshi_key_id,
                private_key_pem=settings.kalshi_private_key_pem,
            ),
        ),
        spool=writer,
    )
    kraken_ws = KrakenWs(
        KrakenConfig(rest_base=settings.kraken_rest_base, ws_url=settings.kraken_ws_url),
        spool=writer,
    )

    fees = KalshiFeeSchedule()
    signal_gen = SignalGenerator(oracle=oracle, bias_model=bias, fees=fees)
    positions_provider = CachingPositionsProvider(ttl_seconds=2.0)
    policy = PaperExecutionPolicy(
        limits=RiskLimits(),
        positions_provider=positions_provider,
    )

    stop = asyncio.Event()

    def _handle_signal() -> None:
        log.info("engine.stop_requested")
        stop.set()

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _handle_signal)
        except NotImplementedError:
            pass  # Windows / non-unix

    kraken_task = asyncio.create_task(
        _kraken_ingest(kraken_ws, hub, list(KRAKEN_PAIRS.values())),
        name="kraken-ingest",
    )

    settlement_ingestor = SettlementIngestor(
        kalshi=kalshi, writer=writer,
        interval_sec=settings.settlement_poll_interval_sec,
    )
    settlement_ingestor.start()

    refit_scheduler = RefitScheduler(
        writer=writer, bias=bias,
        interval_sec=settings.calibration_refit_interval_sec,
        phase_gate_min_sample=settings.phase_gate_min_sample,
    )
    refit_scheduler.start()

    try:
        while not stop.is_set():
            try:
                await _discover_and_trade_once(kalshi, signal_gen, policy,
                                               control, writer, oracle,
                                               positions_provider)
            except Exception as e:
                log.warning("engine.cycle_error", error=str(e))
            try:
                await asyncio.wait_for(stop.wait(),
                                       timeout=settings.loop_interval_sec)
            except asyncio.TimeoutError:
                continue
    finally:
        kraken_task.cancel()
        try:
            await kraken_task
        except (asyncio.CancelledError, Exception):
            pass
        await refit_scheduler.stop()
        await settlement_ingestor.stop()
        await heart.stop()
        await writer.stop()
        await kalshi.close()
        log.info("engine.stopped")


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
