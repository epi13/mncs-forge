"""Resident process entrypoint for the canonical continuous supervisor.

The process owns no policy of its own: it constructs Forge once and delegates
the entire lifetime to :class:`ContinuousSupervisor`.  The small lease file is
only lifecycle coordination metadata and is never semantic evidence.
"""

from __future__ import annotations

import argparse
import os
import signal
import time
from pathlib import Path

from .config import load_config
from .continuous import (
    CONTINUOUS_LIFECYCLE_SCHEMA,
    ContinuousSupervisor,
    _read_json_path,
    _supervisor_lease_path,
    _write_json_path,
)
from .engine import Forge


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mncs-forge-continuous-host")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--mode", choices=("development", "evaluator"), default="development")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    config = load_config(args.config)
    lease_path = _supervisor_lease_path(config)
    pid = os.getpid()
    _write_json_path(
        lease_path,
        {
            "schema_version": CONTINUOUS_LIFECYCLE_SCHEMA,
            "kind": "forge-continuous-supervisor",
            "pid": pid,
            "workspace_root": str(config.root),
            "config_path": str(config.config_path),
            "phase": "starting",
            "started_at": time.time(),
        },
    )
    supervisor: ContinuousSupervisor | None = None
    stop_before_ready = False

    def request_stop(_signum: int, _frame: object) -> None:
        nonlocal stop_before_ready
        if supervisor is None:
            stop_before_ready = True
        else:
            supervisor.request_stop()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    try:
        if stop_before_ready:
            return 0
        forge = Forge(config, mode=args.mode)
        supervisor = ContinuousSupervisor(forge)
        if stop_before_ready:
            return 0
        _write_json_path(
            lease_path,
            {
                "schema_version": CONTINUOUS_LIFECYCLE_SCHEMA,
                "kind": "forge-continuous-supervisor",
                "pid": pid,
                "workspace_root": str(config.root),
                "config_path": str(config.config_path),
                "phase": "running",
                "started_at": time.time(),
            },
        )
        supervisor.run(once=False)
        return 0
    finally:
        current = _read_json_path(lease_path)
        if current and int(current.get("pid", 0) or 0) == pid:
            lease_path.unlink(missing_ok=True)


if __name__ == "__main__":  # pragma: no cover - exercised by lifecycle CLI
    raise SystemExit(main())
