"""kbe CLI. Thin — most of the work is in ``main`` and Alembic."""

from __future__ import annotations

import argparse
import asyncio
import sys

from .main import run as run_engine
from .settings import get_settings


def _healthcheck() -> int:
    from ..storage.control import ControlReader
    from ..storage.db import get_engine

    try:
        eng = get_engine()
        with eng.connect() as c:
            c.exec_driver_sql("SELECT 1")
    except Exception as e:
        print(f"db: FAIL ({e})", file=sys.stderr)
        return 1
    print("db: ok")

    state = ControlReader().get()
    if not state.fresh:
        print("control: FAIL (fail-closed)", file=sys.stderr)
        return 2
    print(f"control: ok (kill_switch={state.kill_switch_active})")
    return 0


def _print_settings() -> int:
    s = get_settings()
    redacted = s.model_dump()
    for k in list(redacted):
        if "private_key" in k or "key_id" in k:
            v = redacted[k]
            redacted[k] = "<set>" if v else "<empty>"
    for k, v in sorted(redacted.items()):
        print(f"{k}: {v}")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(prog="kbe")
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("run", help="Start the engine loop (Phase 1 paper).")
    sub.add_parser("healthcheck", help="Verify DB + control-plane reachability.")
    sub.add_parser("settings", help="Print effective settings (redacted).")

    args = parser.parse_args()
    if args.cmd is None or args.cmd == "run":
        asyncio.run(run_engine())
    elif args.cmd == "healthcheck":
        sys.exit(_healthcheck())
    elif args.cmd == "settings":
        sys.exit(_print_settings())
    else:
        parser.error(f"unknown command: {args.cmd}")


if __name__ == "__main__":
    main()
