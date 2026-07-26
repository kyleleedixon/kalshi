"""Storage layer.

Neon Postgres is the source of truth AND the control plane. The engine also
keeps a local SQLite write-spool so a Neon outage cannot block the trading
loop; spooled writes reconcile on reconnect. Kill-switch READS fail closed —
if Neon is unreachable we do NOT trade.
"""

from .models import Base
from .db import get_engine, session_scope

__all__ = ["Base", "get_engine", "session_scope"]
