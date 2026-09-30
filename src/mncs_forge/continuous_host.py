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

from .config import ForgeConfig, load_config
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
    parser.add_argument(
        "--instance", help="provider reconciliation instance (selected bindings required)"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    config = load_config(args.config)
    lease_path = _supervisor_lease_path(config)
    pid = os.getpid()
    if args.instance:
        return _managed_run(config, args.instance, args.mode)
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
                "resource_state": forge._executor.resource_status()
                if callable(getattr(forge._executor, "resource_status", None))
                else {"state": "unknown", "limitation": "Runner resource status unavailable"},
            },
        )
        supervisor.run(once=False)
        return 0
    finally:
        current = _read_json_path(lease_path)
        if current and int(current.get("pid", 0) or 0) == pid:
            lease_path.unlink(missing_ok=True)


def _managed_run(config: ForgeConfig, instance: str, mode: str) -> int:
    """Reuse the canonical supervisor; only its resident transport is new."""
    from .continuous import ensure_language_service
    from .resident import ResidentEndpoint, language_bindings, selected_identity

    lease_path = _supervisor_lease_path(config)
    deadline = time.monotonic() + 2
    lease = _read_json_path(lease_path) or {}
    while lease.get("instance") != instance and time.monotonic() < deadline:
        time.sleep(0.01)
        lease = _read_json_path(lease_path) or {}
    if lease.get("instance") != instance:
        return 2
    endpoint = None
    supervisor = None
    stopped = False

    def stop(_signum: int, _frame: object) -> None:
        nonlocal stopped
        stopped = True
        if supervisor is not None:
            supervisor.request_stop()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        identity = selected_identity(config)
        if identity != lease.get("provider_identity"):
            raise ValueError("selected bindings changed during resident launch")
        endpoint = ResidentEndpoint(config, identity, instance)
        ensure_language_service(config, selected_bindings=language_bindings(identity))
        if stopped:
            return 0
        forge = Forge(config, mode=mode)
        if selected_identity(config) != identity:
            raise ValueError("selected provider/runtime changed during initialization")
        supervisor = ContinuousSupervisor(forge)
        endpoint.supervisor = supervisor
        if stopped:
            return 0
        _write_json_path(lease_path, {**lease, "phase": "running"})
        result = supervisor.run(once=False)
        if not stopped:
            raise ValueError(
                f"continuous loop exited before stop: {result.get('transport', 'UNKNOWN')}"
            )
        return 0
    except Exception as error:
        _write_json_path(
            lease_path,
            {
                **lease,
                "phase": "failed",
                "error": {
                    "code": getattr(error, "code", "RESIDENT_START_FAILED"),
                    "message": str(error)[:512],
                },
            },
        )
        return 2
    finally:
        if endpoint is not None:
            endpoint.close()
        current = _read_json_path(lease_path) or {}
        if current.get("instance") == instance and current.get("phase") != "failed":
            lease_path.unlink(missing_ok=True)


if __name__ == "__main__":  # pragma: no cover - exercised by lifecycle CLI
    raise SystemExit(main())
