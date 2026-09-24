"""Generation-bound continuous development supervision.

This module is deliberately a policy executor, not a semantic implementation:
Language Service events provide compiler facts, RAVEL selects tests, mncs-test
executes the selected identities, Doctor owns Safe repairs, and mncs-debug owns
failure evidence.  Forge only matches checked-in trigger declarations, binds
every job to a generation, and suppresses stale results.
"""

from __future__ import annotations

import json
import os
import queue
import shlex
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections import Counter, OrderedDict, deque
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote, urlparse

from filelock import FileLock, Timeout

from .application.support import now
from .errors import ForgeError
from .mncs_native import NativeForgeAdapter
from .records import RecordType, new_record
from .serialization import local_json_identity

if TYPE_CHECKING:
    from .engine import Forge

CONTINUOUS_STATUS_SCHEMA = "mncs.continuous-status/1"
REPAIR_RESULT_SCHEMA = "mncs.continuous-repair/1"
CONTINUOUS_LIFECYCLE_SCHEMA = "mncs.continuous-lifecycle/1"
ENVIRONMENT_ENTRY_SCHEMA = "mncs.environment-entry/1"
CONTINUOUS_RECENT_RESULTS = 32
CONTINUOUS_ATTENTION_CAPACITY = 64
CONTINUOUS_REPAIR_CAPACITY = 32
CONTINUOUS_SELECTED_TEST_CAPACITY = 256
CONTINUOUS_PENDING_CAPACITY = 128
CONTINUOUS_EVENT_BATCH_CAPACITY = 64
CONTINUOUS_EVENT_VERIFIER_CAPACITY = 16
CONTINUOUS_STATUS_MAX_BYTES = 8 * 1024 * 1024
_COUNTER_MAX = (1 << 63) - 1


def _mapping(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: object) -> list[str]:
    return [str(item) for item in value] if isinstance(value, list) else []


def _compact_resource_evidence(value: object) -> dict[str, object]:
    evidence = _mapping(value)
    compact = {
        key: evidence[key]
        for key in (
            "resource_envelope_identity",
            "unit_identity",
            "resource_exhausted",
            "resource_metric",
            "resource_bound",
            "resource_observed",
            "cleanup_succeeded",
            "systemd_result",
            "command_returncode",
            "systemd_wrapper_returncode",
            "resource_outcome",
            "native_execution_returncode_available",
            "native_execution_returncode",
            "cancellation_requested",
            "superseded",
            "process_status",
            "process_observation_complete",
            "tree_empty",
            "launcher_reaped",
            "cleanup_complete",
            "cancellation_complete",
            "verification_deferred",
            "native_admission_reason",
            "resource_admission_status",
            "deferred",
            "limitation",
        )
        if key in evidence
    }
    for key, capacity in (
        ("resource_envelope_identity", 128),
        ("unit_identity", 128),
        ("resource_metric", 64),
        ("systemd_result", 64),
        ("limitation", 512),
    ):
        if isinstance(compact.get(key), str):
            compact[key] = str(compact[key])[:capacity]
    context = _mapping(evidence.get("job_context"))
    if context:
        bounded_context: dict[str, object] = {}
        for key, capacity in (
            ("identity", 128),
            ("generation", None),
            ("source_identity", 256),
            ("candidate_identity", 256),
            ("cursor", None),
            ("trigger_id", 128),
            ("action", 64),
            ("verifier_count", None),
        ):
            item = context.get(key)
            if capacity is None and isinstance(item, int) and not isinstance(item, bool):
                bounded_context[key] = item
            elif capacity is not None and isinstance(item, str):
                bounded_context[key] = item[:capacity]
        verifier_ids = context.get("verifier_ids")
        if isinstance(verifier_ids, list):
            bounded_context["verifier_ids"] = [str(item)[:128] for item in verifier_ids[:32]]
        compact["job_context"] = bounded_context
    resource_observations = _mapping(evidence.get("resource_observations"))
    if resource_observations:
        compact["resource_observations"] = {
            key: resource_observations[key]
            for key in (
                "exit_status",
                "timed_out",
                "output_limited",
                "cleanup_known",
                "cleanup_succeeded",
                "cancellation_requested",
                "superseded",
                "memory_current_bytes",
                "cgroup_memory_peak_bytes",
                "process_rss_peak_bytes",
                "process_count_current",
                "process_count_peak",
                "memory_max_events",
                "memory_oom_events",
                "memory_oom_kill_events",
                "memory_oom_group_kill_events",
                "memory_high_events",
                "process_limit_events",
                "cpu_time_microseconds",
            )
            if key in resource_observations
        }
    return compact


def _compact_verification_result(value: object) -> dict[str, object]:
    result = _mapping(value)
    keep = (
        "obligation_identity",
        "verifier_id",
        "status",
        "reused",
        "error_code",
        "resource_exhausted",
        "resource_envelope_identity",
        "resource_metric",
        "resource_bound",
        "resource_observed",
        "cleanup_succeeded",
        "cancellation_requested",
        "superseded",
        "generation",
        "source_identity",
        "source_uri",
        "candidate_identity",
        "trigger_id",
        "action",
        "cursor",
    )
    compact = {key: result[key] for key in keep if key in result}
    for key, capacity in (
        ("obligation_identity", 128),
        ("source_identity", 256),
        ("source_uri", 1024),
        ("candidate_identity", 256),
        ("trigger_id", 128),
        ("action", 64),
    ):
        if isinstance(compact.get(key), str):
            compact[key] = str(compact[key])[:capacity]
    verifier_id = result.get("verifier_id")
    if isinstance(verifier_id, str):
        compact["verifier_id"] = verifier_id[:128]
    for key, capacity in (
        ("resource_envelope_identity", 128),
        ("resource_metric", 64),
    ):
        if isinstance(compact.get(key), str):
            compact[key] = str(compact[key])[:capacity]
    if isinstance(compact.get("status"), str):
        compact["status"] = str(compact["status"])[:32]
    if isinstance(compact.get("error_code"), str):
        compact["error_code"] = str(compact["error_code"])[:128]
    operational_error = _mapping(result.get("operational_error"))
    if isinstance(operational_error.get("code"), str):
        compact["error_code"] = str(operational_error["code"])[:128]
    for key in ("reason", "reuse_blocked"):
        if isinstance(result.get(key), str):
            compact[key] = str(result[key])[:256]
    extensions = _mapping(result.get("extensions"))
    mncs_extensions = _mapping(extensions.get("mncs_forge"))
    resource_evidence = mncs_extensions.get("resource_evidence") or result.get("resource_evidence")
    if resource_evidence:
        compact["resource_evidence"] = _compact_resource_evidence(resource_evidence)
    return compact


def _compact_event_result(value: dict[str, object]) -> dict[str, object]:
    """Retain a bounded status projection; complete evidence stays in its owner."""

    compact: dict[str, object] = {
        key: value[key]
        for key in ("generation", "cursor", "status", "repair_pending_rebound")
        if key in value
    }
    for key in ("reason",):
        if isinstance(value.get(key), str):
            compact[key] = str(value[key])[:256]
    actions = value.get("actions")
    if isinstance(actions, list):
        compact_actions: list[dict[str, object]] = []
        for action in actions[:8]:
            if not isinstance(action, dict):
                continue
            entry: dict[str, object] = {}
            for key, capacity in (("trigger", 128), ("action", 64)):
                if isinstance(action.get(key), str):
                    entry[key] = str(action[key])[:capacity]
            result = _mapping(action.get("result"))
            if isinstance(result.get("results"), list):
                entry["result"] = {
                    "status": result.get("status"),
                    "tier": result.get("tier"),
                    "results": [
                        _compact_verification_result(item)
                        for item in result["results"][:16]
                        if isinstance(item, dict)
                    ],
                }
            elif result.get("repair") is not None:
                repair = _mapping(result.get("repair"))
                compact_repair: dict[str, object] = {}
                for key in ("original_source_identity", "resulting_source_identity"):
                    if isinstance(repair.get(key), str):
                        compact_repair[key] = str(repair[key])[:256]
                if isinstance(repair.get("repair_rounds"), int):
                    compact_repair["repair_rounds"] = repair["repair_rounds"]
                if isinstance(repair.get("failure_conflict_reason"), str):
                    compact_repair["failure_conflict_reason"] = str(
                        repair["failure_conflict_reason"]
                    )[:512]
                entry["result"] = {
                    "status": result.get("status"),
                    "repair": compact_repair,
                }
            else:
                entry["result"] = _compact_verification_result(result)
            compact_actions.append(entry)
        compact["actions"] = compact_actions
    selected = value.get("selected_test_identities")
    if isinstance(selected, list):
        compact["selected_test_identities"] = [str(item)[:256] for item in selected[:128]]
        compact["selected_test_count"] = len(selected)
        compact["selected_test_identities_truncated"] = len(selected) > 128
    return compact


def _source_path(uri: str, root: Path) -> Path:
    parsed = urlparse(uri)
    path = Path(unquote(parsed.path)) if parsed.scheme == "file" else Path(uri)
    try:
        return path.resolve().relative_to(root.resolve())
    except ValueError as exc:
        raise ForgeError("CONTINUOUS_SCOPE", f"event source is outside Forge root: {uri}") from exc


def _lifecycle_dir(config: Any) -> Path:
    return config.state_dir / "continuous"


def _supervisor_lease_path(config: Any) -> Path:
    return _lifecycle_dir(config) / "supervisor.json"


def _language_service_lease_path(config: Any) -> Path:
    return _lifecycle_dir(config) / "language-service.json"


def _read_json_path(path: Path) -> dict[str, object] | None:
    try:
        with path.open("rb") as stream:
            encoded = stream.read(CONTINUOUS_STATUS_MAX_BYTES + 1)
        if len(encoded) > CONTINUOUS_STATUS_MAX_BYTES:
            return None
        value = json.loads(encoded.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _write_json_path(path: Path, value: dict[str, object]) -> None:
    encoded = json.dumps(value, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    if len(encoded) > CONTINUOUS_STATUS_MAX_BYTES:
        raise ForgeError(
            "CONTINUOUS_STATUS_LIMIT",
            f"resident status exceeds the {CONTINUOUS_STATUS_MAX_BYTES}-byte bound",
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as staged:
        staged.write(encoded.decode("utf-8"))
        staged.flush()
        os.fsync(staged.fileno())
        temporary = Path(staged.name)
    os.replace(temporary, path)


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _supervisor_process_matches(pid: int, config: Any) -> bool:
    if not _pid_alive(pid):
        return False
    try:
        command = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\x00", b" ").decode()
    except OSError:
        return True
    return "mncs_forge.continuous_host" in command and str(config.config_path) in command


def _language_service_socket(config: Any) -> Path:
    value = config.continuous_settings.get(
        "language_service_socket", ".mncs/mnls-language-service.sock"
    )
    return config.root / str(value)


def _probe_language_service(config: Any) -> dict[str, object]:
    result = _mapping(
        LanguageServiceSocket(_language_service_socket(config), timeout=0.75).request(
            "workspace_status", {}
        )
    )
    observed_root = result.get("workspace_root")
    if not isinstance(observed_root, str) or Path(observed_root).resolve() != config.root.resolve():
        raise ForgeError(
            "LANGUAGE_SERVICE_IDENTITY",
            f"resident Language Service root is not {config.root}",
        )
    return result


def _language_service_command(config: Any) -> list[str]:
    configured = config.public_commands().get("language_service_host")
    if configured:
        return list(configured)
    environment = os.environ.get("MNLS_LANGUAGE_SERVICE_HOST")
    if environment:
        return shlex.split(environment)
    candidates = []
    language_root = os.environ.get("MNCS_LANGUAGE_SERVICE_ROOT")
    if language_root:
        candidates.append(
            Path(language_root).expanduser() / "target" / "debug" / "mnls-language-service-host"
        )
    candidates.append(
        config.root.parent
        / "mncs-language-service"
        / "target"
        / "debug"
        / "mnls-language-service-host"
    )
    candidates.append(
        Path(__file__).resolve().parents[3]
        / "mncs-language-service"
        / "target"
        / "debug"
        / "mnls-language-service-host"
    )
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return [str(candidate)]
    raise ForgeError(
        "LANGUAGE_SERVICE_HOST_UNAVAILABLE",
        "canonical mnls-language-service-host is not configured or built",
    )


def _terminate_language_service_start(
    process: subprocess.Popen[bytes], lease_path: Path, socket_path: Path
) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)
    lease_path.unlink(missing_ok=True)
    socket_path.unlink(missing_ok=True)


def ensure_language_service(config: Any) -> dict[str, object]:
    try:
        status = _probe_language_service(config)
        return {"state": "attached", "pid": None, "status": status}
    except ForgeError as error:
        if error.code == "LANGUAGE_SERVICE_IDENTITY":
            raise
    socket_path = _language_service_socket(config)
    if socket_path.exists():
        socket_path.unlink()
    command = _language_service_command(config)
    lease_path = _language_service_lease_path(config)
    log_path = _lifecycle_dir(config) / "language-service.log"
    environment = dict(os.environ)
    environment.update(
        {
            "MNLS_WORKSPACE_ROOT": str(config.root),
            "MNLS_SERVICE_SOCKET": str(socket_path),
        }
    )
    log = log_path.open("ab")
    try:
        process = subprocess.Popen(
            command,
            cwd=config.root,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
        )
    except OSError as error:
        log.close()
        raise ForgeError("LANGUAGE_SERVICE_START_FAILED", str(error)) from error
    log.close()
    _write_json_path(
        lease_path,
        {
            "schema_version": CONTINUOUS_LIFECYCLE_SCHEMA,
            "kind": "language-service",
            "pid": process.pid,
            "workspace_root": str(config.root),
            "owned_by_continuous": True,
        },
    )
    deadline = time.monotonic() + float(config.continuous_settings.get("start_timeout_seconds", 60))
    last_error = "resident Language Service did not become ready"
    while time.monotonic() < deadline:
        if process.poll() is not None:
            _terminate_language_service_start(process, lease_path, socket_path)
            raise ForgeError("LANGUAGE_SERVICE_START_FAILED", last_error)
        try:
            status = _probe_language_service(config)
            return {"state": "started", "pid": process.pid, "status": status}
        except ForgeError as error:
            last_error = str(error)
            time.sleep(0.05)
    _terminate_language_service_start(process, lease_path, socket_path)
    raise ForgeError("LANGUAGE_SERVICE_START_TIMEOUT", last_error)


def _supervisor_status(config: Any) -> dict[str, object]:
    path = _supervisor_lease_path(config)
    lease = _read_json_path(path)
    if not lease:
        return {"state": "stopped", "pid": None}
    pid = int(lease.get("pid", 0) or 0)
    if not _supervisor_process_matches(pid, config):
        path.unlink(missing_ok=True)
        return {"state": "stale", "pid": pid}
    return {**lease, "state": str(lease.get("phase", "starting"))}


def _language_service_status(config: Any) -> dict[str, object]:
    try:
        status = _probe_language_service(config)
    except ForgeError as error:
        return {"state": "unavailable", "error": str(error)}
    lease = _read_json_path(_language_service_lease_path(config)) or {}
    return {
        "state": "running",
        "pid": lease.get("pid"),
        "owned_by_continuous": bool(lease.get("owned_by_continuous", False)),
        "status": status,
    }


def continuous_lifecycle(
    config: Any, action: str, *, mode: str = "development"
) -> dict[str, object]:
    """Bounded lifecycle control around the one canonical supervisor loop."""

    lifecycle = _lifecycle_dir(config)
    lifecycle.mkdir(parents=True, exist_ok=True)
    lock = FileLock(str(lifecycle / "lifecycle.lock"), timeout=5)
    try:
        with lock:
            if action == "status":
                status_file = _read_json_path(config.state_dir / "continuous" / "status.json") or {}
                supervisor = _supervisor_status(config)
                resource_state: dict[str, object] = {}
                if bool(config.continuous_settings.get("enabled", False)):
                    from .resource_envelope import SystemdCgroupEnvelope

                    try:
                        envelope = SystemdCgroupEnvelope(
                            _mapping(config.continuous_settings.get("resource_envelope")),
                            required=True,
                        )
                        resource_state = envelope.status()
                    except ForgeError as error:
                        resource_state = {
                            "state": "unavailable",
                            "mechanism": "systemd-user-service+cgroup-v2",
                            "limitation": error.code,
                        }
                supervisor["resource_state"] = resource_state
                persisted_resources = _mapping(status_file.get("resources"))
                resources = {**persisted_resources, **resource_state}
                for key in ("resource_exhaustion_events", "deferred_jobs"):
                    resources[key] = max(
                        int(persisted_resources.get(key, 0) or 0),
                        int(resource_state.get(key, 0) or 0),
                    )
                if not resource_state.get("last_resource_event"):
                    resources["last_resource_event"] = persisted_resources.get(
                        "last_resource_event"
                    )
                status_file["resources"] = resources
                active_job = _mapping(status_file.get("active_job"))
                if active_job:
                    started = active_job.get("started_monotonic")
                    if isinstance(started, (int, float)) and not isinstance(started, bool):
                        active_job["elapsed_seconds"] = round(time.monotonic() - float(started), 3)
                    active_job["resource_protection_state"] = resource_state.get("state")
                    active_job["resource_envelope_identity"] = resource_state.get(
                        "resource_envelope_identity"
                    )
                    active_job["execution_memory_current_bytes"] = resource_state.get(
                        "aggregate_memory_current_bytes"
                    )
                    active_job["execution_memory_peak_bytes"] = resource_state.get(
                        "aggregate_memory_peak_bytes"
                    )
                    active_job["execution_process_count_current"] = resource_state.get(
                        "aggregate_process_count"
                    )
                    status_file["active_job"] = active_job
                    resources["active_jobs"] = [
                        {
                            key: active_job[key]
                            for key in (
                                "identity",
                                "generation",
                                "source_identity",
                                "candidate_identity",
                                "action",
                                "elapsed_seconds",
                                "resource_protection_state",
                                "resource_envelope_identity",
                                "execution_process_count_current",
                            )
                            if key in active_job
                        }
                    ]
                    resources["concurrency"] = int(
                        isinstance(active_job.get("execution_process_count_current"), int)
                        and int(active_job["execution_process_count_current"]) > 0
                    )
                return {
                    "schema_version": CONTINUOUS_LIFECYCLE_SCHEMA,
                    "workspace_root": str(config.root),
                    "language_service": _language_service_status(config),
                    "supervisor": supervisor,
                    "continuous": status_file,
                }
            if action == "start":
                if not bool(config.continuous_settings.get("enabled", False)):
                    raise ForgeError("CONTINUOUS_DISABLED", "continuous mode is not enabled")
                language_service = ensure_language_service(config)
                existing = _supervisor_status(config)
                if existing.get("state") in {"starting", "running"}:
                    return {
                        "schema_version": CONTINUOUS_LIFECYCLE_SCHEMA,
                        "state": "already_running",
                        "language_service": language_service,
                        "supervisor": existing,
                    }
                lease_path = _supervisor_lease_path(config)
                command = [
                    sys.executable,
                    "-m",
                    "mncs_forge.continuous_host",
                    "--config",
                    str(config.config_path),
                    "--mode",
                    mode,
                ]
                log_path = lifecycle / "supervisor.log"
                log = log_path.open("ab")
                process = subprocess.Popen(
                    command,
                    cwd=config.root,
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                    close_fds=True,
                )
                log.close()
                deadline = time.monotonic() + float(
                    config.continuous_settings.get("start_timeout_seconds", 60)
                )
                while time.monotonic() < deadline:
                    current = _supervisor_status(config)
                    if current.get("pid") == process.pid and current.get("state") == "running":
                        return {
                            "schema_version": CONTINUOUS_LIFECYCLE_SCHEMA,
                            "state": "started",
                            "language_service": language_service,
                            "supervisor": current,
                        }
                    if process.poll() is not None:
                        break
                    time.sleep(0.05)
                if process.poll() is None:
                    # A bounded lifecycle command must not leave a second
                    # untracked supervisor behind when Forge initialization
                    # exceeds the declared startup window.
                    os.kill(process.pid, signal.SIGTERM)
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        os.kill(process.pid, signal.SIGKILL)
                        process.wait(timeout=2)
                lease = _read_json_path(lease_path)
                if lease and int(lease.get("pid", 0) or 0) == process.pid:
                    lease_path.unlink(missing_ok=True)
                raise ForgeError(
                    "CONTINUOUS_START_TIMEOUT",
                    "Forge supervisor did not become ready within the bounded startup window",
                )
            if action == "stop":
                current = _supervisor_status(config)
                pid = int(current.get("pid", 0) or 0)
                if current.get("state") not in {"starting", "running"} or not pid:
                    return {
                        "schema_version": CONTINUOUS_LIFECYCLE_SCHEMA,
                        "state": "stopped",
                        "supervisor": current,
                    }
                os.kill(pid, signal.SIGTERM)
                deadline = time.monotonic() + float(
                    config.continuous_settings.get("stop_timeout_seconds", 15)
                )
                while time.monotonic() < deadline and _supervisor_process_matches(pid, config):
                    time.sleep(0.05)
                stopped = _supervisor_status(config)
                return {
                    "schema_version": CONTINUOUS_LIFECYCLE_SCHEMA,
                    "state": "stopped"
                    if stopped.get("state") in {"stopped", "stale"}
                    else "stopping",
                    "supervisor": stopped,
                }
            raise ForgeError("CONTINUOUS_LIFECYCLE", f"unknown lifecycle action: {action}")
    except Timeout as error:
        raise ForgeError(
            "CONTINUOUS_LIFECYCLE_BUSY", "workspace lifecycle is already changing"
        ) from error


def _bounded_repository_state(root: Path) -> dict[str, object]:
    """Observe only the configured repository; never discover sibling repositories."""

    try:
        result = subprocess.run(
            ["git", "status", "--short", "--untracked-files=no"],
            cwd=root,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=1.0,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {
            "repository": root.name,
            "root": str(root),
            "state": "unknown",
            "reason": str(exc),
            "entries": [],
        }
    entries = result.stdout.decode("utf-8", errors="replace").splitlines()
    if result.returncode != 0:
        return {
            "repository": root.name,
            "root": str(root),
            "state": "unknown",
            "reason": result.stderr.decode("utf-8", errors="replace")[:512],
            "entries": [],
        }
    return {
        "repository": root.name,
        "root": str(root),
        "state": "clean" if not entries else "dirty",
        "entries": entries[:32],
        "truncated": len(entries) > 32,
    }


def environment_enter(config: Any, *, mode: str = "development") -> dict[str, object]:
    """Compose one bounded agent-entry capsule from resident authority projections."""

    started = time.perf_counter()
    lifecycle_start = continuous_lifecycle(config, "start", mode=mode)
    lifecycle = continuous_lifecycle(config, "status", mode=mode)
    language_service = _mapping(lifecycle.get("language_service"))
    resident_status = _mapping(language_service.get("status"))
    if resident_status.get("workspace_root") is not None:
        observed_root = Path(str(resident_status["workspace_root"])).resolve()
        if observed_root != config.root.resolve():
            raise ForgeError(
                "LANGUAGE_SERVICE_IDENTITY",
                f"resident Language Service root is not {config.root}",
            )
    family = _mapping(
        LanguageServiceSocket(_language_service_socket(config), timeout=8.0).request(
            "family_agent_context", {"max_items": 16}
        )
    )
    workspace_status = resident_status
    repository = _mapping(family.get("repository"))
    language = _mapping(family.get("language"))
    architecture = _mapping(family.get("architecture"))
    verification = _mapping(family.get("verification"))
    completeness = _mapping(family.get("completeness"))
    continuous = _mapping(lifecycle.get("continuous"))
    supervisor = _mapping(lifecycle.get("supervisor"))
    resource_summary = _mapping(continuous.get("resources")) or _mapping(
        supervisor.get("resource_state")
    )
    pressures = [
        item
        for item in family.get("pressures", [])
        if isinstance(item, dict) and item.get("unresolved") is not False
    ][:16]
    attention = [
        item for item in continuous.get("blocking_attention_events", []) if isinstance(item, dict)
    ][:16]
    counts = _mapping(continuous.get("counts"))
    continuous_summary = {
        "schema_version": continuous.get("schema_version"),
        "workspace_generation": continuous.get("workspace_generation"),
        "current_source_identity": continuous.get("current_source_identity"),
        "pending_checks": [
            item for item in continuous.get("pending_checks", []) if isinstance(item, dict)
        ][:16],
        "pending_check_capacity": continuous.get("pending_check_capacity"),
        "pending_check_overflow_count": continuous.get("pending_check_overflow_count"),
        "pending_check_overflow_identity": continuous.get(
            "pending_check_overflow_identity"
        ),
        "counts": counts,
        "stale_evidence_count": continuous.get("stale_evidence_count"),
        "active_verification_tier": continuous.get("active_verification_tier"),
        "blocking_attention_events": attention,
        "event_cursor": continuous.get("event_cursor"),
        "event_stream_identity": continuous.get("event_stream_identity"),
        "evidence_reused": continuous.get("evidence_reused"),
        "evidence_recomputed": continuous.get("evidence_recomputed"),
        "queued_jobs_cancelled": continuous.get("queued_jobs_cancelled"),
        "deferred_jobs": continuous.get("deferred_jobs"),
        "resource_exhaustion_events": continuous.get("resource_exhaustion_events"),
        "cancellation_requested": continuous.get("cancellation_requested"),
        "resources": resource_summary,
        "active_job": _mapping(continuous.get("active_job")) or None,
        "queue": _mapping(continuous.get("queue")),
    }
    manifest_identity = repository.get("manifest_identity")
    language_identity = language.get("content_identity")
    architecture_identity = architecture.get("content_identity")
    stream_identity = workspace_status.get("stream_identity")
    workspace_identity = local_json_identity(
        {
            "schema_version": "mncs.workspace-entry/1",
            "root": str(config.root.resolve()),
            "project_identity": config.project_identity,
            "manifest_identity": manifest_identity,
            "stream_identity": stream_identity,
        }
    )
    environment_identity = local_json_identity(
        {
            "schema_version": ENVIRONMENT_ENTRY_SCHEMA,
            "workspace_identity": workspace_identity,
            "language_identity": language_identity,
            "architecture_identity": architecture_identity,
            "continuous_configuration": config.continuous_settings,
        }
    )
    return {
        "schema_version": ENVIRONMENT_ENTRY_SCHEMA,
        "environment_identity": environment_identity,
        "workspace_identity": workspace_identity,
        "workspace_generation": workspace_status.get("generation"),
        "language_identity": language_identity,
        "architecture_identity": architecture_identity,
        "resident_services": {
            "language_service": {
                "state": language_service.get("state"),
                "pid": language_service.get("pid"),
                "stream_identity": stream_identity,
            },
            "forge_supervisor": {
                "state": supervisor.get("state"),
                "pid": supervisor.get("pid"),
                "resources": _mapping(supervisor.get("resource_state")),
            },
        },
        "canonical_authority": {
            "language": "mncs-language",
            "semantic_workspace": "mncs-language-service",
            "remediation": "mncs-doctor",
            "verification_planning": "ravel",
            "test": "mncs-test",
            "debug": "mncs-debug",
            "persistence": "mncs-store",
            "orchestration": "mncs-forge",
        },
        "task_environment": {
            "family_context_status": family.get("status"),
            "family_completeness": completeness.get("state"),
            "verification_state": verification.get("state"),
            "current_blocking_attention": attention,
            "pre_existing_failures": {
                "counts": counts,
                "attention_count": len(attention),
            },
            "unresolved_pressures": pressures,
            "relevant_dirty_repositories": [_bounded_repository_state(config.root)],
            "continuous_status": continuous_summary,
        },
        "negative_knowledge": [
            item for item in family.get("negative_knowledge", []) if isinstance(item, dict)
        ][:16],
        "query_handles": [
            "family_agent_context",
            "workspace_status",
            "language_capabilities",
            "describe_subject",
            "obligations",
            "continuous_status",
        ],
        "startup": {
            "state": lifecycle_start.get("state"),
            "language_service_state": _mapping(lifecycle_start.get("language_service")).get(
                "state"
            ),
            "forge_supervisor_state": _mapping(lifecycle_start.get("supervisor")).get("state"),
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
        },
    }


class LanguageServiceSocket:
    """Small bounded client for the Language Service JSON-line host."""

    def __init__(self, path: Path, *, timeout: float = 30.0) -> None:
        self.path = path
        self.timeout = timeout

    def request(self, method: str, params: dict[str, object] | None = None) -> object:
        import socket

        request = {
            "id": 1,
            "method": method,
            "params": params or {},
        }
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(self.timeout)
                client.connect(str(self.path))
                client.sendall((json.dumps(request, separators=(",", ":")) + "\n").encode())
                data = bytearray()
                while len(data) < 8 * 1024 * 1024:
                    chunk = client.recv(65536)
                    if not chunk:
                        break
                    data.extend(chunk)
                    if b"\n" in chunk:
                        break
        except OSError as exc:
            raise ForgeError("LANGUAGE_SERVICE_UNAVAILABLE", str(exc)) from exc
        try:
            response = json.loads(bytes(data).splitlines()[0])
        except (IndexError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ForgeError("LANGUAGE_SERVICE_PROTOCOL", "resident response was not JSON") from exc
        if not isinstance(response, dict) or response.get("ok") is not True:
            raise ForgeError(
                "LANGUAGE_SERVICE_REQUEST",
                str(_mapping(response).get("error", "resident request failed")),
            )
        return response.get("result")


class _ContinuousEventIngress:
    """One bounded owner for the resident Language Service event stream.

    Verifier dispatch can block while a generic process effect runs. This
    ingress keeps consuming the same authoritative stream during that work,
    lets the supervisor cancel a stale generation, and retains events in a
    bounded queue for ordinary dispatch when the verifier returns.
    """

    def __init__(
        self,
        supervisor: "ContinuousSupervisor",
        client: LanguageServiceSocket,
        *,
        after_cursor: int,
        max_events: int,
        poll_interval: float,
    ) -> None:
        self.supervisor = supervisor
        self.client = client
        self.cursor = after_cursor
        self.max_events = max_events
        self.poll_interval = min(max(poll_interval, 0.01), 0.05)
        self.events: queue.Queue[dict[str, object]] = queue.Queue(
            maxsize=CONTINUOUS_PENDING_CAPACITY
        )
        self.stop_event = threading.Event()
        self.failure: str | None = None
        self.thread = threading.Thread(
            target=self._run,
            name="mncs-forge-language-event-ingress",
            daemon=True,
        )

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        self.thread.join(timeout=1.0)

    def _run(self) -> None:
        while not self.stop_event.is_set():
            try:
                self.client.request("refresh_workspace", {})
                poll = self.supervisor._poll(
                    self.client,
                    after_cursor=self.cursor,
                    max_events=self.max_events,
                )
            except (ForgeError, TypeError, ValueError) as error:
                self.failure = f"Language Service event ingress failed: {error}"
                self.stop_event.set()
                return
            if bool(poll.get("reset_required")):
                self.failure = "Language Service event stream reset requires reconciliation"
                self.stop_event.set()
                return
            observed = [item for item in poll.get("events", []) if isinstance(item, dict)]
            for event in observed:
                cursor = int(event.get("cursor", self.cursor))
                if cursor <= self.cursor:
                    continue
                self.supervisor._cancel_active_for_event(event)
                try:
                    self.events.put_nowait(event)
                except queue.Full:
                    self.failure = "Language Service event ingress reached its bounded queue capacity"
                    self.stop_event.set()
                    return
                self.cursor = cursor
            if self.stop_event.wait(self.poll_interval):
                return


class ContinuousSupervisor:
    def __init__(self, forge: Forge) -> None:
        self.forge = forge
        self.config = forge.config
        self.settings = self.config.continuous_settings
        self._resource_semantics = (
            getattr(forge, "_resource_semantics", None)
            or getattr(forge, "_native", None)
            or NativeForgeAdapter(self.config.root)
        )
        self.status_counts: Counter[str] = Counter()
        self.pending: OrderedDict[str, dict[str, object]] = OrderedDict()
        self.attention: list[dict[str, object]] = []
        self.repairs: list[dict[str, object]] = []
        self.selected_test_ids: list[str] = []
        self.selected_test_count = 0
        self.attention_evictions = 0
        self.repair_evictions = 0
        self.pending_overflow_count = 0
        self.pending_overflow_identity: str | None = None
        self.resource_exhaustion_events = 0
        self.deferred_jobs = 0
        self.active_job: dict[str, object] | None = None
        self.stale_jobs = 0
        self.cancelled_jobs = 0
        self.reused_evidence = 0
        self.recomputed_evidence = 0
        self.current_generation = 0
        self.current_source_identity: str | None = None
        self.current_cursor = 0
        self.stream_identity: str | None = None
        self.active_tier = "edit-time"
        self._stop_requested = False
        self._active_job_lock = threading.Lock()

    def request_stop(self) -> None:
        self._stop_requested = True
        try:
            retained = self.read_status()
            retained["cancellation_requested"] = True
            active_job = _mapping(retained.get("active_job"))
            if active_job:
                active_job["cancellation_requested"] = True
                retained["active_job"] = active_job
            self._write_status(retained)
        except (OSError, ForgeError):
            pass

    def _socket(self) -> LanguageServiceSocket:
        value = self.settings.get("language_service_socket", ".mncs/mnls-language-service.sock")
        return LanguageServiceSocket(self.config.root / str(value))

    def _status_path(self) -> Path:
        return self.config.state_dir / "continuous" / "status.json"

    def _family_path(self, value: object, *, label: str) -> Path:
        candidate = Path(str(value))
        if not candidate.is_absolute():
            candidate = self.config.root / candidate
        resolved = candidate.resolve()
        try:
            resolved.relative_to(self.config.root.resolve().parent)
        except ValueError as error:
            raise ForgeError(
                "CONTINUOUS_FAMILY_SCOPE",
                f"{label} must remain inside the configured workspace parent",
            ) from error
        return resolved

    def _poll(
        self, client: LanguageServiceSocket, *, after_cursor: int, max_events: int
    ) -> dict[str, object]:
        params: dict[str, object] = {
            "after_cursor": after_cursor,
            "max_events": max_events,
        }
        if self.stream_identity:
            params["stream_identity"] = self.stream_identity
        poll = _mapping(client.request("poll_events", params))
        observed = poll.get("stream_identity")
        if isinstance(observed, str) and observed:
            if self.stream_identity is None or bool(poll.get("reset_required")):
                self.stream_identity = observed
        return poll

    def _write_status(self, status: dict[str, object]) -> None:
        path = self._status_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(status, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    @contextmanager
    def _job_scope(
        self,
        event: dict[str, object],
        trigger: dict[str, object],
        action: str,
        *,
        client: Any | None = None,
    ) -> Any:
        current = _mapping(event.get("current"))
        verifier_ids = _list(trigger.get("verifier_ids"))
        job: dict[str, object] = {
            "generation": int(event.get("current_generation", 0)),
            "source_identity": str(current.get("identity") or "")[:256] or None,
            "candidate_identity": str(self.settings.get("candidate_identity") or "")[:256] or None,
            "cursor": int(event.get("cursor", 0)),
            "trigger_id": str(trigger.get("id", ""))[:128],
            "action": action[:64],
            "verifier_ids": [item[:128] for item in verifier_ids[:32]],
            "verifier_count": len(verifier_ids),
            "verifier_ids_truncated": len(verifier_ids) > 32,
            "started_at": now(),
            "started_monotonic": time.monotonic(),
        }
        job["identity"] = local_json_identity(
            {
                key: value
                for key, value in job.items()
                if key not in {"started_at", "started_monotonic"}
            }
        )
        runner = getattr(getattr(self, "forge", None), "_executor", None)
        set_context = getattr(runner, "set_job_context", None)
        if callable(set_context):
            set_context(job)
        with self._active_job_lock:
            self.active_job = job
        if getattr(getattr(self, "config", None), "state_dir", None) is not None:
            self._write_status(self.status())
        try:
            yield None
        finally:
            if callable(set_context):
                set_context(None)
            if job.get("cancellation_requested") is True:
                self.last_cancellation = {
                    key: job[key]
                    for key in (
                        "generation",
                        "cancellation_generation",
                        "cancellation_event_cursor",
                        "cancellation_event_to_decision_seconds",
                        "cancellation_native_transition_seconds",
                        "cancellation_decision_to_request_seconds",
                        "cancellation_request_to_tree_empty_observed_seconds",
                        "cancellation_request_to_launcher_reaped_observed_seconds",
                        "cancellation_request_to_cleanup_complete_observed_seconds",
                        "cancellation_request_to_reaped_seconds",
                        "cancellation_observation",
                    )
                    if key in job
                }
            with self._active_job_lock:
                self.active_job = None
            if getattr(getattr(self, "config", None), "state_dir", None) is not None:
                self._write_status(self.status())

    def _cancel_active_for_event(self, event: dict[str, object]) -> None:
        """Apply native stale disposition to the active generic process handle."""

        with self._active_job_lock:
            job = self.active_job
            if not isinstance(job, dict):
                return
            work_generation = int(job.get("generation", 0))
            observed_generation = int(event.get("current_generation", work_generation))
            if observed_generation <= work_generation:
                return
            event_arrived = time.monotonic()
            transition_started = event_arrived
            transition = self._native_resource_transition(
                event=event,
                candidate=str(job.get("candidate_identity") or "event"),
                verifier_id="in-flight-generation",
                outcome="Unknown",
                evidence_status="NotRun",
                has_outcome=False,
                queue_remaining=0,
                in_flight=True,
                work_generation=work_generation,
                current_generation=observed_generation,
            )
            decision_at = time.monotonic()
            if not transition.cancel_owned_work:
                return
            self.current_generation = max(self.current_generation, observed_generation)
            self.current_cursor = max(self.current_cursor, int(event.get("cursor", 0)))
            self.current_source_identity = (
                _mapping(event.get("current")).get("identity") or self.current_source_identity
            )
            runner = getattr(getattr(self, "forge", None), "_executor", None)
            cancel = getattr(runner, "cancel_generation", None)
            cancellation = (
                cancel(work_generation) if callable(cancel) else {"handle_found": False}
            )
            job["cancellation_requested"] = True
            job["cancellation_generation"] = observed_generation
            job["cancellation_event_to_decision_seconds"] = round(
                decision_at - event_arrived, 6
            )
            job["cancellation_native_transition_seconds"] = round(
                decision_at - transition_started, 6
            )
            job["cancellation_observation"] = cancellation
            job["cancellation_event_cursor"] = event.get("cursor")
            job["cancellation_request_at"] = decision_at
            if cancellation.get("decision_to_cancel_request_seconds") is not None:
                job["cancellation_decision_to_request_seconds"] = cancellation[
                    "decision_to_cancel_request_seconds"
                ]
            if cancellation.get("cancel_request_to_reaped_seconds") is not None:
                job["cancellation_request_to_reaped_seconds"] = cancellation[
                    "cancel_request_to_reaped_seconds"
                ]
            for source, target in (
                (
                    "cancel_request_to_tree_empty_observed_seconds",
                    "cancellation_request_to_tree_empty_observed_seconds",
                ),
                (
                    "cancel_request_to_launcher_reaped_observed_seconds",
                    "cancellation_request_to_launcher_reaped_observed_seconds",
                ),
                (
                    "cancel_request_to_cleanup_complete_observed_seconds",
                    "cancellation_request_to_cleanup_complete_observed_seconds",
                ),
            ):
                if cancellation.get(source) is not None:
                    job[target] = cancellation[source]

    def _attention(
        self, event: dict[str, object], reason: str, *, tier: str = "incremental"
    ) -> None:
        entry = {
            "generation": event.get("current_generation"),
            "cursor": event.get("cursor"),
            "source_identity": str(_mapping(event.get("current")).get("identity") or "")[:256]
            or None,
            "reason": reason[:512],
            "tier": tier,
        }
        if entry not in self.attention:
            if len(self.attention) >= CONTINUOUS_ATTENTION_CAPACITY:
                del self.attention[0]
                self.attention_evictions = min(_COUNTER_MAX, self.attention_evictions + 1)
            self.attention.append(entry)

    def _escalate(
        self,
        event: dict[str, object],
        trigger: dict[str, object],
        reason: str,
        *,
        status: str = "UNKNOWN",
        tier: str = "incremental",
    ) -> None:
        policy = str(trigger.get("escalation", "attention"))
        if policy == "silent" or (policy == "unknown" and status != "UNKNOWN"):
            return
        self._attention(event, reason, tier=tier)

    def _record_status(self, status: str) -> None:
        if status in {"PASS", "FAIL", "UNKNOWN"}:
            if not hasattr(self, "status_counts"):
                self.status_counts = Counter()
            self.status_counts[status] = min(_COUNTER_MAX, self.status_counts[status] + 1)

    def _native_resource_transition(
        self,
        *,
        event: Mapping[str, object],
        candidate: str,
        verifier_id: str,
        outcome: str,
        evidence_status: str,
        has_outcome: bool,
        queue_remaining: int,
        resource_outcome_observed: bool = False,
        resource_gate_closed: bool = False,
        in_flight: bool = False,
        source_identity_matches: bool = True,
        work_generation: int | None = None,
        current_generation: int | None = None,
    ) -> Any:
        semantics = self._resource_semantics
        transition = getattr(semantics, "verification_resource_transition", None)
        if not callable(transition):
            raise ForgeError(
                "NATIVE_CONTINUOUS_UNAVAILABLE",
                "continuous resource transitions require the canonical MNCS Forge core",
        )
        generation = int(event.get("current_generation", 0))
        selected_work_generation = generation if work_generation is None else work_generation
        identity = self._pending_identity(selected_work_generation, candidate, verifier_id)
        return transition(
            {
                "current_generation": (
                    self.current_generation
                    if current_generation is None
                    else current_generation
                ),
                "work_generation": selected_work_generation,
                "source_identity_matches": source_identity_matches,
                "verification_required": True,
                "in_flight": in_flight,
                "has_outcome": has_outcome,
                "resource_outcome_observed": resource_outcome_observed,
                "outcome": outcome,
                "evidence_status": evidence_status,
                "pending_exists": identity in self.pending,
                "pending_count": len(self.pending),
                "pending_capacity": CONTINUOUS_PENDING_CAPACITY,
                "queue_remaining": queue_remaining,
                "resource_gate_closed": resource_gate_closed,
            }
        )

    def _native_queue_admission(self, total_count: int) -> tuple[int, int]:
        semantics = self._resource_semantics
        admission = getattr(semantics, "verification_queue_admit", None)
        if not callable(admission):
            raise ForgeError(
                "NATIVE_CONTINUOUS_UNAVAILABLE",
                "continuous queue admission requires the canonical MNCS Forge core",
            )
        return admission(total_count)

    def _native_completed_freshness(
        self,
        event: Mapping[str, object],
        *,
        candidate: str,
        verifier_id: str,
        observed_generation: int,
    ) -> Any:
        return self._native_resource_transition(
            event={**event, "current_generation": observed_generation},
            candidate=candidate,
            verifier_id=verifier_id,
            outcome="Unknown",
            evidence_status="NotRun",
            has_outcome=False,
            queue_remaining=0,
            in_flight=False,
            work_generation=int(event.get("current_generation", 0)),
            current_generation=observed_generation,
        )

    def _remember_pending(self, identity: str, value: dict[str, object]) -> None:
        """Retain only the newest bounded pending obligations by exact identity."""

        bounded_identity = identity if len(identity) <= 256 else local_json_identity(identity)
        if bounded_identity in self.pending:
            self.pending.move_to_end(bounded_identity)
        elif len(self.pending) >= CONTINUOUS_PENDING_CAPACITY:
            self.pending_overflow_count = min(_COUNTER_MAX, self.pending_overflow_count + 1)
            self.pending_overflow_identity = local_json_identity(
                {
                    "previous_overflow_identity": self.pending_overflow_identity,
                    "deferred_obligation_identity": bounded_identity,
                }
            )
            return
        compact = _compact_verification_result(value)
        compact["obligation_identity"] = bounded_identity[:128]
        self.pending[bounded_identity] = compact

    @staticmethod
    def _pending_identity(generation: int, candidate: str, verifier_id: str) -> str:
        return local_json_identity(
            {
                "generation": generation,
                "candidate_identity": candidate,
                "verifier_id": verifier_id,
            }
        )

    def _resolve_micro_pending(
        self, event: dict[str, object], candidate: str, verifier_id: str
    ) -> None:
        self.pending.pop(
            self._pending_identity(
                int(event.get("current_generation", 0)), candidate, verifier_id
            ),
            None,
        )

    def _remember_micro_pending(
        self,
        event: dict[str, object],
        trigger: dict[str, object],
        candidate: str,
        verifier_id: str,
        reason: str,
        outcome: object | None = None,
    ) -> None:
        generation = int(event.get("current_generation", 0))
        current = _mapping(event.get("current"))
        value: dict[str, object] = {
            "verifier_id": verifier_id,
            "status": "UNKNOWN",
            "reason": reason[:256],
            "reused": False,
            "generation": generation,
            "source_identity": str(current.get("identity") or "")[:256],
            "source_uri": str(current.get("uri") or "")[:1024],
            "candidate_identity": candidate[:256],
            "trigger_id": str(trigger.get("id") or "")[:128],
            "action": str(trigger.get("action") or "")[:64],
            "cursor": int(event.get("cursor", 0)),
        }
        if outcome is not None:
            value.update(_compact_verification_result(outcome))
        identity = self._pending_identity(generation, candidate, verifier_id)
        self._remember_pending(identity, value)

    def _restore_pending(self, prior: dict[str, object]) -> None:
        count = prior.get("pending_check_overflow_count")
        if isinstance(count, int) and not isinstance(count, bool):
            self.pending_overflow_count = min(_COUNTER_MAX, max(0, count))
        identity = prior.get("pending_check_overflow_identity")
        if isinstance(identity, str):
            self.pending_overflow_identity = identity[:128]
        values = prior.get("pending_checks")
        if not isinstance(values, list):
            return
        for item in values[:CONTINUOUS_PENDING_CAPACITY]:
            if not isinstance(item, dict):
                continue
            identity = item.get("obligation_identity")
            key = identity if isinstance(identity, str) and identity else local_json_identity(item)
            self._remember_pending(key, item)

    def _cancel_superseded_pending(self, event: dict[str, object]) -> None:
        current = _mapping(event.get("current"))
        uri = str(current.get("uri") or "")[:1024]
        identity = str(current.get("identity") or "")[:256]
        if not uri or not identity:
            return
        stale: list[str] = []
        for key, item in self.pending.items():
            if item.get("source_uri") != uri:
                continue
            work_generation = item.get("generation")
            if not isinstance(work_generation, int) or isinstance(work_generation, bool):
                work_generation = 0
            transition = self._native_resource_transition(
                event=event,
                candidate=str(item.get("candidate_identity") or "pending"),
                verifier_id=str(item.get("verifier_id") or "pending"),
                outcome="Unknown",
                evidence_status="Unknown",
                has_outcome=True,
                queue_remaining=0,
                work_generation=work_generation,
                source_identity_matches=item.get("source_identity") == identity,
            )
            if transition.disposition == "DiscardStale":
                stale.append(key)
        for key in stale:
            self.pending.pop(key, None)
        self.cancelled_jobs = min(_COUNTER_MAX, self.cancelled_jobs + len(stale))

    def _remember_repair(self, value: dict[str, object]) -> None:
        """Retain only the bounded user-facing summary of a completed repair."""

        compact: dict[str, object] = {}
        if "repair_rounds" in value:
            compact["repair_rounds"] = value["repair_rounds"]
        for key in ("original_source_identity", "resulting_source_identity"):
            if isinstance(value.get(key), str):
                compact[key] = str(value[key])[:256]
        for key in ("doctor_fix_identities", "migration_rule_identities"):
            identities = value.get(key)
            if isinstance(identities, list):
                compact[key] = [str(item)[:256] for item in identities[:32]]
                compact[f"{key}_truncated"] = len(identities) > 32
        for key in ("validation_result", "focused_verification_result"):
            if value.get(key) is not None:
                compact[key] = _compact_verification_result(value[key])
        conflict = value.get("failure_conflict_reason")
        if isinstance(conflict, str) and conflict:
            compact["failure_conflict_reason"] = conflict[:512]
        if len(self.repairs) >= CONTINUOUS_REPAIR_CAPACITY:
            del self.repairs[0]
            self.repair_evictions = min(_COUNTER_MAX, self.repair_evictions + 1)
        self.repairs.append(compact)

    def _set_selected_test_ids(self, identities: list[str]) -> None:
        seen: set[str] = set()
        selected: list[str] = []
        for identity in identities[:CONTINUOUS_SELECTED_TEST_CAPACITY]:
            bounded = str(identity)[:256]
            if bounded in seen:
                continue
            seen.add(bounded)
            selected.append(bounded)
        self.selected_test_count = len(identities)
        self.selected_test_ids = sorted(selected)

    def _run_command(self, command: list[str], *, cwd: Path, timeout: float) -> dict[str, object]:
        environment = {
            key: os.environ[key]
            for key in self.config.raw.get("environment_allowlist", [])
            if key in os.environ
        }
        session = self.forge._executor.run(  # type: ignore[attr-defined]
            command,
            cwd=cwd,
            timeout=timeout,
            output_cap=self.config.output_cap,
            environment=environment,
        )
        result = session.result
        stdout = result.stdout.decode("utf-8", errors="replace") if result else ""
        stderr = (
            result.stderr.decode("utf-8", errors="replace")
            if result
            else session.error_message or ""
        )
        return {
            "command": list(command),
            "returncode": result.returncode if result else None,
            "stdout": stdout[-self.config.output_cap :],
            "stderr": stderr[-4096:],
            "duration_seconds": session.observation.duration_seconds,
            "error_code": session.error_code,
            "resource_evidence": {
                "resource_envelope": dict(session.observation.resource_envelope),
                **dict(session.observation.resource_observations),
            }
            if session.observation.resource_envelope or session.observation.resource_observations
            else {},
        }

    @staticmethod
    def _stdout_json(execution: dict[str, object]) -> dict[str, object] | None:
        try:
            value = json.loads(str(execution.get("stdout", "")))
        except json.JSONDecodeError:
            return None
        return value if isinstance(value, dict) else None

    def _matches(self, trigger: dict[str, object], event: dict[str, object]) -> bool:
        impact = _mapping(event.get("impact"))
        diagnostics = _mapping(event.get("diagnostics"))
        obligations = _mapping(event.get("obligations"))
        nodes = [node for node in impact.get("nodes", []) if isinstance(node, dict)]
        state: dict[str, object] = {
            "event_kinds": _list(trigger.get("event_kinds")),
            "change_kinds": _list(trigger.get("change_kinds")),
            "guarantee_domains": _list(trigger.get("guarantee_domains")),
            "risk_flags": _list(trigger.get("risk_flags")),
            "subject_kinds": _list(trigger.get("subject_kinds")),
            "contract_patterns": _list(trigger.get("contract_identities")),
            "obligation_patterns": _list(trigger.get("obligation_identities")),
            "diagnostic_patterns": _list(trigger.get("diagnostics")),
            "diagnostic_added": _list(diagnostics.get("added")),
            "diagnostic_resolved": _list(diagnostics.get("resolved")),
            "semantic_subjects": _list(event.get("semantic_subjects")),
            "obligation_added": _list(obligations.get("added")),
            "obligation_resolved": _list(obligations.get("resolved")),
            "obligation_status_changed": _list(obligations.get("status_changed")),
            "impact_change_kinds": _list(impact.get("change_kinds")),
            "impact_guarantee_domains": _list(impact.get("guarantee_domains")),
            "impact_risk_flags": _list(impact.get("risk_flags")),
            "impact_node_kinds": [str(_mapping(node).get("kind")) for node in nodes],
            "impact_node_identities": [str(_mapping(node).get("identity")) for node in nodes],
            "security": bool(trigger.get("security", False)),
            "reconciled": bool(event.get("reconciled", False)),
        }
        matcher = getattr(self._resource_semantics, "continuous_trigger_matches", None)
        if not callable(matcher):
            raise ForgeError(
                "NATIVE_CONTINUOUS_UNAVAILABLE",
                "continuous trigger routing requires the canonical MNCS Forge core",
            )
        return bool(matcher(state))

    def _trigger_cost_allowed(self, trigger: dict[str, object], verifier_id: str) -> bool:
        verifier = self.config.verifiers.get(verifier_id)
        maximum = str(trigger.get("maximum_cost", "high"))
        return self._resource_semantics.verification_cost_admit(
            verifier.cost if verifier is not None else None,
            maximum,
        )

    def _debounce_ms(self) -> int:
        values = [
            int(trigger.get("debounce_ms", 0) or 0)
            for trigger in self.settings.get("triggers", [])
            if isinstance(trigger, dict)
        ]
        select = getattr(self._resource_semantics, "continuous_debounce_ms", None)
        if not callable(select):
            raise ForgeError(
                "NATIVE_CONTINUOUS_UNAVAILABLE",
                "continuous debounce selection requires the canonical MNCS Forge core",
            )
        return int(select(int(self.settings.get("debounce_ms", 0) or 0), values))

    def _current_identity(self, client: LanguageServiceSocket, uri: str) -> str | None:
        try:
            response = _mapping(client.request("document_diagnostics", {"uri": uri}))
            return _mapping(response.get("snapshot")).get("source_identity") or None
        except ForgeError:
            return None

    def _doctor_safe(
        self, client: LanguageServiceSocket, trigger: dict[str, object], event: dict[str, object]
    ) -> dict[str, object]:
        command = self.config.public_commands().get("mncs_doctor")
        current = _mapping(event.get("current"))
        uri = str(current.get("uri", ""))
        if not command:
            self._escalate(
                event,
                trigger,
                "automatic Safe Doctor repair is declared but mncs_doctor is not configured",
            )
            return {"status": "UNKNOWN", "reason": "mncs_doctor command is not declared"}
        try:
            relative = _source_path(uri, self.config.root).as_posix()
        except ForgeError as error:
            self._escalate(event, trigger, str(error))
            return {"status": "UNKNOWN", "reason": str(error)}
        expected_identity = str(current.get("identity", ""))
        if self._current_identity(client, uri) != expected_identity:
            self._escalate(event, trigger, "Safe repair source identity changed before promotion")
            return {"status": "UNKNOWN", "reason": "source identity conflict"}
        base = [
            *command,
            "fix",
            "--root",
            str(self.config.root),
            "--json",
            "--quiet",
            "--changed-path",
            relative,
            "--safe-only",
        ]
        dry = self._run_command(
            [*base, "--dry-run"],
            cwd=self.config.root,
            timeout=float(self.settings.get("max_run_seconds", 300)),
        )
        dry_report = self._stdout_json(dry)
        if dry_report is None:
            self._escalate(
                event,
                trigger,
                "Doctor Safe dry-run did not emit its machine-readable report",
            )
            return {
                "status": "UNKNOWN",
                "reason": "malformed Doctor dry-run report",
                "error_code": dry.get("error_code"),
                "resource_evidence": dry.get("resource_evidence"),
                "execution": dry,
            }
        planned = dry_report.get("planned_diffs")
        if not planned:
            self._record_status("PASS")
            return {
                "status": "PASS",
                "applied": [],
                "rounds": 0,
                "reason": "already canonical",
                "dry_run": dry_report,
            }
        applied_execution = self._run_command(
            base, cwd=self.config.root, timeout=float(self.settings.get("max_run_seconds", 300))
        )
        report = self._stdout_json(applied_execution)
        if report is None:
            self._escalate(
                event,
                trigger,
                "Doctor Safe apply did not emit its machine-readable report",
            )
            return {
                "status": "UNKNOWN",
                "reason": "malformed Doctor apply report",
                "error_code": applied_execution.get("error_code"),
                "resource_evidence": applied_execution.get("resource_evidence"),
                "execution": applied_execution,
            }
        convergence = (
            report.get("convergence") if isinstance(report.get("convergence"), list) else []
        )
        fix_ids = _list(report.get("fix_identities")) or _list(dry_report.get("fix_identities"))
        fix_ids.extend(
            str(item)
            for entry in convergence
            if isinstance(entry, dict)
            for item in _list(entry.get("applied"))
        )
        migration_ids = _list(report.get("migration_rule_identities")) or _list(
            dry_report.get("migration_rule_identities")
        )
        migration_ids.extend(
            str(step.get("id"))
            for migration in report.get("migrations", [])
            if isinstance(migration, dict)
            for step in migration.get("steps", [])
            if isinstance(step, dict) and step.get("id")
        )
        repair_rounds = max(
            [int(entry.get("iterations", 0)) for entry in convergence if isinstance(entry, dict)]
            or [0]
        )
        if planned and repair_rounds == 0:
            repair_rounds = 1
        result: dict[str, object] = {
            "schema_version": REPAIR_RESULT_SCHEMA,
            "original_workspace_generation": event.get("current_generation"),
            "original_source_identity": expected_identity,
            "resulting_source_identity": None,
            "doctor_fix_identities": sorted(set(fix_ids)),
            "migration_rule_identities": sorted(set(migration_ids)),
            "repair_rounds": repair_rounds,
            "validation_result": report.get("verification")
            or {"status": "PASS" if int(report.get("exit_code", 4)) in {0, 1} else "UNKNOWN"},
            "focused_verification_result": None,
            "failure_conflict_reason": None,
            "report": report,
        }
        try:
            client.request("refresh_workspace", {})
            result["resulting_source_identity"] = self._current_identity(client, uri)
            if not result["resulting_source_identity"]:
                result["failure_conflict_reason"] = (
                    "Language Service did not publish the repaired source identity"
                )
        except ForgeError as error:
            result["failure_conflict_reason"] = str(error)
        repair_summary = {
            "original_source_identity": result.get("original_source_identity"),
            "resulting_source_identity": result.get("resulting_source_identity"),
            "doctor_fix_identities": list(result.get("doctor_fix_identities", []))[:32],
            "migration_rule_identities": list(result.get("migration_rule_identities", []))[:32],
            "repair_rounds": result.get("repair_rounds"),
            "validation_result": _compact_verification_result(result.get("validation_result")),
            "focused_verification_result": None,
            "failure_conflict_reason": str(result.get("failure_conflict_reason") or "")[:512]
            or None,
        }
        self._remember_repair(repair_summary)
        self._persist_repair(event, result, applied_execution)
        status = "PASS" if result.get("failure_conflict_reason") is None else "UNKNOWN"
        self._record_status(status)
        if status != "PASS":
            self._escalate(
                event,
                trigger,
                "automatic Safe Doctor repair was not validated",
                status=status,
            )
        return {"status": status, "repair": result}

    def _persist_repair(
        self, event: dict[str, object], repair: dict[str, object], execution: dict[str, object]
    ) -> None:
        fields = {
            "candidate_identity": str(repair.get("original_source_identity")),
            "subject_type": "continuous-safe-repair",
            "provider_or_evaluator_identity": "mncs-doctor",
            "method": "safe-fixpoint",
            "workflow": "continuous.safe-repair",
            "category": "mncs_bundle_validation",
            "scope": "workspace-generation",
            "environment": {"workspace_generation": event.get("current_generation")},
            "duration_seconds": execution.get("duration_seconds"),
            "status": "PASS" if repair.get("failure_conflict_reason") is None else "UNKNOWN",
            "witnesses_or_counterexamples": [repair],
            "limitations": [
                "Doctor owns migration semantics; Forge records only the bounded repair projection."
            ],
            "unsupported_constructs": [],
            "stderr_diagnostic": str(execution.get("stderr", ""))[-4096:],
            "returncode": execution.get("returncode"),
            "protocol_request_identity": local_json_identity(
                {
                    "operation": "continuous.safe-repair",
                    "event_cursor": event.get("cursor"),
                    "workspace_generation": event.get("current_generation"),
                    "source_identity": repair.get("original_source_identity"),
                }
            ),
            "recorded_at": now(),
        }
        try:
            record = new_record(RecordType.WORKFLOW_RESULT, fields)
            self.forge.record_store.commit("results", "result", record)
        except (
            Exception
        ) as error:  # durable evidence failure must be visible, not fatal to source state
            repair["store_persistence"] = {"status": "UNKNOWN", "reason": str(error)}

    def _ravel_plan(self, event: dict[str, object], path: Path, run_dir: Path) -> dict[str, object]:
        commands = self.config.public_commands()
        ravel = commands.get("ravel_impact")
        mncs = commands.get("mncs")
        impact = _mapping(event.get("impact"))
        roots = _list(impact.get("roots"))
        if not ravel or not mncs or not roots:
            raise ForgeError(
                "VERIFICATION_UNKNOWN", "RAVEL, mncs, and compiler roots must be declared"
            )
        change_class = next(iter(_list(impact.get("change_kinds"))), "implementation")
        allowed = {
            "implementation",
            "public_contract",
            "shared_type",
            "parser_semantics",
            "serialization_format",
            "effect_semantics",
            "abi_boundary",
            "cross_repository_contract",
        }
        if change_class not in allowed:
            change_class = "implementation"
        plan_path = run_dir / "verification-plan.json"
        command = [*ravel, str(path), "--mncs", mncs[0]]
        for root in roots:
            command.extend(("--root", root))
        for library in self.settings.get("library_paths", []):
            command.extend(("--library", str(self.config.root / str(library))))
        cross_repository = bool(self.settings.get("cross_repository", False)) or (
            change_class == "cross_repository_contract"
        )
        if cross_repository:
            command.append("--cross-repository")
        for option, setting in (
            ("--family-graph", "family_graph_file"),
            ("--commons-root", "commons_root"),
            ("--obligation-inventory", "obligation_inventory"),
            ("--current-evidence", "current_evidence"),
            ("--obligation-output", "obligation_output"),
        ):
            value = self.settings.get(setting)
            if value:
                command.extend((option, str(self._family_path(value, label=setting))))
        repository = self.settings.get("repository")
        if repository:
            command.extend(("--repository", str(repository)))
        command.extend(
            (
                "--change-class",
                change_class,
                "--cwd",
                str(self.config.root),
                "--output",
                str(plan_path),
            )
        )
        execution = self._run_command(
            command, cwd=self.config.root, timeout=float(self.settings.get("max_run_seconds", 300))
        )
        if execution.get("returncode") not in {0, None} or not plan_path.is_file():
            raise ForgeError(
                "VERIFICATION_UNKNOWN",
                f"RAVEL did not produce a plan: {execution.get('stderr', '')}",
                details={"resource_evidence": execution.get("resource_evidence", {})},
            )
        try:
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ForgeError(
                "VERIFICATION_UNKNOWN", f"RAVEL plan is not valid JSON: {error}"
            ) from error
        if not isinstance(plan, dict):
            raise ForgeError("VERIFICATION_UNKNOWN", "RAVEL plan is not an object")
        self.forge._mncs_development_service._validate_verification_plan(plan)  # type: ignore[attr-defined]
        return plan

    def _selected_verification(
        self, client: LanguageServiceSocket, event: dict[str, object], trigger: dict[str, object]
    ) -> dict[str, object]:
        impact = _mapping(event.get("impact"))
        if not bool(event.get("impact_complete", False)) or not bool(impact.get("complete", False)):
            self._escalate(
                event,
                trigger,
                "compiler semantic impact is incomplete; exact verification remains UNKNOWN",
            )
            self._record_status("UNKNOWN")
            return {
                "status": "UNKNOWN",
                "reason": "incomplete compiler impact",
                "tier": "incremental",
            }
        manifest = self.settings.get("test_manifest")
        if not manifest:
            self._escalate(
                event,
                trigger,
                "continuous verification trigger matched but test_manifest is not configured",
            )
            self._record_status("UNKNOWN")
            return {"status": "UNKNOWN", "reason": "test manifest is not declared"}
        source_uri = str(_mapping(event.get("current")).get("uri", ""))
        source_path = self.config.root / _source_path(source_uri, self.config.root)
        run_dir = (
            self.config.state_dir
            / "continuous"
            / "runs"
            / f"g{event.get('current_generation')}-c{event.get('cursor')}"
        )
        run_dir.mkdir(parents=True, exist_ok=True)
        try:
            plan = self._ravel_plan(event, source_path, run_dir)
            selected = _list(_mapping(plan.get("selection")).get("selected_test_identities"))
            self._set_selected_test_ids(selected)
            commands = self.config.public_commands()
            mncs = commands.get("mncs")
            loop = self.forge.mncs_failure_loop(
                manifest=str(manifest),
                test_command=commands.get("mncs_test"),
                debug_command=commands.get("mncs_debug"),
                mncs_binary=mncs[0] if mncs else None,
                library_paths=[
                    str(self.config.root / str(value))
                    for value in self.settings.get("library_paths", [])
                ],
                working_directory=str(self.settings.get("test_working_directory", ".")),
                test_result_file=str(
                    (run_dir / "mncs-test-result.json").relative_to(self.config.root)
                ),
                test_check_file=str(
                    (run_dir / "mncs-test-check.json").relative_to(self.config.root)
                ),
                test_artifacts_directory=str(
                    (run_dir / "mncs-test-artifacts").relative_to(self.config.root)
                ),
                debug_witness_file=str(
                    (run_dir / "mncs-debug-witness.json").relative_to(self.config.root)
                ),
                debug_artifacts_directory=str(
                    (run_dir / "mncs-debug-artifacts").relative_to(self.config.root)
                ),
                capture_policy="events",
                verification_plan_file=str(
                    (run_dir / "verification-plan.json").relative_to(self.config.root)
                ),
                ravel_command=commands.get("ravel_impact"),
                actions_evidence_files=[
                    str(value) for value in self.settings.get("actions_evidence_files", [])
                ],
                actions_command=commands.get("mncs_actions"),
                family_graph_file=(
                    str(self.settings.get("family_graph_file"))
                    if self.settings.get("family_graph_file")
                    else None
                ),
                family_workspace_root=(
                    str(self.settings.get("family_workspace_root"))
                    if self.settings.get("family_workspace_root")
                    else None
                ),
                family_proof_directory=str(
                    self.settings.get("family_proof_directory", ".mncs-forge/family-proof")
                ),
                timeout_seconds=float(self.settings.get("max_run_seconds", self.config.timeout)),
                output_file=str((run_dir / "failure-loop.json").relative_to(self.config.root)),
            )
        except ForgeError as error:
            try:
                current = _mapping(client.request("workspace_status", {}))
            except ForgeError:
                current = {}
            if current:
                freshness = self._native_completed_freshness(
                    event,
                    candidate=str(self.settings.get("candidate_identity") or "event"),
                    verifier_id="selected-verification",
                    observed_generation=int(
                        current.get("generation", event.get("current_generation", 0))
                    ),
                )
                if freshness.disposition == "DiscardStale":
                    self.stale_jobs = min(_COUNTER_MAX, self.stale_jobs + 1)
                    return {
                        "status": "STALE",
                        "reason": "superseded while verification was interrupted",
                        "error_code": error.code,
                        "selected_test_identities": list(self.selected_test_ids),
                        "selected_test_count": self.selected_test_count,
                    }
            self._escalate(event, trigger, str(error))
            self._record_status("UNKNOWN")
            result: dict[str, object] = {
                "status": "UNKNOWN",
                "reason": str(error),
                "selected_test_identities": list(self.selected_test_ids),
                "selected_test_count": self.selected_test_count,
                "selected_test_identities_truncated": self.selected_test_count
                > CONTINUOUS_SELECTED_TEST_CAPACITY,
            }
            result["error_code"] = error.code
            if isinstance(error.details.get("resource_evidence"), dict):
                result["resource_evidence"] = _compact_resource_evidence(
                    error.details["resource_evidence"]
                )
            return result
        current = _mapping(client.request("workspace_status", {}))
        freshness = self._native_completed_freshness(
            event,
            candidate=str(self.settings.get("candidate_identity") or "event"),
            verifier_id="selected-verification",
            observed_generation=int(current.get("generation", 0)),
        )
        if freshness.disposition == "DiscardStale":
            self.stale_jobs = min(_COUNTER_MAX, self.stale_jobs + 1)
            self._record_status("UNKNOWN")
            return {
                "status": "STALE",
                "reason": "superseded while verification was running",
                "selected_test_identities": selected,
            }
        verdict = str(loop.get("verdict", _mapping(loop.get("test")).get("verdict", "UNKNOWN")))
        if verdict not in {"PASS", "FAIL", "UNKNOWN"}:
            verdict = "UNKNOWN"
        self._record_status(verdict)
        if verdict in {"FAIL", "UNKNOWN"}:
            self._escalate(
                event,
                trigger,
                f"selected verification reached {verdict}",
                status=verdict,
            )
        return {
            "status": verdict,
            "selected_test_identities": sorted(set(selected)),
            "selected_test_count": len(selected),
            "available_test_count": _mapping(plan.get("selection")).get("available_test_count"),
            "plan_id": plan.get("plan_id"),
            "failure_loop": loop,
            "tier": "incremental",
        }

    def _reusable_result(
        self, event: dict[str, object], trigger: dict[str, object], verifier_id: str
    ) -> dict[str, object] | None:
        candidate = self.settings.get("candidate_identity")
        if not isinstance(candidate, str) or not candidate:
            return None
        verifier = self.config.verifiers.get(verifier_id)
        if verifier is None:
            return None
        provider = self.config.providers[verifier.provider_id]
        workflow = self.config.workflows[verifier.workflow]
        environment = self.config.environment(workflow)
        service = self.forge._verifier_service  # type: ignore[attr-defined]
        identities = service._material_identities(verifier, provider, workflow, environment)
        try:
            _source_path(str(_mapping(event.get("current")).get("uri", "")), self.config.root)
        except ForgeError:
            return None
        for entry in reversed(self.forge.ledger.records("verifier_result")):
            payload = entry.payload.to_object_dict()
            if payload.get("verifier_id") != verifier_id or payload.get("mode") != self.forge.mode:
                continue
            if payload.get("candidate_identity") != candidate:
                continue
            if any(payload.get(key) != value for key, value in identities.items()):
                continue
            recorded_inputs = _mapping(payload.get("input_identities"))
            if recorded_inputs.get("candidate_identity") != candidate:
                continue
            if any(
                recorded_inputs.get(key) != expected
                for key, expected in {
                    "contract_identity": None,
                    "dependency_slice_identities": {},
                    "prior_artifact_identity": None,
                    "question_parameters_identity": local_json_identity({}),
                }.items()
            ):
                continue
            envelope = _mapping(payload.get("dependency_envelope"))
            if envelope.get("complete") is not True:
                continue
            freshness = service._freshness(payload, allow_protected=True)
            if freshness.get("state") != "CURRENT":
                continue
            return {
                "status": payload.get("status", "UNKNOWN"),
                "output_identity": payload.get("output_identity"),
                "reused": True,
                "freshness": freshness,
            }
        return None

    def _defer_micro_verifiers(
        self,
        event: dict[str, object],
        trigger: dict[str, object],
        candidate: str,
        verifier_ids: list[str],
        reason: str,
    ) -> int:
        """Apply native transitions to deferred obligations and return unresolved count."""

        unresolved_count = 0
        for index, verifier_id in enumerate(verifier_ids):
            transition = self._native_resource_transition(
                event=event,
                candidate=candidate,
                verifier_id=verifier_id,
                outcome="Unknown",
                evidence_status="NotRun",
                has_outcome=False,
                queue_remaining=len(verifier_ids) - index - 1,
                resource_gate_closed=True,
                in_flight=False,
            )
            if transition.disposition in {"CancelStale", "DiscardStale"}:
                self.stale_jobs = min(_COUNTER_MAX, self.stale_jobs + 1)
                continue
            if transition.retain_current_pending:
                self._remember_micro_pending(
                    event,
                    trigger,
                    candidate,
                    verifier_id,
                    reason,
                )
                self.deferred_jobs = min(_COUNTER_MAX, self.deferred_jobs + 1)
            if transition.disposition == "EscalateUnknown":
                self._escalate(
                    event,
                    trigger,
                    reason,
                    status="UNKNOWN",
                    tier="micro",
                )
            if transition.retain_current_pending or transition.disposition == "EscalateUnknown":
                unresolved_count += 1
        return unresolved_count

    @staticmethod
    def _resource_evidence_in_result(value: object) -> dict[str, object]:
        result = _mapping(value)
        extensions = _mapping(result.get("extensions"))
        mncs = _mapping(extensions.get("mncs_forge"))
        return _mapping(mncs.get("resource_evidence") or result.get("resource_evidence"))

    def _micro(self, event: dict[str, object], trigger: dict[str, object]) -> dict[str, object]:
        work_generation = int(event.get("current_generation", 0))
        self.current_generation = max(self.current_generation, work_generation)
        verifier_ids = _list(trigger.get("verifier_ids"))
        if not verifier_ids:
            self._escalate(event, trigger, f"trigger {trigger.get('id')} has no verifier_ids")
            return {"status": "UNKNOWN", "reason": "no verifier declared"}
        candidate = self.settings.get("candidate_identity")
        if not isinstance(candidate, str) or not candidate:
            self._escalate(
                event, trigger, "micro-verifier matched without a configured candidate identity"
            )
            self._record_status("UNKNOWN")
            return {"status": "UNKNOWN", "reason": "candidate identity unavailable"}
        results = []
        selected_count, deferred_count = self._native_queue_admission(len(verifier_ids))
        selected_ids = verifier_ids[:selected_count]
        capacity_deferred = verifier_ids[selected_count : selected_count + deferred_count]
        unresolved_count = 0
        for index, verifier_id in enumerate(selected_ids):
            if not self._trigger_cost_allowed(trigger, verifier_id):
                self._escalate(
                    event,
                    trigger,
                    f"trigger {trigger.get('id')} exceeds declared verifier cost boundary",
                )
                self._record_status("UNKNOWN")
                results.append(
                    {
                        "verifier_id": verifier_id,
                        "status": "UNKNOWN",
                        "reason": "cost boundary",
                        "reused": False,
                    }
                )
                continue
            reused = self._reusable_result(event, trigger, verifier_id)
            if reused is not None:
                self.reused_evidence = min(_COUNTER_MAX, self.reused_evidence + 1)
                results.append({"verifier_id": verifier_id, **reused})
                reused_status = str(reused["status"])
                self._record_status(reused_status)
                reused_resource = self._resource_evidence_in_result(reused)
                raw_outcome = reused_resource.get("resource_outcome")
                resource_outcome_observed = isinstance(raw_outcome, str)
                outcome = str(raw_outcome) if resource_outcome_observed else "Unknown"
                evidence_status = (
                    "Pass"
                    if reused_status == "PASS"
                    else "Fail"
                    if reused_status == "FAIL"
                    else "Unknown"
                )
                transition = self._native_resource_transition(
                    event=event,
                    candidate=candidate,
                    verifier_id=verifier_id,
                    outcome=outcome,
                    evidence_status=evidence_status,
                    has_outcome=True,
                    queue_remaining=(
                        len(selected_ids) - index - 1 + len(capacity_deferred)
                    ),
                    resource_outcome_observed=resource_outcome_observed,
                )
                if transition.disposition == "Resolve":
                    self._resolve_micro_pending(event, candidate, verifier_id)
                elif transition.retain_current_pending:
                    self._remember_micro_pending(
                        event,
                        trigger,
                        candidate,
                        verifier_id,
                        "reused verification remains unresolved",
                        reused,
                    )
                    self.deferred_jobs = min(_COUNTER_MAX, self.deferred_jobs + 1)
                if transition.defer_remaining:
                    capacity_deferred = [*selected_ids[index + 1 :], *capacity_deferred]
                    unresolved_count += self._defer_micro_verifiers(
                        event,
                        trigger,
                        candidate,
                        capacity_deferred,
                        "remaining micro-verifiers deferred by native resource policy",
                    )
                    capacity_deferred = []
                    break
                if transition.disposition in {"CancelStale", "DiscardStale"}:
                    self.stale_jobs = min(_COUNTER_MAX, self.stale_jobs + 1)
                    break
                continue
            self.recomputed_evidence = min(_COUNTER_MAX, self.recomputed_evidence + 1)
            try:
                path = _source_path(
                    str(_mapping(event.get("current")).get("uri", "")), self.config.root
                ).as_posix()
                result = self.forge.verifier_run(
                    verifier_id, candidate_identity=candidate, changed_paths=[path]
                )
                envelope_complete = (
                    _mapping(result.get("dependency_envelope")).get("complete") is True
                )
                if (
                    bool(trigger.get("complete_dependency_envelope", False))
                    and not envelope_complete
                ):
                    result = {
                        **result,
                        "status": "UNKNOWN",
                        "reuse_blocked": "verifier did not declare a complete dependency envelope",
                    }
                result_status = str(result.get("status", "UNKNOWN"))
                resource_evidence = self._resource_evidence_in_result(result)
                raw_outcome = resource_evidence.get("resource_outcome")
                resource_outcome_observed = isinstance(raw_outcome, str)
                outcome = str(raw_outcome) if resource_outcome_observed else "Unknown"
                evidence_status = (
                    "Pass"
                    if result_status == "PASS"
                    else "Fail"
                    if result_status == "FAIL"
                    else "Unknown"
                )
                transition = self._native_resource_transition(
                    event=event,
                    candidate=candidate,
                    verifier_id=verifier_id,
                    outcome=str(outcome),
                    evidence_status=evidence_status,
                    has_outcome=True,
                    queue_remaining=(
                        len(selected_ids) - index - 1 + len(capacity_deferred)
                    ),
                    resource_outcome_observed=resource_outcome_observed,
                )
                if transition.disposition in {"CancelStale", "DiscardStale"}:
                    result = {
                        **result,
                        "status": "UNKNOWN",
                        "freshness": "STALE",
                        "resource_evidence": {
                            **resource_evidence,
                            "resource_outcome": "Stale",
                        },
                    }
                    result_status = "UNKNOWN"
                results.append({"verifier_id": verifier_id, **result, "reused": False})
                self._record_status(result_status)
                if transition.retain_current_pending:
                    self.deferred_jobs = min(_COUNTER_MAX, self.deferred_jobs + 1)
                    self._remember_micro_pending(
                        event,
                        trigger,
                        candidate,
                        verifier_id,
                        "current micro-verifier remains pending under native resource policy",
                        result,
                    )
                if transition.disposition == "Resolve":
                    self._resolve_micro_pending(event, candidate, verifier_id)
                if transition.disposition == "EscalateUnknown":
                    self._escalate(
                        event,
                        trigger,
                        "native continuous policy could not retain this verification obligation",
                        status="UNKNOWN",
                        tier="micro",
                    )
                if transition.defer_remaining:
                    capacity_deferred = [*selected_ids[index + 1 :], *capacity_deferred]
                    unresolved_count += self._defer_micro_verifiers(
                        event,
                        trigger,
                        candidate,
                        capacity_deferred,
                        "later micro-verifiers deferred by native resource policy",
                    )
                    capacity_deferred = []
                    break
                if transition.disposition in {"CancelStale", "DiscardStale"}:
                    self.stale_jobs = min(_COUNTER_MAX, self.stale_jobs + 1)
                    break
            except ForgeError as error:
                self._escalate(event, trigger, f"micro-verifier {verifier_id}: {error}")
                self._record_status("UNKNOWN")
                failure: dict[str, object] = {
                    "verifier_id": verifier_id,
                    "status": "UNKNOWN",
                    "reason": str(error)[:512],
                    "error_code": error.code,
                    "reused": False,
                }
                if isinstance(error.details.get("resource_evidence"), dict):
                    failure["resource_evidence"] = _compact_resource_evidence(
                        error.details["resource_evidence"]
                    )
                results.append(failure)
                resource_evidence = _mapping(error.details.get("resource_evidence"))
                raw_outcome = resource_evidence.get("resource_outcome")
                resource_outcome_observed = isinstance(raw_outcome, str)
                outcome = str(raw_outcome) if resource_outcome_observed else "Unknown"
                gate_closed = bool(
                    resource_evidence.get("verification_deferred")
                    or resource_evidence.get("deferred")
                )
                transition = self._native_resource_transition(
                    event=event,
                    candidate=candidate,
                    verifier_id=verifier_id,
                    outcome=str(outcome),
                    evidence_status="Unknown",
                    has_outcome=True,
                    queue_remaining=(
                        len(selected_ids) - index - 1 + len(capacity_deferred)
                    ),
                    resource_outcome_observed=resource_outcome_observed,
                    resource_gate_closed=gate_closed,
                )
                if transition.retain_current_pending:
                    self.deferred_jobs = min(_COUNTER_MAX, self.deferred_jobs + 1)
                    self._remember_micro_pending(
                        event,
                        trigger,
                        candidate,
                        verifier_id,
                        "current micro-verifier remains pending under native resource policy",
                        failure,
                    )
                if transition.disposition == "EscalateUnknown":
                    self._escalate(
                        event,
                        trigger,
                        "native continuous policy could not retain this verification obligation",
                        status="UNKNOWN",
                        tier="micro",
                    )
                if transition.defer_remaining:
                    capacity_deferred = [*selected_ids[index + 1 :], *capacity_deferred]
                    unresolved_count += self._defer_micro_verifiers(
                        event,
                        trigger,
                        candidate,
                        capacity_deferred,
                        "later micro-verifiers deferred by native resource policy",
                    )
                    capacity_deferred = []
                    break
                if transition.disposition in {"CancelStale", "DiscardStale"}:
                    self.stale_jobs = min(_COUNTER_MAX, self.stale_jobs + 1)
                    break
        if capacity_deferred:
            unresolved_count += self._defer_micro_verifiers(
                event,
                trigger,
                candidate,
                capacity_deferred,
                "per-event micro-verifier capacity reached",
            )
        decide_status = getattr(self._resource_semantics, "verification_status_decide", None)
        if not callable(decide_status):
            raise ForgeError(
                "NATIVE_CONTINUOUS_UNAVAILABLE",
                "micro-verifier status aggregation requires the canonical MNCS status lattice",
            )
        status_decision = decide_status(
            [str(result.get("status", "UNKNOWN")) for result in results],
            verification_required=True,
            unresolved_count=unresolved_count,
        )
        status = status_decision.status
        if status_decision.escalation_required:
            self._escalate(
                event,
                trigger,
                f"micro-verification reached {status}",
                status=status,
                tier="micro",
            )
        return {"status": status, "results": results, "tier": "micro"}

    def _process_event(
        self, client: LanguageServiceSocket, event: dict[str, object]
    ) -> dict[str, object]:
        generation = int(event.get("current_generation", 0))
        self.current_generation = max(self.current_generation, generation)
        configured_candidate = self.settings.get("candidate_identity")
        transition = self._native_resource_transition(
            event=event,
            candidate=configured_candidate if isinstance(configured_candidate, str) else "event",
            verifier_id="event-generation",
            outcome="Unknown",
            evidence_status="NotRun",
            has_outcome=False,
            queue_remaining=0,
        )
        if transition.disposition == "DiscardStale":
            self.stale_jobs = min(_COUNTER_MAX, self.stale_jobs + 1)
            return {"status": "STALE", "reason": "event generation is older than current"}
        self._cancel_superseded_pending(event)
        self.current_source_identity = _mapping(event.get("current")).get("identity") or None
        self.current_cursor = max(self.current_cursor, int(event.get("cursor", 0)))
        triggers = self.settings.get("triggers", [])
        try:
            matched = [
                trigger
                for trigger in triggers
                if isinstance(trigger, dict) and self._matches(trigger, event)
            ]
        except ForgeError as error:
            self._attention(event, f"native trigger decision is unknown: {error}")
            self._record_status("UNKNOWN")
            return {"generation": generation, "cursor": event.get("cursor"), "status": "UNKNOWN"}
        action_results = []
        repaired = False
        for trigger in matched:
            action = str(trigger.get("action"))
            if action == "doctor_safe":
                with self._job_scope(event, trigger, action):
                    result = self._doctor_safe(client, trigger, event)
                action_results.append(
                    {"trigger": trigger.get("id"), "action": action, "result": result}
                )
                repaired = repaired or result.get("repair") is not None
        if repaired:
            self.active_tier = "micro"
            return {
                "generation": generation,
                "cursor": event.get("cursor"),
                "status": "PASS",
                "repair_pending_rebound": True,
                "actions": action_results,
            }
        for trigger in matched:
            action = str(trigger.get("action"))
            if action == "verification_plan":
                self.active_tier = "incremental"
                with self._job_scope(event, trigger, action, client=client):
                    result = self._selected_verification(client, event, trigger)
                action_results.append(
                    {
                        "trigger": trigger.get("id"),
                        "action": action,
                        "result": result,
                    }
                )
            elif action in {"micro_verifier", "security_micro_verifier"}:
                self.active_tier = "micro"
                with self._job_scope(event, trigger, action, client=client):
                    result = self._micro(event, trigger)
                action_results.append(
                    {
                        "trigger": trigger.get("id"),
                        "action": action,
                        "result": result,
                    }
                )
        rebound_verification = next(
            (
                action.get("result")
                for action in action_results
                if action.get("action") == "verification_plan"
            ),
            None,
        )
        if rebound_verification is not None:
            identity = self.current_source_identity
            for repair in reversed(self.repairs):
                if (
                    repair.get("resulting_source_identity") == identity
                    and repair.get("focused_verification_result") is None
                ):
                    repair["focused_verification_result"] = rebound_verification
                    break
        decide_status = getattr(self._resource_semantics, "verification_status_decide", None)
        if not callable(decide_status):
            raise ForgeError(
                "NATIVE_CONTINUOUS_UNAVAILABLE",
                "continuous result aggregation requires the canonical MNCS status lattice",
            )
        status_decision = decide_status(
            [
                str(_mapping(action.get("result")).get("status", "PASS"))
                for action in action_results
                if isinstance(action, dict)
            ],
            verification_required=False,
        )
        if status_decision.escalation_required:
            return {
                "generation": generation,
                "cursor": event.get("cursor"),
                "status": status_decision.status,
                "actions": action_results,
            }
        try:
            current = _mapping(client.request("workspace_status", {}))
        except ForgeError as error:
            self._attention(event, f"could not confirm continuous result freshness: {error}")
            self._record_status("UNKNOWN")
            return {
                "generation": generation,
                "cursor": event.get("cursor"),
                "status": "UNKNOWN",
                "actions": action_results,
            }
        freshness = self._native_completed_freshness(
            event,
            candidate=(
                configured_candidate
                if isinstance(configured_candidate, str)
                else "event"
            ),
            verifier_id="continuous-event-result",
            observed_generation=int(current.get("generation", generation)),
        )
        if freshness.disposition == "DiscardStale":
            self.stale_jobs = min(_COUNTER_MAX, self.stale_jobs + 1)
            self._record_status("UNKNOWN")
            return {
                "generation": generation,
                "cursor": event.get("cursor"),
                "status": "STALE",
                "reason": "superseded while continuous work was running",
                "actions": action_results,
            }
        return {
            "generation": generation,
            "cursor": event.get("cursor"),
            "status": "PASS",
            "actions": action_results,
        }

    def status(self) -> dict[str, object]:
        counts = {key: self.status_counts.get(key, 0) for key in ("PASS", "FAIL", "UNKNOWN")}
        runner = getattr(getattr(self, "forge", None), "_executor", None)
        resource_status = getattr(runner, "resource_status", None)
        resources = (
            resource_status()
            if callable(resource_status)
            else {
                "state": "unknown",
                "mechanism": None,
                "limitation": "the configured Runner does not expose resource status",
            }
        )
        native = getattr(self.forge, "_native", None)
        cache_status = getattr(native, "cache_status", None)
        record_store = getattr(self.forge, "record_store", None)
        store_status = getattr(record_store, "resident_status", None)
        resources = {
            **resources,
            "configured_output_limit_bytes": int(getattr(self.config, "output_cap", 0)),
            "native_projection_caches": cache_status() if callable(cache_status) else {},
            "store_projection": store_status() if callable(store_status) else {},
        }
        self.resource_exhaustion_events = max(
            self.resource_exhaustion_events,
            int(resources.get("resource_exhaustion_events", 0) or 0),
        )
        self.deferred_jobs = max(self.deferred_jobs, int(resources.get("deferred_jobs", 0) or 0))
        active_job = dict(self.active_job) if self.active_job is not None else None
        if active_job is not None:
            active_job["cancellation_requested"] = self._stop_requested
            started = active_job.get("started_monotonic")
            if isinstance(started, (int, float)) and not isinstance(started, bool):
                active_job["elapsed_seconds"] = round(time.monotonic() - float(started), 3)
        return {
            "schema_version": CONTINUOUS_STATUS_SCHEMA,
            "workspace_generation": self.current_generation,
            "current_source_identity": self.current_source_identity,
            "pending_checks": [
                _compact_verification_result(item)
                for item in list(self.pending.values())[-CONTINUOUS_PENDING_CAPACITY:]
            ],
            "pending_check_capacity": CONTINUOUS_PENDING_CAPACITY,
            "pending_check_overflow_count": self.pending_overflow_count,
            "pending_check_overflow_identity": self.pending_overflow_identity,
            "counts": counts,
            "stale_evidence_count": self.stale_jobs,
            "automatic_repairs_applied": list(self.repairs),
            "repair_history_capacity": CONTINUOUS_REPAIR_CAPACITY,
            "repair_history_evictions": self.repair_evictions,
            "selected_test_identities": list(self.selected_test_ids),
            "selected_test_count": self.selected_test_count,
            "selected_test_identities_truncated": self.selected_test_count
            > CONTINUOUS_SELECTED_TEST_CAPACITY,
            "active_verification_tier": self.active_tier,
            "blocking_attention_events": list(self.attention),
            "attention_capacity": CONTINUOUS_ATTENTION_CAPACITY,
            "attention_evictions": self.attention_evictions,
            "active_job": active_job,
            "cancellation_requested": self._stop_requested,
            "last_cancellation": getattr(self, "last_cancellation", None),
            "queue": {
                "depth": 0,
                "capacity": 1,
                "policy": "single synchronous job; incoming same-source events coalesce",
            },
            "event_cursor": self.current_cursor,
            "event_stream_identity": self.stream_identity,
            "evidence_reused": self.reused_evidence,
            "evidence_recomputed": self.recomputed_evidence,
            "queued_jobs_cancelled": self.cancelled_jobs,
            "deferred_jobs": self.deferred_jobs,
            "resource_exhaustion_events": self.resource_exhaustion_events,
            "resources": resources,
            "limitations": [
                (
                    "ReferenceCompiler frontend work remains synchronous; late results are "
                    "discarded by generation identity."
                ),
                (
                    "The bounded Language Service cursor may require a reset after transient "
                    "event history ages out."
                ),
            ],
        }

    def read_status(self) -> dict[str, object]:
        path = self._status_path()
        value = _read_json_path(path)
        return value if value is not None else self.status()

    def run(
        self,
        *,
        once: bool = False,
        max_events: int | None = None,
        after_cursor: int | None = None,
        poll_interval_seconds: float | None = None,
    ) -> dict[str, object]:
        if not bool(self.settings.get("enabled", False)):
            self._attention(
                {"current_generation": 0},
                "continuous mode is not enabled in Forge configuration",
                tier="edit-time",
            )
            return self.status()
        client = self._socket()
        requested_limit = int(max_events or self.settings.get("poll_max_events", 32))
        limit = min(max(requested_limit, 1), CONTINUOUS_EVENT_BATCH_CAPACITY)
        interval = float(poll_interval_seconds or self.settings.get("poll_interval_seconds", 0.2))
        try:
            client.request("refresh_workspace", {})
            prior = self.read_status()
            self._restore_pending(prior)
            prior_stream = prior.get("event_stream_identity")
            if self.stream_identity is None and isinstance(prior_stream, str) and prior_stream:
                self.stream_identity = prior_stream
            if after_cursor is None:
                after_cursor = int(prior.get("event_cursor", 0))
            live_status = _mapping(client.request("workspace_status", {}))
            live_stream = live_status.get("stream_identity")
            if self.stream_identity is None and isinstance(live_stream, str) and live_stream:
                self.stream_identity = live_stream
            poll = self._poll(client, after_cursor=after_cursor, max_events=limit)
        except ForgeError as error:
            self._attention({"current_generation": 0}, str(error), tier="edit-time")
            self._record_status("UNKNOWN")
            result = self.status()
            result["transport"] = "UNKNOWN"
            self._write_status(result)
            return result
        if bool(poll.get("reset_required")):
            self._attention(
                {"current_generation": 0},
                "Language Service event cursor reset required",
                tier="edit-time",
            )
            self._record_status("UNKNOWN")
        events = [event for event in poll.get("events", []) if isinstance(event, dict)]
        try:
            debounce_ms = self._debounce_ms()
        except ForgeError as error:
            self._attention(
                {"current_generation": self.current_generation},
                f"native debounce decision is unknown: {error}",
            )
            self._record_status("UNKNOWN")
            result = self.status()
            result["transport"] = "UNKNOWN"
            self._write_status(result)
            return result
        if events and debounce_ms:
            # A debounce window is a bounded event-collection policy, not a
            # semantic guess.  Poll from the original cursor so a rapid edit
            # burst is coalesced by the same identity logic below; duplicate
            # cursors are removed before dispatch.
            time.sleep(debounce_ms / 1000)
            try:
                client.request("refresh_workspace", {})
                debounced = _mapping(
                    self._poll(client, after_cursor=after_cursor, max_events=limit)
                )
                seen_cursors = {
                    int(event.get("cursor", -1))
                    for event in events
                    if isinstance(event.get("cursor"), int)
                }
                events.extend(
                    event
                    for event in debounced.get("events", [])
                    if isinstance(event, dict) and int(event.get("cursor", -1)) not in seen_cursors
                )
                poll["current_cursor"] = max(
                    int(poll.get("current_cursor", 0)),
                    int(debounced.get("current_cursor", 0)),
                )
            except ForgeError as error:
                self._attention(
                    {"current_generation": self.current_generation},
                    f"debounce poll failed: {error}",
                )
                self._record_status("UNKNOWN")
        if events:
            observed_generation = max(
                int(event.get("current_generation", 0)) for event in events
            )
            self.current_generation = max(self.current_generation, observed_generation)
        result_history: deque[dict[str, object]] = deque(maxlen=CONTINUOUS_RECENT_RESULTS)
        events_processed = 0

        def record_result(value: dict[str, object]) -> None:
            nonlocal events_processed
            events_processed = min(_COUNTER_MAX, events_processed + 1)
            result_history.append(_compact_event_result(value))

        def persist_runtime_status() -> None:
            runtime = self.status()
            runtime["events_processed"] = events_processed
            runtime["event_results"] = list(result_history)
            self._write_status(runtime)

        ingress = _ContinuousEventIngress(
            self,
            client,
            after_cursor=int(poll.get("current_cursor", self.current_cursor)),
            max_events=limit,
            poll_interval=interval,
        )
        ingress_started = bool(events) or not once
        if ingress_started:
            ingress.start()
        try:
            for event in events:
                record_result(self._process_event(client, event))
                self.current_cursor = max(self.current_cursor, int(event.get("cursor", 0)))
            self.current_cursor = max(
                self.current_cursor, int(poll.get("current_cursor", self.current_cursor))
            )
            persist_runtime_status()
            if once:
                # The shared ingress stays attached while a verifier runs.
                # Drain its bounded queue after each dispatch so edits that
                # superseded the active generation are handled in this same
                # one-shot invocation.
                quiet_intervals = 0
                for _ in range(8):
                    if not ingress_started:
                        break
                    try:
                        first = ingress.events.get(timeout=max(interval, 0.05))
                    except queue.Empty:
                        quiet_intervals += 1
                        if quiet_intervals >= 2:
                            break
                        continue
                    quiet_intervals = 0
                    batch = [first]
                    while True:
                        try:
                            batch.append(ingress.events.get_nowait())
                        except queue.Empty:
                            break
                    for event in batch:
                        record_result(self._process_event(client, event))
                        self.current_cursor = max(
                            self.current_cursor, int(event.get("cursor", 0))
                        )
                    persist_runtime_status()
                    if ingress.failure is not None:
                        break
            else:
                while not self._stop_requested:
                    if ingress.failure is not None:
                        self._attention(
                            {"current_generation": self.current_generation},
                            ingress.failure,
                        )
                        self._record_status("UNKNOWN")
                        break
                    try:
                        first = ingress.events.get(timeout=max(interval, 0.05))
                    except queue.Empty:
                        persist_runtime_status()
                        continue
                    batch = [first]
                    while True:
                        try:
                            batch.append(ingress.events.get_nowait())
                        except queue.Empty:
                            break
                    for event in batch:
                        record_result(self._process_event(client, event))
                        self.current_cursor = max(
                            self.current_cursor, int(event.get("cursor", 0))
                        )
                    persist_runtime_status()
        except BaseException:
            if ingress_started:
                ingress.stop()
            raise
        finally:
            if ingress_started:
                ingress.stop()
        if ingress.failure is not None:
            self._attention(
                {"current_generation": self.current_generation},
                ingress.failure,
            )
            self._record_status("UNKNOWN")
        result = self.status()
        result["events_processed"] = events_processed
        result["event_results"] = list(result_history)
        self._write_status(result)
        return result
