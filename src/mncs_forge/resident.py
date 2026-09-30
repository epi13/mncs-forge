"""Bounded resident transport, binding identity, and lifecycle delegation.

Linux process birth identity prevents PID reuse from authorizing control. A
live challenge proves that the selected supervisor responds, rather than
treating its lease as readiness evidence. Assurance remains native Forge.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, cast

from filelock import FileLock, Timeout

from .continuous import (
    _language_service_lease_path,
    _lifecycle_dir,
    _probe_language_service,
    _read_json_path,
    _supervisor_lease_path,
    _write_json_path,
)
from .errors import ForgeError
from .workspace_binding import resolve_continuous_config

STATUS_SCHEMA = "mncs.forge.resident-status/1"
RECONCILE_SCHEMA = "mncs.forge.resident-reconciliation/1"
MAX_BYTES = 8192


def process_identity(pid: int) -> dict[str, str] | None:
    """Linux-only birth identity; unsupported/unreadable is never ownership."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        fields = stat[stat.rindex(")") + 2 :].split()
        if fields[0] == "Z":
            return None
        return {
            "boot": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
            "start_ticks": fields[19],
        }
    except (OSError, ValueError, IndexError):
        return None


def owns_process(lease: dict[str, Any]) -> bool:
    pid = lease.get("pid")
    birth = lease.get("process_identity")
    return (
        type(pid) is int and pid > 0 and isinstance(birth, dict) and process_identity(pid) == birth
    )


def signal_owned(lease: dict[str, Any]) -> None:
    """Pin the process before verification/control; never signal a recycled PID."""
    if not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal"):
        raise ForgeError("RESIDENT_CONTROL_UNSUPPORTED", "Linux pidfd control is unavailable")
    descriptor = os.pidfd_open(lease["pid"])
    try:
        if not owns_process(lease):
            raise ForgeError(
                "RESIDENT_PROCESS_CHANGED", "process birth identity changed before control"
            )
        signal.pidfd_send_signal(descriptor, signal.SIGTERM)
    finally:
        os.close(descriptor)


def selected_identity(config: Any) -> dict[str, Any]:
    checkout = Path(__file__).resolve().parents[2]
    try:
        revision = subprocess.run(
            ["git", "-C", str(checkout), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=0.5,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError) as error:
        raise ForgeError(
            "RESIDENT_PROVIDER_IDENTITY", "cannot observe selected Forge HEAD"
        ) from error
    runtime = {}
    for name in (
        "MNCS_LANGUAGE_ROOT",
        "MNCS_BIN",
        "MNCS_EMBED_LIB",
        "MNCS_STORE_ROOT",
        "MNCS_LANGUAGE_SERVICE_HOST",
    ):
        raw = os.environ.get(name)
        if not raw or not Path(raw).is_absolute() or not Path(raw).exists():
            raise ForgeError(
                "RESIDENT_BINDING_MISSING",
                f"select an existing absolute {name}; ambient fallback is forbidden",
            )
        runtime[name] = str(Path(raw).resolve())
    language = Path(runtime["MNCS_LANGUAGE_ROOT"])
    artifacts = {}
    for name in ("MNCS_BIN", "MNCS_EMBED_LIB", "MNCS_LANGUAGE_SERVICE_HOST"):
        path = Path(runtime[name])
        if not path.is_file() or not os.access(
            path, os.R_OK if name == "MNCS_EMBED_LIB" else os.X_OK
        ):
            raise ForgeError("RESIDENT_BINDING_MISSING", f"selected {name} is not a usable file")
        with path.open("rb") as artifact:
            artifacts[name] = hashlib.file_digest(artifact, "sha256").hexdigest()
    for name in ("MNCS_BIN", "MNCS_EMBED_LIB"):
        if not Path(runtime[name]).is_relative_to(language):
            raise ForgeError(
                "RESIDENT_BINDING_MISMATCH", f"{name} is outside the selected Language checkout"
            )
    if not (Path(runtime["MNCS_STORE_ROOT"]) / "python/mncs_store").is_dir():
        raise ForgeError("RESIDENT_BINDING_MISSING", "selected Store package is unavailable")
    identity = {
        "checkout": str(checkout),
        "revision": revision,
        "workspace_root": str(config.root),
        "project_identity": config.project_identity,
        "config_path": str(config.config_path),
        "configuration_identity": hashlib.sha256(
            json.dumps(config.raw, sort_keys=True).encode()
        ).hexdigest(),
        "runtime": runtime,
        "runtime_artifacts": artifacts,
    }
    if len(json.dumps(identity).encode()) > MAX_BYTES // 2:
        raise ForgeError("RESIDENT_BINDING_LIMIT", "selected binding identity exceeds 4096 bytes")
    return identity


def language_bindings(identity: dict[str, Any]) -> dict[str, Any]:
    return {**identity["runtime"], "artifact_identities": identity.get("runtime_artifacts", {})}


def selected_environment(identity: dict[str, Any]) -> dict[str, str]:
    """Pin child imports and all existing Forge runtime selectors."""
    environment = dict(os.environ)
    environment.update(identity["runtime"])
    environment.update(
        MNCS_CLI=environment["MNCS_BIN"],
        MNLS_LANGUAGE_SERVICE_HOST=environment["MNCS_LANGUAGE_SERVICE_HOST"],
        MNCS_LIBRARY_PATH=str(Path(environment["MNCS_LANGUAGE_ROOT"]) / "library"),
        PYTHONPATH=os.pathsep.join(
            [
                str(Path(identity["checkout"]) / "src"),
                str(Path(environment["MNCS_STORE_ROOT"]) / "python"),
            ]
        ),
    )
    environment.pop("MNCS_FORGE_NATIVE_MODE", None)
    return environment


def _socket_address(config: Any) -> str:
    # Linux abstract socket avoids Unix path-length dependence on campaign roots.
    return "\0mncs-forge-" + hashlib.sha256(str(_lifecycle_dir(config)).encode()).hexdigest()[:32]


def _receive(connection: socket.socket) -> dict[str, Any]:
    data = bytearray()
    deadline = time.monotonic() + 0.35
    while b"\n" not in data:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ValueError("resident transport deadline exceeded")
        connection.settimeout(min(connection.gettimeout() or remaining, remaining))
        chunk = connection.recv(min(1024, MAX_BYTES + 1 - len(data)))
        if not chunk or len(data) > MAX_BYTES:
            raise ValueError("resident response is incomplete or exceeds its bound")
        data.extend(chunk)
        if len(data) > MAX_BYTES:
            raise ValueError("resident response exceeds its bound")
    value = json.loads(data.split(b"\n", 1)[0])
    if not isinstance(value, dict):
        raise ValueError("resident response is not an object")
    return cast(dict[str, Any], value)


def _lease(config: Any) -> dict[str, Any] | None:
    path = _supervisor_lease_path(config)
    try:
        with path.open("rb") as source:
            raw = source.read(MAX_BYTES + 1)
    except FileNotFoundError:
        return None
    try:
        if len(raw) > MAX_BYTES:
            raise ValueError("lease exceeds its bound")
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError("lease is not an object")
        return cast(dict[str, Any], value)
    except ValueError as error:
        raise ForgeError(
            "RESIDENT_LEASE_INVALID", "repair malformed resident lease; no process was controlled"
        ) from error


class ResidentEndpoint:
    """Read-only challenge responder attached to the actual supervisor."""

    def __init__(self, config: Any, identity: dict[str, Any], instance: str):
        self.identity = identity
        self.instance = instance
        self.supervisor: Any = None
        self.stopping = threading.Event()
        self.socket = socket.socket(socket.AF_UNIX)
        self.socket.bind(_socket_address(config))
        self.socket.listen(4)
        self.socket.settimeout(0.1)
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self) -> None:
        while not self.stopping.is_set():
            try:
                connection, _ = self.socket.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            with connection:
                connection.settimeout(0.2)
                try:
                    request = _receive(connection)
                    supervisor = self.supervisor
                    response = {
                        "nonce": request.get("nonce"),
                        "identity": self.identity,
                        "instance": self.instance,
                        "pid": os.getpid(),
                        "state": "ready"
                        if supervisor is not None
                        and supervisor.stream_identity
                        and not supervisor._stop_requested
                        else "starting",
                        "stream_identity": supervisor.stream_identity if supervisor else None,
                        "generation": supervisor.current_generation if supervisor else None,
                        "cursor": supervisor.current_cursor if supervisor else None,
                    }
                    connection.sendall(json.dumps(response, sort_keys=True).encode() + b"\n")
                except (OSError, ValueError):
                    pass

    def close(self) -> None:
        self.stopping.set()
        self.socket.close()
        self.thread.join(timeout=0.5)


def resident_status(config: Any, identity: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {
        "schema_version": STATUS_SCHEMA,
        "state": "stopped",
        "selected": identity,
        "observed": None,
        "diagnostics": [],
        "recovery": "invoke resident-reconcile with the same selected bindings and configuration",
    }
    lease = _lease(config)
    if not lease:
        return result
    if not owns_process(lease):
        return {
            **result,
            "state": "failed" if lease.get("error") else "stale",
            "diagnostics": [
                lease.get("error")
                or {
                    "code": "RESIDENT_STALE_PROCESS",
                    "message": "lease has no matching Linux process birth identity",
                }
            ],
        }
    if lease.get("provider_identity") != identity:
        return {
            **result,
            "state": "incompatible",
            "observed": lease.get("provider_identity"),
            "diagnostics": [
                {
                    "code": "RESIDENT_SELECTION_MISMATCH",
                    "message": "resident has a different provider/runtime/configuration binding; "
                    "stop it through its owning provider",
                }
            ],
        }
    nonce = uuid.uuid4().hex
    try:
        with socket.socket(socket.AF_UNIX) as client:
            client.settimeout(0.35)
            client.connect(_socket_address(config))
            client.sendall(json.dumps({"nonce": nonce}).encode() + b"\n")
            observed = _receive(client)
        if (
            observed.get("nonce") != nonce
            or observed.get("identity") != identity
            or observed.get("pid") != lease.get("pid")
            or observed.get("instance") != lease.get("instance")
        ):
            raise ValueError("live resident challenge does not match the selected lease")
        result.update(state=observed["state"], observed=observed)
        observed.pop("nonce", None)
        if observed["state"] == "starting" and time.monotonic() - float(
            lease.get("started_monotonic", 0)
        ) >= float(config.continuous_settings.get("start_timeout_seconds", 60)):
            raise ValueError("resident initialization exceeded the configured startup deadline")
        if observed["state"] == "ready":
            language_lease = _read_json_path(_language_service_lease_path(config)) or {}
            if not owns_process(language_lease) or language_lease.get(
                "selected_bindings"
            ) != language_bindings(identity):
                raise ValueError(
                    "resident Language Service has no matching selected process binding"
                )
            language = _probe_language_service(config)
            if not language.get("stream_identity") or language.get(
                "stream_identity"
            ) != observed.get("stream_identity"):
                raise ValueError("Forge has not attached to the current Language Service stream")
            result["language_service"] = {
                "stream_identity": language["stream_identity"],
                "workspace_root": language["workspace_root"],
            }
    except (OSError, ValueError, ForgeError) as error:
        result.update(
            state="degraded",
            diagnostics=[{"code": "RESIDENT_PROBE_FAILED", "message": str(error)[:512]}],
        )
        if lease.get("phase") == "starting" and time.monotonic() - float(
            lease.get("started_monotonic", 0)
        ) < float(config.continuous_settings.get("start_timeout_seconds", 60)):
            result["state"] = "starting"
    return result


def resident_reconcile(
    config: Any,
    identity: dict[str, Any],
    *,
    stop: bool = False,
    include_language_service: bool = False,
) -> dict[str, Any]:
    """Bounded asynchronous start under the same provider lifecycle lock."""
    lifecycle = _lifecycle_dir(config)
    lifecycle.mkdir(parents=True, exist_ok=True)
    try:
        with FileLock(str(lifecycle / "lifecycle.lock"), timeout=0):
            status = resident_status(config, identity)
            if not stop and status["state"] in {"ready", "starting"}:
                return {
                    "schema_version": RECONCILE_SCHEMA,
                    "operation": "reused",
                    "status": status,
                }
            lease = _lease(config) or {}
            if (
                lease
                and not isinstance(lease.get("process_identity"), dict)
                and type(lease.get("pid")) is int
                and process_identity(lease["pid"]) is not None
            ):
                return {
                    "schema_version": RECONCILE_SCHEMA,
                    "operation": "blocked",
                    "status": {
                        **status,
                        "diagnostics": [
                            {
                                "code": "RESIDENT_LEGACY_LEASE",
                                "message": "live legacy lease has no birth identity; "
                                "stop it through its owning lifecycle before reconciliation",
                            }
                        ],
                    },
                }
            if status["state"] == "incompatible" and any(
                (lease.get("provider_identity") or {}).get(key) != identity[key]
                for key in ("checkout", "config_path", "workspace_root")
            ):
                return {
                    "schema_version": RECONCILE_SCHEMA,
                    "operation": "blocked",
                    "status": status,
                }
            prior_identity = lease.get("provider_identity") or {}
            language = _read_json_path(_language_service_lease_path(config)) or {}
            changed_runtime = prior_identity.get("runtime") != identity[
                "runtime"
            ] or prior_identity.get("runtime_artifacts") != identity.get("runtime_artifacts")
            if (
                changed_runtime
                and owns_process(language)
                and all(
                    prior_identity.get(key) == identity[key]
                    for key in ("checkout", "config_path", "workspace_root")
                )
            ) and language.get("selected_bindings") in (
                prior_identity.get("runtime"),
                language_bindings(prior_identity),
            ):
                signal_owned(language)
                if owns_process(lease):
                    signal_owned(lease)
                return {
                    "schema_version": RECONCILE_SCHEMA,
                    "operation": "stopping",
                    "status": status,
                }
            if owns_process(lease):
                if stop and include_language_service:
                    language = _read_json_path(_language_service_lease_path(config)) or {}
                    if owns_process(language):
                        if language.get("selected_bindings") != language_bindings(identity):
                            raise ForgeError(
                                "LANGUAGE_SERVICE_SELECTION_MISMATCH",
                                "cannot stop a Language Service owned by another binding",
                            )
                        signal_owned(language)
                # A nonresponding selected process must exit before a replacement
                # is launched. Never control an incompatible/legacy PID lease.
                signal_owned(lease)
                return {
                    "schema_version": RECONCILE_SCHEMA,
                    "operation": "stopping",
                    "status": status,
                }
            if stop:
                return {
                    "schema_version": RECONCILE_SCHEMA,
                    "operation": "stopped",
                    "status": status,
                }
            if not config.continuous_settings.get("enabled"):
                raise ForgeError(
                    "CONTINUOUS_DISABLED",
                    "enable continuous mode in the selected Forge configuration",
                )
            instance = uuid.uuid4().hex
            log = (lifecycle / "supervisor.log").open("ab")
            try:
                child = subprocess.Popen(
                    [
                        sys.executable,
                        "-m",
                        "mncs_forge.continuous_host",
                        "--config",
                        str(config.config_path),
                        "--instance",
                        instance,
                    ],
                    cwd=config.root,
                    env=selected_environment(identity),
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                    close_fds=True,
                )
            finally:
                log.close()
            birth = process_identity(child.pid)
            if birth is None:
                child.terminate()
                child.wait(timeout=0.5)
                raise ForgeError(
                    "RESIDENT_PROCESS_IDENTITY", "Linux process birth identity is unavailable"
                )
            _write_json_path(
                _supervisor_lease_path(config),
                {
                    "pid": child.pid,
                    "process_identity": birth,
                    "provider_identity": identity,
                    "instance": instance,
                    "phase": "starting",
                    "started_monotonic": time.monotonic(),
                    "workspace_root": str(config.root),
                    "config_path": str(config.config_path),
                },
            )
            return {
                "schema_version": RECONCILE_SCHEMA,
                "operation": "started",
                "status": resident_status(config, identity),
            }
    except Timeout:
        return {
            "schema_version": RECONCILE_SCHEMA,
            "operation": "busy",
            "status": resident_status(config, identity),
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("status", "reconcile", "stop"))
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--workspace", type=Path)
    parser.add_argument("--include-language-service", action="store_true")
    args = parser.parse_args(argv)
    try:
        config = resolve_continuous_config(workspace=args.workspace, explicit_config=args.config)
        identity = selected_identity(config)
        result = (
            resident_status(config, identity)
            if args.operation == "status"
            else resident_reconcile(
                config,
                identity,
                stop=args.operation == "stop",
                include_language_service=args.include_language_service,
            )
        )
    except (ForgeError, OSError, ValueError) as error:
        result = {
            "schema_version": STATUS_SCHEMA if args.operation == "status" else RECONCILE_SCHEMA,
            "state": "blocked",
            "diagnostics": [
                {"code": getattr(error, "code", "RESIDENT_TRANSPORT"), "message": str(error)[:512]}
            ],
        }
    print(json.dumps(result, sort_keys=True))
    return 0
