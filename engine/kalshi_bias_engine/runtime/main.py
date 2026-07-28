"""Engine entrypoint (Phase 1 paper).

Wires together the crypto domain, Kraken vol pipeline, Kalshi read-only
client, oracle, bias model, signal generator, and PaperExecutionPolicy.

Does NOT import ``execution.live`` — that package raises ImportError by
design in Phase 1.
"""

from __future__ import annotations

import asyncio
import random
import signal
import uuid
from datetime import datetime, timezone
from typing import Any

import structlog

from .. import __version__
from ..core.estimate import StalenessReason
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


def _should_persist_signal(s, settings) -> bool:
    """Persistence gate — only spool signals worth keeping.

    Drops:
      * stale signals (no informational value)
      * FRESH signals close to Kalshi mid AND outside top-N rank
        (kept probabilistically for baseline calibration coverage)
    """
    if s.raw.staleness is not StalenessReason.FRESH:
        return False
    bid, ask = s.book.bid, s.book.ask
    if bid is not None and ask is not None:
        mid = 0.5 * (bid + ask)
        if abs(s.raw.p - mid) >= settings.persist_min_edge_gap:
            return True
    if s.rank is not None and s.rank <= settings.persist_top_n_rank:
        return True
    return random.random() < settings.persist_sample_rate


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

    Persistence gate: contract_upsert/quote are buffered per-contract and
    only flushed to the spool for signals that pass ``_should_persist_signal``.
    Skipping the 90%+ of near-mid, unranked contracts keeps Neon write
    volume manageable while preserving the training set.
    """
    settings = get_settings()
    report = load_latest_report(default_min_sample=settings.phase_gate_min_sample)
    kill_switch = control.get().kill_switch_active

    mapper = DomainRegistry.get("crypto").mapper
    picks: list[tuple[Any, BookSnapshot]] = []
    pending: dict[str, tuple[dict, dict]] = {}  # contract_id -> (upsert, quote)

    series_tickers = mapper.discovery_series_tickers()
    if series_tickers:
        market_iters = [
            kalshi.iter_markets(status="open", series_ticker=st)
            for st in series_tickers
        ]
    else:
        market_iters = [kalshi.iter_markets(status="open")]

    for it in market_iters:
        async for m in it:
            if not mapper.matches(m):
                continue
            contract = mapper.to_contract(m)
            if contract is None:
                continue
            book = _book_from_market_row(m)
            if book is None:
                continue
            if book.bid is None or book.ask is None:
                continue

            pending[contract.contract_id] = (
                _contract_upsert_payload(contract),
                {
                    "contract_id": contract.contract_id,
                    "bid": book.bid,
                    "ask": book.ask,
                    "bid_size": book.bid_size,
                    "ask_size": book.ask_size,
                    "last_trade_price": None,
                    "data_ts": book.data_ts.isoformat(),
                    "ingest_ts": datetime.now(timezone.utc).isoformat(),
                },
            )
            picks.append((contract, book))

    signals = await signal_gen.generate(picks, report)
    persist_decisions = [_should_persist_signal(s, settings) for s in signals]
    n_persisted = sum(persist_decisions)
    log.info("loop.done", picks=len(picks), signals=len(signals),
             persisted=n_persisted,
             stale_reasons=_summarize_stale(signals))

    for s, keep in zip(signals, persist_decisions):
        if not keep:
            continue

        estimate_external_id = str(uuid.uuid4())
        signal_external_id = str(uuid.uuid4())

        upsert, quote = pending[s.contract.contract_id]
        await writer.enqueue("contract_upsert", upsert)
        await writer.enqueue("quote", quote)
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


def _summarize_stale(signals: list) -> dict[str, int]:
    from collections import Counter
    return dict(Counter(s.raw.staleness.value for s in signals))


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


def _book_from_market_row(m: dict[str, Any]) -> BookSnapshot | None:
    """Best bid/ask straight off Kalshi's /markets row.

    Row shape (current): yes_bid_dollars, yes_ask_dollars, no_bid_dollars,
    no_ask_dollars as decimal strings/floats in [0, 1]; *_size_fp for sizes.
    A zero bid/ask means "no live order" — treat as None so downstream mid()
    guards work.
    """
    def _num(v: Any) -> float | None:
        if v is None or v == "":
            return None
        try:
            f = float(v)
        except (TypeError, ValueError):
            return None
        return f if f > 0.0 else None

    yes_bid = _num(m.get("yes_bid_dollars"))
    yes_ask = _num(m.get("yes_ask_dollars"))
    no_bid = _num(m.get("no_bid_dollars"))
    # YES ask can also be derived from NO bid: ask_yes = 1 - no_bid. Prefer
    # the direct yes_ask when Kalshi publishes it, else fall back.
    bid = yes_bid
    ask = yes_ask if yes_ask is not None else (
        (1.0 - no_bid) if no_bid is not None else None
    )
    if bid is None and ask is None:
        return None
    bid_size = _num(m.get("yes_bid_size_fp"))
    ask_size = _num(m.get("yes_ask_size_fp"))
    return BookSnapshot(
        bid=bid, ask=ask,
        bid_size=bid_size, ask_size=ask_size,
        data_ts=datetime.now(timezone.utc),
    )


def _parse_book(ob: dict[str, Any]) -> BookSnapshot | None:
    # Kalshi orderbook response (current shape):
    #   {"orderbook_fp": {"yes_dollars": [["0.01", "size"], ...],
    #                      "no_dollars":  [["0.01", "size"], ...]}}
    # Prices are strings in dollars [0, 1]; each list holds bids for that side.
    # Best YES bid = max yes price. YES ask is derived from best NO bid:
    # ask_yes = 1 - best_no_bid.
    ob_root = ob.get("orderbook_fp") or ob.get("orderbook") or {}
    yes = ob_root.get("yes_dollars") or ob_root.get("yes") or []
    no = ob_root.get("no_dollars") or ob_root.get("no") or []
    yes_price, yes_size = _best_bid(yes)
    no_price, no_size = _best_bid(no)
    bid = yes_price
    ask = None if no_price is None else (1.0 - no_price)
    ask_size = no_size
    if bid is None and ask is None:
        return None
    return BookSnapshot(
        bid=bid, ask=ask,
        bid_size=yes_size, ask_size=ask_size,
        data_ts=datetime.now(timezone.utc),
    )


def _best_bid(levels: list) -> tuple[float | None, float | None]:
    """Best (highest) bid price and its size from a Kalshi orderbook side.

    Kalshi's ``*_dollars`` levels come as ``[["0.0100", "size"], ...]`` with
    prices as decimal strings already in dollar units. Some legacy
    ``yes``/``no`` fields carried integer cents; handle both by inferring
    scale from magnitude.
    """
    best_price: float | None = None
    best_size: float | None = None
    for row in levels or []:
        try:
            raw_price = float(row[0])
            size = float(row[1]) if len(row) > 1 else None
        except (TypeError, ValueError, IndexError):
            continue
        # If the value is > 1 assume cents (legacy shape); else dollars.
        price = raw_price / 100.0 if raw_price > 1.0 else raw_price
        if best_price is None or price > best_price:
            best_price = price
            best_size = size
    return best_price, best_size


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
                private_key_pem=settings.resolved_kalshi_private_key_pem(),
            ),
        ),
        spool=writer,
        record_raw_pulls=settings.persist_raw_pulls,
    )
    kraken_ws = KrakenWs(
        KrakenConfig(rest_base=settings.kraken_rest_base, ws_url=settings.kraken_ws_url),
        spool=writer,
        record_raw_pulls=settings.persist_raw_pulls,
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
