"""Runtime settings loaded from environment / .env.

The Phase field is the code-level phase gate: PAPER is the only value that
lets the engine start without a live-execution module present, and even in
LIVE mode per-domain unlock still requires calibration store approval
(section 6 of the spec).
"""

from __future__ import annotations

from enum import Enum
from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Phase(str, Enum):
    PAPER = "PAPER"
    LIVE = "LIVE"
    HALTED = "HALTED"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="KBE_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: str = Field(
        default="postgresql+psycopg://kbe:kbe@localhost:5432/kbe",
        description="Neon Postgres URL. psycopg (v3) driver.",
    )
    spool_path: str = Field(default="./spool.sqlite")

    kalshi_api_base: str = Field(
        default="https://api.elections.kalshi.com/trade-api/v2"
    )
    kalshi_key_id: str = Field(default="")
    kalshi_private_key_pem: str = Field(default="")
    kalshi_private_key_path: str = Field(
        default="",
        description="Optional path to Kalshi PEM. Takes precedence over inline PEM.",
    )

    def resolved_kalshi_private_key_pem(self) -> str:
        if self.kalshi_private_key_path:
            with open(self.kalshi_private_key_path) as f:
                return f.read()
        return self.kalshi_private_key_pem

    kraken_rest_base: str = Field(default="https://api.kraken.com")
    kraken_ws_url: str = Field(default="wss://ws.kraken.com/v2")

    engine_id: str = Field(default="kbe-local-1")
    loop_interval_sec: float = Field(default=5.0)
    heartbeat_interval_sec: float = Field(default=15.0)
    settlement_poll_interval_sec: float = Field(default=60.0)
    calibration_refit_interval_sec: float = Field(default=900.0)
    phase: Phase = Field(default=Phase.PAPER)

    # calibration gate defaults
    phase_gate_min_sample: int = Field(default=200)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
