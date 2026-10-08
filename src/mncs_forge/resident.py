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
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any, cast

from filelock import FileLock, Timeout

from .continuous import (
    _language_service_lease_path,
    _language_service_socket,
    _lifecycle_dir,
    _probe_language_service,
    _read_json_path,
    _supervisor_lease_path,
    _write_json_path,
)
from .errors import ForgeError
from .mncs_native import selected_library_root
from .workspace_binding import resolve_continuous_config

STATUS_SCHEMA = "mncs.forge.resident-status/1"
RECONCILE_SCHEMA = "mncs.forge.resident-reconciliation/1"
MAX_BYTES = 8192
DEADLINE_SECONDS = 2.5


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
    library = selected_library_root(language)
    if library is None or not library.is_dir():
        raise ForgeError("RESIDENT_BINDING_MISSING", "selected stdlib library is unavailable")
    runtime["MNCS_LIBRARY_ROOT"] = str(library.resolve())
    stdlib_identity = hashlib.sha256(
        json.dumps(
            [
                [str(path.relative_to(library)), hashlib.sha256(path.read_bytes()).hexdigest()]
                for path in sorted(library.rglob("*.mncs"))
            ],
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
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
    # Optional provider composition is part of the resident identity, never
    # an unrecorded ambient selector inherited by a long-lived process.
    composition_keys = (
        "MNCS_COMPILER_CHECKOUT",
        "MNCS_COMPILER_PROBE",
        "MNCS_VM_CHECKOUT",
        "MNCS_VM_BIN",
    )
    selection = {name: os.environ.get(name) for name in composition_keys}
    if any(selection.values()):
        if not all(selection.values()):
            raise ForgeError(
                "RESIDENT_BINDING_MISSING", "incomplete compiler/VM provider composition"
            )
        for name, raw in selection.items():
            path = Path(raw)
            if not path.is_absolute() or not path.exists():
                raise ForgeError("RESIDENT_BINDING_MISSING", f"selected {name} is unavailable")
            runtime[name] = str(path.resolve())
        for name, owner in (
            ("MNCS_COMPILER_PROBE", "MNCS_COMPILER_CHECKOUT"),
            ("MNCS_VM_BIN", "MNCS_VM_CHECKOUT"),
        ):
            path = Path(runtime[name])
            if not path.is_relative_to(Path(runtime[owner])):
                raise ForgeError(
                    "RESIDENT_BINDING_MISMATCH", f"{name} is outside its selected provider checkout"
                )
            if not path.is_file() or not os.access(path, os.X_OK):
                raise ForgeError("RESIDENT_BINDING_MISSING", f"selected {name} is not executable")
            artifacts[name] = hashlib.sha256(path.read_bytes()).hexdigest()
        for name, relative in (
            ("MNCS_COMPILER_CHECKOUT", "tools/vm_provider.py"),
            ("MNCS_VM_CHECKOUT", "python/mncs_vm_client/__init__.py"),
        ):
            path = Path(runtime[name]) / relative
            artifacts[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
        runtime["MNCS_VM_ARTIFACT_CACHE"] = str(
            Path(
                os.environ.get(
                    "MNCS_VM_ARTIFACT_CACHE", str(Path(config.root) / ".mncs/cache/compiler-vm")
                )
            ).resolve()
        )
    service_selection = {
        name: os.environ.get(name)
        for name in (
            "MNLS_SERVICE_SOCKET",
            "MNLS_SERVICE_STREAM_IDENTITY",
            "MNLS_SERVICE_WORKSPACE_ROOT",
            "MNLS_SERVICE_REPOSITORY_ROOTS_JSON",
        )
    }
    if any(service_selection.values()):
        if not all(service_selection.values()):
            raise ForgeError(
                "RESIDENT_BINDING_MISSING",
                "incomplete selected Language Service endpoint identity",
            )
        socket_path = Path(str(service_selection["MNLS_SERVICE_SOCKET"]))
        workspace_root = Path(str(service_selection["MNLS_SERVICE_WORKSPACE_ROOT"]))
        try:
            repository_roots = json.loads(
                str(service_selection["MNLS_SERVICE_REPOSITORY_ROOTS_JSON"])
            )
        except json.JSONDecodeError as error:
            raise ForgeError(
                "RESIDENT_BINDING_MISMATCH",
                "selected Language Service repository roots are invalid",
            ) from error
        if (
            not socket_path.is_absolute()
            or not workspace_root.is_absolute()
            or not isinstance(repository_roots, list)
            or len(repository_roots) > 128
            or any(
                not isinstance(root, str) or not Path(root).is_absolute()
                for root in repository_roots
            )
            or str(config.root.resolve())
            not in {str(Path(root).resolve()) for root in repository_roots}
            or not str(service_selection["MNLS_SERVICE_STREAM_IDENTITY"]).strip()
        ):
            raise ForgeError(
                "RESIDENT_BINDING_MISMATCH",
                "selected Language Service endpoint does not include the Forge workspace",
            )
        if not socket_path.exists():
            raise ForgeError(
                "RESIDENT_BINDING_MISSING", "selected Language Service socket is absent"
            )
        runtime.update({name: str(value) for name, value in service_selection.items()})
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
        "effective_stdlib_content_identity": stdlib_identity,
    }
    if len(json.dumps(identity).encode()) > MAX_BYTES // 2:
        raise ForgeError("RESIDENT_BINDING_LIMIT", "selected binding identity exceeds 4096 bytes")
    return identity


def language_bindings(identity: dict[str, Any]) -> dict[str, Any]:
    return {**identity["runtime"], "artifact_identities": identity.get("runtime_artifacts", {})}


def selected_environment(identity: dict[str, Any]) -> dict[str, str]:
    """Pin child imports and all existing Forge runtime selectors."""
    environment = dict(os.environ)
    for name in (
        "MNCS_COMPILER_CHECKOUT",
        "MNCS_COMPILER_PROBE",
        "MNCS_VM_CHECKOUT",
        "MNCS_VM_BIN",
        "MNCS_VM_ARTIFACT_CACHE",
    ):
        environment.pop(name, None)
    environment.update(identity["runtime"])
    environment.update(
        MNCS_CLI=environment["MNCS_BIN"],
        MNLS_LANGUAGE_SERVICE_HOST=environment["MNCS_LANGUAGE_SERVICE_HOST"],
        MNCS_LIBRARY_PATH=environment["MNCS_LIBRARY_ROOT"],
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
        self.config = config
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
                    consumer = _continuous_consumer_status(self.config, supervisor)
                    active = (
                        supervisor is not None
                        and supervisor.stream_identity
                        and not supervisor._stop_requested
                    )
                    state = "starting"
                    if active:
                        state = {
                            "ready": "ready",
                            "blocked": "degraded",
                            "retry_required": "degraded",
                        }.get(str(consumer.get("state")), "starting")
                    response = {
                        "nonce": request.get("nonce"),
                        "identity": self.identity,
                        "instance": self.instance,
                        "pid": os.getpid(),
                        "state": state,
                        "stream_identity": supervisor.stream_identity if supervisor else None,
                        "generation": supervisor.current_generation if supervisor else None,
                        "cursor": supervisor.current_cursor if supervisor else None,
                        "continuous_consumer": consumer,
                    }
                    connection.sendall(json.dumps(response, sort_keys=True).encode() + b"\n")
                except (OSError, ValueError):
                    pass

    def close(self) -> None:
        self.stopping.set()
        self.socket.close()
        self.thread.join(timeout=0.5)


def _continuous_consumer_status(config: Any, supervisor: Any | None = None) -> dict[str, Any]:
    """Expose Forge's acknowledged Language Service position to Environment."""
    status_path = config.state_dir / "continuous" / "status.json"
    status = _read_json_path(status_path)
    if status is None:
        # Test doubles and a never-started service have no durable cursor yet.
        if supervisor is not None and not callable(getattr(supervisor, "_status_path", None)):
            return {
                "state": "ready" if getattr(supervisor, "stream_identity", None) else "starting",
                "stream_identity": getattr(supervisor, "stream_identity", None),
                "cursor": getattr(supervisor, "current_cursor", None),
            }
        return {"state": "starting", "reason": "no durable continuous cursor exists"}
    recovery = status.get("cursor_recovery")
    recovery_status = recovery.get("status") if isinstance(recovery, dict) else None
    if recovery_status == "required":
        state = "blocked"
    elif recovery_status == "owner_retry_required":
        state = "retry_required"
    elif supervisor is None:
        state = "unknown"
    elif (
        status.get("event_stream_identity") == getattr(supervisor, "stream_identity", None)
        and type(status.get("event_cursor")) is int
        and status.get("event_cursor") == getattr(supervisor, "current_cursor", None)
    ):
        state = "ready"
    else:
        state = "reconciling"
    return {
        "state": state,
        "stream_identity": status.get("event_stream_identity"),
        "cursor": status.get("event_cursor"),
        "cursor_recovery": recovery,
    }


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
        # A never-started resident still reports its (absent) consumer
        # position so readiness predicates evaluate to not-ready instead
        # of a schema-shaped hole that withholds recovery. Read-only: the
        # consumer helper never creates state for a missing status file.
        return {**result, "continuous_consumer": _continuous_consumer_status(config)}
    if not owns_process(lease):
        consumer = _continuous_consumer_status(config)
        diagnostics = [
            lease.get("error")
            or {
                "code": "RESIDENT_STALE_PROCESS",
                "message": "lease has no matching Linux process birth identity",
            }
        ]
        if consumer.get("state") in {"blocked", "retry_required"}:
            diagnostics.append(
                {
                    "code": "RESIDENT_EVENT_CURSOR_RECOVERY",
                    "message": "continuous owner did not acknowledge the durable event cursor",
                    "recovery": consumer.get("cursor_recovery"),
                }
            )
        return {
            **result,
            "state": "failed" if lease.get("error") else "stale",
            "observed": {"continuous_consumer": consumer},
            "continuous_consumer": consumer,
            "diagnostics": diagnostics,
        }
    if lease.get("provider_identity") != identity:
        consumer = _continuous_consumer_status(config)
        return {
            **result,
            "state": "incompatible",
            "observed": lease.get("provider_identity"),
            "continuous_consumer": consumer,
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
        result["continuous_consumer"] = observed.get("continuous_consumer")
        if observed["state"] == "degraded":
            consumer = observed.get("continuous_consumer")
            recovery = consumer.get("cursor_recovery") if isinstance(consumer, dict) else None
            result["diagnostics"] = [
                {
                    "code": "RESIDENT_EVENT_CURSOR_RECOVERY",
                    "message": "continuous owner did not acknowledge the durable event cursor",
                    "recovery": recovery,
                }
            ]
        if observed["state"] == "starting" and time.monotonic() - float(
            lease.get("started_monotonic", 0)
        ) >= float(config.continuous_settings.get("start_timeout_seconds", 60)):
            raise ValueError("resident initialization exceeded the configured startup deadline")
        if observed["state"] == "ready":
            external_service = "MNLS_SERVICE_SOCKET" in identity.get("runtime", {})
            if not external_service:
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
            observed = status.get("observed")
            consumer = status.get("continuous_consumer")
            if not isinstance(consumer, dict) and isinstance(observed, dict):
                consumer = observed.get("continuous_consumer")
            if isinstance(consumer, dict) and consumer.get("state") == "blocked":
                return {
                    "schema_version": RECONCILE_SCHEMA,
                    "operation": "blocked",
                    "status": {
                        **status,
                        "diagnostics": [
                            *status.get("diagnostics", []),
                            {
                                "code": "RESIDENT_EVENT_CURSOR_RECONCILIATION_REQUIRED",
                                "message": "preserve the acknowledged cursor until "
                                "the semantic owner completes bounded reconciliation",
                                "recovery": consumer.get("cursor_recovery"),
                            },
                        ],
                    },
                }
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
            language_stopping = False
            if stop and include_language_service and owns_process(language):
                if language.get("selected_bindings") != language_bindings(identity):
                    raise ForgeError(
                        "LANGUAGE_SERVICE_SELECTION_MISMATCH",
                        "cannot stop a Language Service owned by another binding",
                    )
                signal_owned(language)
                language_stopping = True
            elif (
                stop
                and include_language_service
                and type(language.get("pid")) is int
                and language.get("pid") > 0
                and process_identity(language["pid"]) is not None
            ):
                return {
                    "schema_version": RECONCILE_SCHEMA,
                    "operation": "blocked",
                    "status": {
                        **status,
                        "diagnostics": [
                            {
                                "code": "RESIDENT_LANGUAGE_LEASE_UNVERIFIED",
                                "message": "live Language Service lease lacks a matching birth "
                                "identity",
                            }
                        ],
                    },
                }
            elif (
                stop
                and include_language_service
                and language.get("owned_by_continuous") is True
                and type(language.get("pid")) is int
                and language.get("pid") > 0
                and process_identity(language["pid"]) is None
            ):
                if (
                    language.get("selected_bindings") != language_bindings(identity)
                    or language.get("workspace_root") != str(config.root.resolve())
                    or "MNLS_SERVICE_SOCKET" in identity.get("runtime", {})
                ):
                    return {
                        "schema_version": RECONCILE_SCHEMA,
                        "operation": "blocked",
                        "status": {
                            **status,
                            "diagnostics": [
                                {
                                    "code": "RESIDENT_LANGUAGE_LEASE_MISMATCH",
                                    "message": "stale Language Service lease does not match the "
                                    "selected Forge-owned binding",
                                }
                            ],
                        },
                    }
                socket_path = _language_service_socket(config)
                private_short_socket = False
                if socket_path.parent.exists() and not socket_path.parent.is_symlink():
                    try:
                        parent_stat = socket_path.parent.stat()
                        private_short_socket = (
                            socket_path.parent.parent == Path(tempfile.gettempdir())
                            and socket_path.name == "language.sock"
                            and socket_path.parent.name.startswith(f"mncs-forge-{os.getuid()}-")
                            and parent_stat.st_uid == os.getuid()
                            and parent_stat.st_mode & 0o077 == 0
                        )
                    except OSError:
                        private_short_socket = False
                if socket_path.is_symlink() or (
                    not socket_path.resolve().is_relative_to(config.root.resolve())
                    and not private_short_socket
                ):
                    return {
                        "schema_version": RECONCILE_SCHEMA,
                        "operation": "blocked",
                        "status": {
                            **status,
                            "diagnostics": [
                                {
                                    "code": "RESIDENT_LANGUAGE_SOCKET_MISMATCH",
                                    "message": "stale Language Service socket is outside the "
                                    "selected project boundary",
                                }
                            ],
                        },
                    }
                if socket_path.exists():
                    socket_stat = socket_path.lstat()
                    if not stat.S_ISSOCK(socket_stat.st_mode) or socket_stat.st_uid != os.getuid():
                        return {
                            "schema_version": RECONCILE_SCHEMA,
                            "operation": "blocked",
                            "status": {
                                **status,
                                "diagnostics": [
                                    {
                                        "code": "RESIDENT_LANGUAGE_SOCKET_OCCUPIED",
                                        "message": "stale lease socket path is occupied by a "
                                        "non-owned file",
                                    }
                                ],
                            },
                        }
                    try:
                        _probe_language_service(config)
                    except ForgeError as error:
                        if error.code != "LANGUAGE_SERVICE_UNAVAILABLE":
                            raise
                    else:
                        return {
                            "schema_version": RECONCILE_SCHEMA,
                            "operation": "blocked",
                            "status": {
                                **status,
                                "diagnostics": [
                                    {
                                        "code": "RESIDENT_LANGUAGE_SOCKET_RESPONDS",
                                        "message": "socket still responds after the owned process "
                                        "exited; preserve it for provider reconciliation",
                                    }
                                ],
                            },
                        }
                    socket_path.unlink()
                _language_service_lease_path(config).unlink(missing_ok=True)
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
                    "operation": "stopping" if language_stopping else "stopped",
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
    # Environment selects the exact workspace root and supplies it here.
    # The provider then uses its bounded, fail-closed config resolver instead
    # of requiring consumers to copy an ambient config path into their policy.
    parser.add_argument("--config", type=Path)
    parser.add_argument("--workspace", type=Path)
    parser.add_argument("--include-language-service", action="store_true")
    parser.add_argument(
        "--stop",
        action="store_true",
        help="stop a selected resident through the reconcile capability",
    )
    args = parser.parse_args(argv)
    if args.config is None and args.workspace is None:
        parser.error("supply the selected --workspace or an explicit --config")

    def deadline(_signal: int, _frame: Any) -> None:
        raise ForgeError("RESIDENT_DEADLINE", "resident operation exceeded its 2.5-second deadline")

    # This provider already requires Linux. Bound configuration/artifact reads
    # as well as the individual socket and Git operations, including direct
    # invocation without Environment's additional transport budget.
    previous_handler = signal.signal(signal.SIGALRM, deadline)
    signal.setitimer(signal.ITIMER_REAL, DEADLINE_SECONDS)
    try:
        config = resolve_continuous_config(workspace=args.workspace, explicit_config=args.config)
        identity = selected_identity(config)
        result = (
            resident_status(config, identity)
            if args.operation == "status"
            else resident_reconcile(
                config,
                identity,
                stop=args.operation == "stop" or args.stop,
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
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
    encoded = json.dumps(result, sort_keys=True)
    if len(encoded.encode()) + 1 > MAX_BYTES:
        encoded = json.dumps(
            {
                "schema_version": STATUS_SCHEMA if args.operation == "status" else RECONCILE_SCHEMA,
                "state": "blocked",
                "diagnostics": [
                    {
                        "code": "RESIDENT_OUTPUT_LIMIT",
                        "message": "resident result exceeds 8192 bytes",
                    }
                ],
            },
            sort_keys=True,
        )
    print(encoded)
    return 0
