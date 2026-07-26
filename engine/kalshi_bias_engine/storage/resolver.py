"""FK resolution for the drain sink.

The trading loop only knows business keys — Kalshi ``contract_id`` (ticker),
UUID ``external_id`` for oracle estimates and signals — not database primary
keys, because those are only assigned when the drain sink commits. The
resolver turns business keys into pks at drain time.

Design notes
------------
* No in-process caching. Contract lookups are indexed and cheap (<10ms),
  and the drainer is a background task, not a hot path. Cached pks would
  need cache invalidation on commit failure, and getting that wrong risks
  cross-contract FK confusion — worse than an extra indexed SELECT.
* Every lookup happens inside the caller's ``Session`` so it sees the same
  transactional snapshot as the write itself. That is why the resolver
  takes a ``Session`` per call rather than opening its own.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Contract, OracleEstimate, Signal


class UnknownContract(Exception):
    """Raised when a payload references a contract_id that has not been
    upserted yet. The drainer will retry — the ``contract_upsert`` for
    the same contract should appear earlier in the spool."""


class UnknownOracleEstimate(Exception):
    """Raised when a signal payload references an oracle_estimate external_id
    that has not been drained yet. Should not happen if the loop enqueues
    the estimate before the signal."""


class UnknownSignal(Exception):
    """Raised when a paper_order payload references a signal external_id
    that has not been drained yet."""


class PkResolver:
    """Resolves business keys to primary keys inside a session."""

    def contract_pk(self, sess: Session, contract_id: str) -> int:
        pk = sess.execute(
            select(Contract.id).where(Contract.contract_id == contract_id)
        ).scalar_one_or_none()
        if pk is None:
            raise UnknownContract(contract_id)
        return pk

    def oracle_estimate_pk(self, sess: Session, external_id: str) -> int:
        pk = sess.execute(
            select(OracleEstimate.id).where(
                OracleEstimate.external_id == external_id
            )
        ).scalar_one_or_none()
        if pk is None:
            raise UnknownOracleEstimate(external_id)
        return pk

    def signal_pk(self, sess: Session, external_id: str) -> int:
        pk = sess.execute(
            select(Signal.id).where(Signal.external_id == external_id)
        ).scalar_one_or_none()
        if pk is None:
            raise UnknownSignal(external_id)
        return pk
