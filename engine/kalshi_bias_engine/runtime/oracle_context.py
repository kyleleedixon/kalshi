"""OracleContext provider for the runtime.

Wires the Kraken vol store + latest spot + basis stats into the shape the
KrakenDigitalOptionOracle expects, and hands out fresh snapshots on demand
without leaking mutable state into the oracle.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from ..ingest.realized_vol import MultiHorizonVol
from ..oracles.kraken_digital import OracleContext, SpotSnapshot


@dataclass
class OracleCtxHub:
    vol_store: MultiHorizonVol
    spot: dict[str, SpotSnapshot] = field(default_factory=dict)
    tail_inflation: dict[str, float] = field(default_factory=dict)
    basis_bps: dict[str, float] = field(default_factory=dict)
    basis_std_bps: dict[str, float] = field(default_factory=dict)

    def record_trade(self, underlying: str, ts_epoch: float, price: float) -> None:
        self.spot[underlying] = SpotSnapshot(price=price, ts_epoch=ts_epoch)
        self.vol_store.update(underlying, ts_epoch, price)

    def snapshot(self) -> OracleContext:
        return OracleContext(
            spot=dict(self.spot),
            vol_store=self.vol_store,
            tail_inflation=dict(self.tail_inflation),
            basis_bps=dict(self.basis_bps),
            basis_std_bps=dict(self.basis_std_bps),
            now_epoch=time.time(),
        )
