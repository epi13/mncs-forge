"""Linux cgroup-v2 execution budgets for Forge's local Runner boundary.

The manager is deliberately an execution adapter: Forge chooses when work is
optional or required, while this module places one fixed argv in a transient
systemd service whose cgroup covers the complete process tree.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from filelock import FileLock, Timeout

from .errors import ForgeError

if TYPE_CHECKING:
    from .mncs_native import (
        NativeResourceAdmissionDecision,
        NativeResourceBudgetDecision,
        NativeResourceOutcomeDecision,
    )


class ResourceSemantics(Protocol):
    """Native Forge decision port used by this Linux realization adapter."""

    def resource_budget_select(
        self, observation: Mapping[str, object], settings: Mapping[str, object]
    ) -> NativeResourceBudgetDecision: ...

    def resource_budget_identity(self, decision: NativeResourceBudgetDecision) -> str: ...

    def resource_admission(
        self, observation: Mapping[str, object], policy: Mapping[str, object]
    ) -> NativeResourceAdmissionDecision: ...

    def resource_outcome(
        self, observation: Mapping[str, object]
    ) -> NativeResourceOutcomeDecision: ...

SLICE_NAME = "mncs-forge-verification.slice"
MAX_VERIFIER_ENVIRONMENT_BYTES = 1024 * 1024
_COUNTER_MAX = (1 << 63) - 1


def _host_memory() -> tuple[int | None, int | None]:
    total: int | None = None
    available: int | None = None
    try:
        for line in Path("/proc/meminfo").read_text(encoding="ascii").splitlines():
            key, _, rest = line.partition(":")
            number = int(rest.strip().split()[0]) * 1024
            if key == "MemTotal":
                total = number
            elif key == "MemAvailable":
                available = number
            if total is not None and available is not None:
                break
    except (OSError, ValueError, IndexError):
        pass
    if total is None:
        try:
            total = int(os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE"))
        except (OSError, ValueError):
            total = None
    return total, available


def _effective_memory_ceiling(total: int | None) -> int | None:
    """Respect every visible cgroup-v2 memory.max ancestor, including the host."""

    try:
        membership = Path("/proc/self/cgroup").read_text(encoding="ascii")
        relative = next(
            line.split("::", 1)[1].strip("/")
            for line in membership.splitlines()
            if line.startswith("0::")
        )
    except (OSError, StopIteration, IndexError):
        return total
    current = Path("/sys/fs/cgroup") / relative
    ceiling = total
    while True:
        try:
            raw = (current / "memory.max").read_text(encoding="ascii").strip()
            value = None if raw == "max" else int(raw)
        except (OSError, ValueError):
            value = None
        if value is not None and value > 0:
            ceiling = value if ceiling is None else min(ceiling, value)
        if current == Path("/sys/fs/cgroup") or current.parent == current:
            break
        current = current.parent
    return ceiling


def _cgroup_memory_headroom() -> int | None:
    """Read the minimum visible ancestor cgroup memory headroom."""

    try:
        membership = Path("/proc/self/cgroup").read_text(encoding="ascii")
        relative = next(
            line.split("::", 1)[1].strip("/")
            for line in membership.splitlines()
            if line.startswith("0::")
        )
    except (OSError, StopIteration, IndexError):
        return None
    current = Path("/sys/fs/cgroup") / relative
    headroom: int | None = None
    while True:
        try:
            raw = (current / "memory.max").read_text(encoding="ascii").strip()
            limit = None if raw == "max" else int(raw)
        except (OSError, ValueError):
            limit = None
        if limit is not None and limit > 0:
            try:
                used = int((current / "memory.current").read_text(encoding="ascii").strip())
            except (OSError, ValueError):
                return None
            value = max(0, limit - used)
            headroom = value if headroom is None else min(headroom, value)
        if current == Path("/sys/fs/cgroup") or current.parent == current:
            break
        current = current.parent
    return headroom


@dataclass(frozen=True, slots=True)
class ResourceBudget:
    memory_high_bytes: int
    memory_max_bytes: int
    memory_swap_max_bytes: int
    tasks_max: int
    concurrency_max: int
    runtime_max_seconds: float
    host_memory_total_bytes: int
    envelope_identity: str

    def to_dict(self) -> dict[str, object]:
        return {
            "identity": self.envelope_identity,
            "memory_high_bytes": self.memory_high_bytes,
            "memory_max_bytes": self.memory_max_bytes,
            "memory_swap_max_bytes": self.memory_swap_max_bytes,
            "tasks_max": self.tasks_max,
            "concurrency_max": self.concurrency_max,
            "runtime_max_seconds": self.runtime_max_seconds,
            "host_memory_total_bytes": self.host_memory_total_bytes,
            "mechanism": "systemd-user-service+cgroup-v2",
        }


@dataclass(frozen=True, slots=True)
class PreparedExecution:
    argv: tuple[str, ...]
    unit_name: str
    timeout_seconds: float
    started_monotonic: float
    environment_file_path: Path


class SystemdCgroupEnvelope:
    """Place and observe complete local verifier trees under a user cgroup-v2 slice."""

    def __init__(
        self,
        settings: Mapping[str, object],
        *,
        required: bool,
        control_runner: Callable[..., subprocess.CompletedProcess[bytes]] | None = None,
        host_memory_reader: Callable[[], tuple[int | None, int | None]] | None = None,
        cgroup_root: Path = Path("/sys/fs/cgroup"),
        systemd_run: str | None = None,
        systemctl: str | None = None,
        runtime_dir: Path | None = None,
        resource_semantics: ResourceSemantics | None = None,
        cgroup_memory_reader: Callable[[], int | None] | None = None,
        cgroup_memory_available_reader: Callable[[], int | None] | None = None,
    ) -> None:
        self._host_memory_reader = host_memory_reader or self._read_host_memory
        self._cgroup_memory_available_reader = (
            cgroup_memory_available_reader
            if cgroup_memory_available_reader is not None
            else _cgroup_memory_headroom
            if host_memory_reader is None
            else lambda: None
        )
        self._control = control_runner or subprocess.run
        self._cgroup_root = cgroup_root
        self.systemd_run = systemd_run if systemd_run is not None else shutil.which("systemd-run")
        self.systemctl = systemctl if systemctl is not None else shutil.which("systemctl")
        total, _host_available = self._host_memory_reader()
        cgroup_memory = (
            cgroup_memory_reader()
            if cgroup_memory_reader is not None
            else _effective_memory_ceiling(None)
            if host_memory_reader is None
            else None
        )
        if resource_semantics is None:
            raise ForgeError(
                "NATIVE_RESOURCE_POLICY_UNAVAILABLE",
                "MNCS resource policy is required to construct a cgroup envelope",
        )
        self._resource_semantics = resource_semantics
        self._budget_decision = resource_semantics.resource_budget_select(
            {
                "host_memory_total_bytes": total,
                "cgroup_memory_max_bytes": cgroup_memory,
            },
            settings,
        )
        if self._budget_decision.status == "InvalidPolicy":
            raise ForgeError(
                "RESOURCE_POLICY_INVALID",
                "the MNCS resource policy rejected the configured limits",
            )
        if self._budget_decision.status == "Selected":
            self.budget = ResourceBudget(
                memory_high_bytes=self._budget_decision.memory_high_bytes,
                memory_max_bytes=self._budget_decision.memory_max_bytes,
                memory_swap_max_bytes=self._budget_decision.memory_swap_max_bytes,
                tasks_max=self._budget_decision.tasks_max,
                concurrency_max=self._budget_decision.concurrency_max,
                runtime_max_seconds=self._budget_decision.runtime_max_seconds,
                host_memory_total_bytes=self._budget_decision.effective_host_memory_bytes,
                envelope_identity=resource_semantics.resource_budget_identity(
                    self._budget_decision
                ),
            )
        else:
            self.budget = None
        self.required = required
        self._runtime_directory = runtime_dir or self._runtime_dir()
        self._available = False
        self._limitation: str | None = None
        self._active: dict[str, dict[str, object]] = {}
        self._active_lock = threading.RLock()
        self._job_context: dict[str, object] = {}
        self._slice_cgroup_group: str | None = None
        self._resource_events = 0
        self._deferred = 0
        self._last_resource_event: dict[str, object] | None = None
        self._lock_path = self._runtime_directory / "mncs-forge-verification.lock"
        self._prepare()

    @staticmethod
    def _read_host_memory() -> tuple[int | None, int | None]:
        return _host_memory()

    @staticmethod
    def _runtime_dir() -> Path:
        value = os.environ.get("XDG_RUNTIME_DIR")
        if value:
            return Path(value)
        return Path(f"/run/user/{os.getuid()}")

    def _prepare(self) -> None:
        if self.budget is None:
            self._limitation = "host or cgroup memory capacity is unknown or below 256 MiB"
            return
        if not sys_platform_linux() or not self.systemd_run or not self.systemctl:
            self._limitation = "Linux systemd-run/systemctl user execution is unavailable"
            return
        if not (self._cgroup_root / "cgroup.controllers").is_file():
            self._limitation = "unified cgroup v2 is unavailable"
            return
        try:
            controllers = (
                (self._cgroup_root / "cgroup.controllers").read_text(encoding="ascii").split()
            )
        except OSError:
            controllers = []
        if not {"memory", "pids"}.issubset(controllers):
            self._limitation = "cgroup v2 memory and pids controllers are not available"
            return
        runtime_dir = self._runtime_directory
        if not runtime_dir.is_dir():
            self._limitation = "the systemd user runtime directory is unavailable"
            return
        try:
            runtime_stat = runtime_dir.stat()
        except OSError:
            self._limitation = "the systemd user runtime directory cannot be inspected"
            return
        if runtime_stat.st_uid != os.getuid() or runtime_stat.st_mode & 0o077:
            self._limitation = "the systemd user runtime directory is not private to this user"
            return
        budget = self.budget
        property_arguments = [
            f"MemoryHigh={budget.memory_high_bytes}",
            f"MemoryMax={budget.memory_max_bytes}",
            "MemorySwapMax=0",
            f"TasksMax={budget.tasks_max}",
            "MemoryAccounting=yes",
            "TasksAccounting=yes",
        ]
        try:
            result = self._control(
                [
                    self.systemctl,
                    "--user",
                    "set-property",
                    "--runtime",
                    SLICE_NAME,
                    *property_arguments,
                ],
                check=False,
                capture_output=True,
                timeout=3,
            )
        except (OSError, subprocess.TimeoutExpired):
            result = None
        if result is None or result.returncode != 0:
            detail = "" if result is None else result.stderr.decode("utf-8", "replace")[:256]
            self._limitation = "the user manager could not configure the shared cgroup slice" + (
                f": {detail}" if detail else ""
            )
            return
        unit_properties = self._unit_properties(SLICE_NAME)
        group = unit_properties.get("ControlGroup") or None
        verified = (
            self._verify_slice_limits(group, budget)
            if group is not None
            else self._verify_systemd_limits(unit_properties, budget)
        )
        verified = verified and all(
            unit_properties.get(name) == "yes" for name in ("MemoryAccounting", "TasksAccounting")
        )
        if not verified:
            self._limitation = "the configured cgroup-v2 slice limits could not be verified"
            return
        self._slice_cgroup_group = group
        self._available = True

    @staticmethod
    def _verify_systemd_limits(properties: Mapping[str, str], budget: ResourceBudget) -> bool:
        expected = {
            "MemoryHigh": budget.memory_high_bytes,
            "MemoryMax": budget.memory_max_bytes,
            "MemorySwapMax": budget.memory_swap_max_bytes,
            "TasksMax": budget.tasks_max,
        }
        try:
            return {name: int(properties[name]) for name in expected} == expected and all(
                properties.get(name) == "yes" for name in ("MemoryAccounting", "TasksAccounting")
            )
        except (KeyError, ValueError):
            return False

    def _verify_slice_limits(self, group: str, budget: ResourceBudget) -> bool:
        expected = {
            "memory.high": budget.memory_high_bytes,
            "memory.max": budget.memory_max_bytes,
            "memory.swap.max": budget.memory_swap_max_bytes,
            "pids.max": budget.tasks_max,
        }
        cgroup = self._cgroup_root / group.lstrip("/")
        try:
            observed = {
                filename: int((cgroup / filename).read_text(encoding="ascii").strip())
                for filename in expected
            }
        except (OSError, ValueError):
            return False
        return observed == expected

    @property
    def available(self) -> bool:
        return self._available

    @property
    def resource_limit_capability(self) -> str:
        return "enforced" if self._available else "unknown"

    def set_job_context(self, value: Mapping[str, object] | None) -> None:
        """Bind active runner observations to the generation currently supervised."""

        with self._active_lock:
            self._job_context = (
                {str(key): item for key, item in value.items()}
                if isinstance(value, Mapping)
                else {}
            )

    def launcher_environment(self, declared: Mapping[str, str]) -> dict[str, str]:
        """Add only the user-manager transport environment to the outer launcher.

        The verifier's own environment remains the exact allowlisted mapping
        passed after ``env -i`` in the service command.
        """

        result = dict(declared)
        result["XDG_RUNTIME_DIR"] = str(self._runtime_directory)
        for key in ("DBUS_SESSION_BUS_ADDRESS", "SYSTEMD_BUS_ADDRESS"):
            value = os.environ.get(key)
            if value is not None:
                result[key] = value
        return result

    def _admission(
        self,
        *,
        host_available_memory: int | None,
        cgroup_available_memory: int | None,
        active_tasks: int | None,
        execution_slot_available: bool,
        requested_runtime: float,
    ) -> NativeResourceAdmissionDecision:
        budget = self.budget
        return self._resource_semantics.resource_admission(
            {
                "containment_available": self._available,
                "has_budget": budget is not None,
                "host_available_memory_bytes": host_available_memory,
                "cgroup_available_memory_bytes": cgroup_available_memory,
                "active_tasks": active_tasks,
                "execution_slot_available": execution_slot_available,
                "requested_runtime_seconds": requested_runtime,
            },
            {
                "containment_required": self.required,
                "memory_max_bytes": budget.memory_max_bytes if budget else 0,
                "concurrency_max": budget.concurrency_max if budget else 1,
                "runtime_max_seconds": budget.runtime_max_seconds if budget else 0.0,
            },
        )

    def _raise_admission(
        self,
        decision: NativeResourceAdmissionDecision,
        *,
        host_available_memory: int | None,
        cgroup_available_memory: int | None,
        active_tasks: int | None,
    ) -> None:
        if decision.status in {"Admit", "Uncontained"}:
            return
        deferred = decision.deferred
        if deferred:
            self._deferred = min(_COUNTER_MAX, self._deferred + 1)
        codes = {
            "LowHostHeadroom": "RESOURCE_PRESSURE",
            "ActiveProcessTree": "RESOURCE_CONCURRENCY_LIMIT",
            "ExecutionSlotOccupied": "RESOURCE_CONCURRENCY_LIMIT",
            "ProcessCountUnknown": "RESOURCE_ENVELOPE_UNAVAILABLE",
            "ContainmentUnavailable": "RESOURCE_ENVELOPE_UNAVAILABLE",
            "BudgetUnavailable": "RESOURCE_ENVELOPE_UNAVAILABLE",
            "InvalidRuntime": "RESOURCE_ADMISSION_INVALID",
            "Defer": "RESOURCE_PRESSURE",
            "Unavailable": "RESOURCE_ENVELOPE_UNAVAILABLE",
            "InvalidInput": "RESOURCE_ADMISSION_INVALID",
        }
        code = codes.get(
            decision.reason,
            codes.get(decision.status, "RESOURCE_ENVELOPE_UNAVAILABLE"),
        )
        if decision.reason == "LowHostHeadroom":
            metric = "host-memory-available"
            bound = decision.required_headroom_bytes
            observed = {
                "host_available_memory_bytes": host_available_memory,
                "cgroup_available_memory_bytes": cgroup_available_memory,
            }
            message = (
                "host/cgroup available memory observations are below the "
                f"{decision.required_headroom_bytes}-byte admission headroom"
            )
        elif decision.reason == "ActiveProcessTree":
            metric, bound, observed = "concurrency", self.budget.concurrency_max if self.budget else 1, active_tasks
            message = "an earlier verification process tree is still active in the protected slice"
        elif decision.reason == "ExecutionSlotOccupied":
            metric, bound, observed = "concurrency", 1, 1
            message = "another Forge verification already occupies the single execution slot"
        elif decision.reason == "ProcessCountUnknown":
            metric, bound, observed = "process-count", self.budget.tasks_max if self.budget else None, None
            message = "the verification cgroup process count cannot be observed"
        elif decision.reason == "InvalidRuntime":
            metric, bound, observed = "wall-duration", None, None
            message = "the requested runtime is outside the native admission contract"
        else:
            metric, bound, observed = None, None, None
            message = self._limitation or "tree-wide cgroup resource enforcement is unavailable"
        raise ForgeError(
            code,
            message,
            details={
                "resource_evidence": {
                    "resource_envelope_identity": self.budget.envelope_identity if self.budget else None,
                    "resource_metric": metric,
                    "resource_bound": bound,
                    "resource_observed": observed,
                    "host_available_memory_bytes": host_available_memory,
                    "cgroup_available_memory_bytes": cgroup_available_memory,
                    "resource_exhausted": False,
                    "deferred": deferred,
                    "limitation": self._limitation if code == "RESOURCE_ENVELOPE_UNAVAILABLE" else None,
                    "native_admission_reason": decision.reason,
                    "cleanup_succeeded": True,
                }
            },
        )

    @contextmanager
    def execution_lock(self) -> Iterator[None]:
        _total, host_available = self._host_memory_reader()
        cgroup_available = self._cgroup_memory_available_reader()
        runtime = self.budget.runtime_max_seconds if self.budget else 1.0
        if not self._available:
            decision = self._admission(
                host_available_memory=host_available,
                cgroup_available_memory=cgroup_available,
                active_tasks=None,
                execution_slot_available=True,
                requested_runtime=runtime,
            )
            self._raise_admission(
                decision,
                host_available_memory=host_available,
                cgroup_available_memory=cgroup_available,
                active_tasks=None,
            )
            yield
            return
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with FileLock(str(self._lock_path), timeout=0):
                active_tasks = self._slice_value("pids.current")
                decision = self._admission(
                    host_available_memory=host_available,
                    cgroup_available_memory=cgroup_available,
                    active_tasks=active_tasks,
                    execution_slot_available=True,
                    requested_runtime=runtime,
                )
                self._raise_admission(
                    decision,
                    host_available_memory=host_available,
                    cgroup_available_memory=cgroup_available,
                    active_tasks=active_tasks,
                )
                yield
        except Timeout as error:
            decision = self._admission(
                host_available_memory=host_available,
                cgroup_available_memory=cgroup_available,
                active_tasks=None,
                execution_slot_available=False,
                requested_runtime=runtime,
            )
            try:
                self._raise_admission(
                    decision,
                    host_available_memory=host_available,
                    cgroup_available_memory=cgroup_available,
                    active_tasks=None,
                )
            except ForgeError as admission_error:
                raise admission_error from error
            raise ForgeError("RESOURCE_ADMISSION_UNKNOWN", "native admission allowed a locked slot") from error

    def prepare_execution(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        environment: Mapping[str, str],
        timeout: float,
    ) -> PreparedExecution | None:
        if not self._available or self.budget is None or self.systemd_run is None:
            _total, host_available = self._host_memory_reader()
            cgroup_available = self._cgroup_memory_available_reader()
            decision = self._admission(
                host_available_memory=host_available,
                cgroup_available_memory=cgroup_available,
                active_tasks=None,
                execution_slot_available=True,
                requested_runtime=float(timeout),
            )
            self._raise_admission(
                decision,
                host_available_memory=host_available,
                cgroup_available_memory=cgroup_available,
                active_tasks=None,
            )
            return None
        _total, host_available = self._host_memory_reader()
        cgroup_available = self._cgroup_memory_available_reader()
        decision = self._admission(
            host_available_memory=host_available,
            cgroup_available_memory=cgroup_available,
            active_tasks=0,
            execution_slot_available=True,
            requested_runtime=float(timeout),
        )
        self._raise_admission(
            decision,
            host_available_memory=host_available,
            cgroup_available_memory=cgroup_available,
            active_tasks=0,
        )
        unit = f"mncs-forge-job-{uuid.uuid4().hex[:16]}.service"
        started = time.monotonic()
        runtime = decision.runtime_seconds
        budget = self.budget
        environment_path = self._runtime_directory / f"mncs-forge-env-{uuid.uuid4().hex}.json"
        self._write_environment_file(environment_path, environment)
        helper = Path(__file__).with_name("resource_process.py").resolve()
        command = [
            self.systemd_run,
            "--user",
            "--wait",
            "--pipe",
            "--quiet",
            f"--unit={unit}",
            f"--slice={SLICE_NAME}",
            f"--working-directory={cwd.resolve()}",
            f"--property=MemoryHigh={budget.memory_high_bytes}",
            f"--property=MemoryMax={budget.memory_max_bytes}",
            "--property=MemorySwapMax=0",
            f"--property=TasksMax={budget.tasks_max}",
            "--property=MemoryAccounting=yes",
            "--property=TasksAccounting=yes",
            "--property=Delegate=yes",
            f"--property=RuntimeMaxSec={runtime:g}s",
            "--property=TimeoutStopSec=1s",
            "--property=KillMode=control-group",
            "--",
            sys.executable,
            str(helper),
            str(environment_path),
            *argv,
        ]
        with self._active_lock:
            self._active[unit] = {
                "unit": unit,
                "command": Path(argv[0]).name[:128],
                "started_monotonic": started,
                "timeout_seconds": runtime,
                "envelope_identity": budget.envelope_identity,
                "memory_max_bytes": budget.memory_max_bytes,
                "tasks_max": budget.tasks_max,
                "cgroup_group": None,
                "next_cgroup_probe_monotonic": 0.0,
                "resource_observations": {},
                "generation": None,
                "source_identity": None,
                **self._job_context,
            }
        return PreparedExecution(tuple(command), unit, runtime, started, environment_path)

    @staticmethod
    def _write_environment_file(path: Path, environment: Mapping[str, str]) -> None:
        if any(
            not isinstance(key, str)
            or not isinstance(value, str)
            or not key
            or "=" in key
            or "\x00" in key
            or "\x00" in value
            for key, value in environment.items()
        ):
            raise ForgeError(
                "RESOURCE_ENVIRONMENT_INVALID",
                "verifier environment contains an invalid key or value",
                details={
                    "resource_evidence": {
                        "resource_exhausted": False,
                        "deferred": True,
                        "cleanup_succeeded": True,
                    }
                },
            )
        encoded = json.dumps(dict(environment), sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
        if len(encoded) > MAX_VERIFIER_ENVIRONMENT_BYTES:
            raise ForgeError(
                "RESOURCE_ENVIRONMENT_LIMIT",
                f"verifier environment exceeds the {MAX_VERIFIER_ENVIRONMENT_BYTES}-byte bound",
                details={
                    "resource_evidence": {
                        "resource_metric": "environment-bytes",
                        "resource_bound": MAX_VERIFIER_ENVIRONMENT_BYTES,
                        "resource_observed": len(encoded),
                        "resource_exhausted": False,
                        "deferred": True,
                        "cleanup_succeeded": True,
                    }
                },
            )
        try:
            descriptor = os.open(
                path,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0),
                0o600,
            )
        except OSError as error:
            raise ForgeError(
                "RESOURCE_ENVIRONMENT_UNAVAILABLE",
                f"cannot create the private verifier environment: {error}",
                details={
                    "resource_evidence": {
                        "resource_exhausted": False,
                        "deferred": True,
                        "cleanup_succeeded": True,
                    }
                },
            ) from error
        try:
            remaining = memoryview(encoded)
            while remaining:
                written = os.write(descriptor, remaining)
                if written <= 0:
                    raise OSError("short verifier environment write")
                remaining = remaining[written:]
        except OSError as error:
            with suppress(OSError):
                path.unlink(missing_ok=True)
            raise ForgeError(
                "RESOURCE_ENVIRONMENT_UNAVAILABLE",
                f"cannot stage the private verifier environment: {error}",
                details={
                    "resource_evidence": {
                        "resource_exhausted": False,
                        "deferred": True,
                        "cleanup_succeeded": False,
                    }
                },
            ) from error
        finally:
            os.close(descriptor)

    def observe_execution(self, prepared: PreparedExecution) -> None:
        """Sample live cgroup counters before systemd removes the transient unit."""

        with self._active_lock:
            active = self._active.get(prepared.unit_name)
            if active is None:
                return
            group = active.get("cgroup_group")
            now = time.monotonic()
            next_probe = active.get("next_cgroup_probe_monotonic", 0.0)
            probe_due = not isinstance(next_probe, (int, float)) or now >= float(next_probe)
            if not probe_due:
                return
            interval = 0.01 if not group else 0.05
            active["next_cgroup_probe_monotonic"] = now + interval
        if not group:
            properties = self._unit_properties(prepared.unit_name)
            group = properties.get("ControlGroup") or None
            property_observations = self._systemd_observations(properties)
            if property_observations:
                with self._active_lock:
                    active = self._active.get(prepared.unit_name)
                    if active is not None:
                        active["resource_observations"] = self._merge_observations(
                            active.get("resource_observations", {}), property_observations
                        )
            if group:
                with self._active_lock:
                    active = self._active.get(prepared.unit_name)
                    if active is not None:
                        active["cgroup_group"] = group
                if self._slice_cgroup_group is None:
                    self._slice_cgroup_group = (
                        self._unit_properties(SLICE_NAME).get("ControlGroup") or None
                    )
        if not group:
            return
        observed = self._cgroup_values(str(group))
        if not observed:
            return
        with self._active_lock:
            active = self._active.get(prepared.unit_name)
            if active is None:
                return
            active["resource_observations"] = self._merge_observations(
                active.get("resource_observations", {}), observed
            )

    def cancel_execution(self, prepared: PreparedExecution) -> bool:
        """Realize an already-authorized cancellation request for the owned unit."""

        if not self.systemctl:
            return False
        with self._active_lock:
            if prepared.unit_name not in self._active:
                return False
        signalled = False
        for signal_name in ("SIGTERM", "SIGKILL"):
            try:
                result = self._control(
                    [
                        self.systemctl,
                        "--user",
                        "kill",
                        "--kill-whom=all",
                        f"--signal={signal_name}",
                        prepared.unit_name,
                    ],
                    check=False,
                    capture_output=True,
                    timeout=0.5,
                )
            except (OSError, subprocess.TimeoutExpired):
                continue
            signalled = result.returncode == 0 or signalled
        return signalled

    @staticmethod
    def _systemd_observations(properties: Mapping[str, str]) -> dict[str, int]:
        values: dict[str, int] = {}
        for source, target in (
            ("MemoryPeak", "cgroup_memory_peak_bytes"),
            ("TasksCurrent", "process_count_current"),
        ):
            try:
                value = int(properties[source])
            except (KeyError, ValueError):
                continue
            values[target] = value
        with suppress(KeyError, ValueError):
            values["cpu_time_microseconds"] = int(properties["CPUUsageNSec"]) // 1_000
        return values

    @staticmethod
    def _merge_observations(previous: object, incoming: Mapping[str, int]) -> dict[str, int]:
        retained = dict(previous) if isinstance(previous, Mapping) else {}
        for key, value in incoming.items():
            if key.endswith(("_peak", "_events")) or key in {
                "cgroup_memory_peak_bytes",
                "cpu_time_nanoseconds",
                "process_count_peak",
                "memory_max_events",
                "memory_oom_events",
                "memory_oom_kill_events",
                "memory_oom_group_kill_events",
                "memory_high_events",
                "process_limit_events",
            }:
                retained[key] = max(int(retained.get(key, 0) or 0), value)
            else:
                retained[key] = value
        return retained

    def _unit_properties(self, unit: str) -> dict[str, str]:
        if not self.systemctl:
            return {}
        try:
            result = self._control(
                [
                    self.systemctl,
                    "--user",
                    "show",
                    unit,
                    "--property=ControlGroup",
                    "--property=Result",
                    "--property=ActiveState",
                    "--property=MemoryPeak",
                    "--property=CPUUsageNSec",
                    "--property=SubState",
                    "--property=ExecMainCode",
                    "--property=ExecMainStatus",
                    "--property=TasksCurrent",
                    "--property=MemoryHigh",
                    "--property=MemoryMax",
                    "--property=MemorySwapMax",
                    "--property=TasksMax",
                    "--property=LoadState",
                    "--property=MemoryAccounting",
                    "--property=TasksAccounting",
                    "--property=CPUAccounting",
                ],
                check=False,
                capture_output=True,
                timeout=1,
            )
        except (OSError, subprocess.TimeoutExpired):
            return {}
        values: dict[str, str] = {}
        if result.returncode == 0:
            for line in result.stdout.decode("utf-8", "replace").splitlines():
                key, separator, value = line.partition("=")
                if separator:
                    values[key] = value
        return values

    def _cgroup_values(self, group: str) -> dict[str, int]:
        path = self._cgroup_root / group.lstrip("/")
        values: dict[str, int] = {}
        for filename, key in (
            ("memory.current", "memory_current_bytes"),
            ("memory.peak", "cgroup_memory_peak_bytes"),
            ("pids.current", "process_count_current"),
            ("pids.peak", "process_count_peak"),
        ):
            with suppress(OSError, ValueError):
                values[key] = int((path / filename).read_text(encoding="ascii").strip())
        try:
            events = dict(
                (key, int(value))
                for key, value in (
                    line.split()
                    for line in (path / "memory.events").read_text(encoding="ascii").splitlines()
                )
            )
            values["memory_oom_events"] = events.get("oom", 0)
            values["memory_oom_kill_events"] = events.get("oom_kill", 0)
            values["memory_oom_group_kill_events"] = events.get("oom_group_kill", 0)
            values["memory_high_events"] = events.get("high", 0)
            values["memory_max_events"] = events.get("max", 0)
        except (OSError, ValueError):
            pass
        try:
            events = dict(
                (key, int(value))
                for key, value in (
                    line.split()
                    for line in (path / "pids.events").read_text(encoding="ascii").splitlines()
                )
            )
            values["process_limit_events"] = events.get("max", 0)
        except (OSError, ValueError):
            pass
        try:
            cpu = dict(
                (key, int(value))
                for key, value in (
                    line.split()
                    for line in (path / "cpu.stat").read_text(encoding="ascii").splitlines()
                )
            )
            values["cpu_time_microseconds"] = cpu.get("usage_usec", 0)
        except (OSError, ValueError):
            pass
        return values

    def finish_execution(
        self,
        prepared: PreparedExecution,
        *,
        timed_out: bool = False,
        failure: ForgeError | None = None,
        wrapper_returncode: int | None = None,
    ) -> dict[str, object]:
        properties = self._unit_properties(prepared.unit_name)
        group = properties.get("ControlGroup", "")
        live_values = self._cgroup_values(group) if group else {}
        systemd_values = self._systemd_observations(properties)
        with self._active_lock:
            active = self._active.get(prepared.unit_name, {})
            sampled_values = active.get("resource_observations", {})
        values = self._merge_observations(sampled_values, systemd_values)
        values = self._merge_observations(values, live_values)
        result = properties.get("Result", "unknown")
        command_returncode: int | None = None
        try:
            main_code = int(properties.get("ExecMainCode", "-1"))
            main_status = int(properties["ExecMainStatus"])
            if main_code == 1:  # CLD_EXITED
                command_returncode = main_status
            elif main_code in {2, 3}:  # CLD_KILLED / CLD_DUMPED
                command_returncode = -main_status
        except (KeyError, ValueError):
            pass
        output_evidence: Mapping[str, object] = {}
        if failure is not None:
            candidate_evidence = failure.details.get("resource_evidence")
            if isinstance(candidate_evidence, Mapping):
                output_evidence = candidate_evidence
        output_limited = failure is not None and failure.code == "OUTPUT_LIMIT"
        cancellation_observation = (
            failure.details.get("execution_cancellation")
            if failure is not None
            else None
        )
        cancellation_facts = (
            cancellation_observation
            if isinstance(cancellation_observation, Mapping)
            else {}
        )
        with self._active_lock:
            active = self._active.pop(prepared.unit_name, {})
        tree_cleanup_succeeded = self._cleanup_unit(prepared.unit_name, group)
        try:
            prepared.environment_file_path.unlink(missing_ok=True)
            environment_file_cleanup_succeeded = True
        except OSError:
            environment_file_cleanup_succeeded = False
        cleanup_succeeded = tree_cleanup_succeeded and environment_file_cleanup_succeeded
        observed_memory = values.get("cgroup_memory_peak_bytes")
        observed_tasks = values.get("process_count_peak")
        outcome = self._resource_semantics.resource_outcome(
            {
                "exit_status": command_returncode,
                "wrapper_returncode": wrapper_returncode,
                "systemd_result": result,
                "timed_out": timed_out,
                "output_limited": output_limited,
                "memory_high_events": values.get("memory_high_events", 0),
                "memory_max_events": values.get("memory_max_events", 0),
                "memory_oom_events": values.get("memory_oom_events", 0),
                "memory_oom_kill_events": values.get("memory_oom_kill_events", 0),
                "memory_oom_group_kill_events": values.get("memory_oom_group_kill_events", 0),
                "process_limit_events": values.get("process_limit_events", 0),
                "cleanup_known": True,
                "cleanup_succeeded": cleanup_succeeded,
                "cancellation_requested": cancellation_facts.get(
                    "cancellation_requested", False
                )
                is True,
                "superseded": cancellation_facts.get("superseded", False) is True,
            }
        )
        exhausted = outcome.resource_exhausted
        if exhausted:
            self._resource_events = min((1 << 63) - 1, self._resource_events + 1)
        observed: object
        bound: object
        if outcome.metric == "Memory" and exhausted:
            metric = "host-memory-peak"
            observed = observed_memory
            bound = self.budget.memory_max_bytes if self.budget else None
        elif outcome.metric == "Memory":
            metric = "host-memory-high-events"
            observed = values.get("memory_high_events")
            bound = self.budget.memory_high_bytes if self.budget else None
        elif outcome.metric == "ProcessCount":
            metric = "process-count"
            observed = observed_tasks
            bound = self.budget.tasks_max if self.budget else None
        elif outcome.metric == "WallTime":
            metric = "wall-duration"
            observed = round(time.monotonic() - prepared.started_monotonic, 6)
            bound = prepared.timeout_seconds
        elif outcome.metric == "Output":
            metric = "output-bytes"
            observed = output_evidence.get("resource_observed")
            bound = output_evidence.get("resource_bound")
        else:
            metric, observed, bound = None, None, None
        evidence: dict[str, object] = {
            "resource_envelope_identity": self.budget.envelope_identity if self.budget else None,
            "unit_identity": prepared.unit_name,
            "resource_exhausted": exhausted,
            "resource_outcome": outcome.status,
            "native_execution_returncode_available": outcome.has_execution_exit_status,
            "native_execution_returncode": outcome.execution_exit_status,
            "cancellation_requested": outcome.cancellation_requested,
            "superseded": outcome.superseded,
            "execution_cancellation": dict(cancellation_facts),
            "verification_deferred": outcome.deferred,
            "resource_metric": metric,
            "resource_bound": bound,
            "resource_observed": observed,
            "cleanup_succeeded": cleanup_succeeded,
            "tree_cleanup_succeeded": tree_cleanup_succeeded,
            "environment_file_cleanup_succeeded": environment_file_cleanup_succeeded,
            "systemd_result": result,
            "systemd_unit_load_state": properties.get("LoadState"),
            "systemd_wrapper_returncode": wrapper_returncode,
            "command_returncode": command_returncode,
            "wrapper_returncode_mismatch": (
                wrapper_returncode != command_returncode
                if wrapper_returncode is not None and command_returncode is not None
                else None
            ),
            "resource_observations": values,
            "resource_limits": self.budget.to_dict() if self.budget else {},
            "job_context": {
                key: value
                for key, value in active.items()
                if key
                not in {
                    "unit",
                    "command",
                    "started_monotonic",
                    "timeout_seconds",
                    "envelope_identity",
                    "memory_max_bytes",
                    "tasks_max",
                }
            },
        }
        self._last_resource_event = {
            key: evidence.get(key)
            for key in (
                "resource_envelope_identity",
                "unit_identity",
                "resource_exhausted",
                "resource_outcome",
                "resource_metric",
                "resource_bound",
                "resource_observed",
                "cleanup_succeeded",
                "systemd_result",
                "job_context",
            )
        }
        return evidence

    def _cleanup_unit(self, unit: str, group: str) -> bool:
        if not self.systemctl:
            return False
        for command in (
            [self.systemctl, "--user", "kill", "--kill-whom=all", "--signal=SIGKILL", unit],
            [self.systemctl, "--user", "stop", unit],
        ):
            with suppress(OSError, subprocess.TimeoutExpired):
                # A completed transient service may already be inactive. The
                # post-stop cgroup/process observation below is authoritative.
                self._control(command, check=False, capture_output=True, timeout=2)
        properties = self._unit_properties(unit)
        inactive = properties.get("ActiveState") in {"inactive", "failed"}
        dead = properties.get("SubState") in {"dead", "failed"}
        not_found = properties.get("LoadState") == "not-found"
        observed_group = properties.get("ControlGroup", "")
        cgroup_group = observed_group or group
        cgroup_path = self._cgroup_root / cgroup_group.lstrip("/") if cgroup_group else None
        current: int | None = None
        if cgroup_path is not None and cgroup_path.exists():
            current = self._cgroup_values(cgroup_group).get("process_count_current")
        no_live_group = not observed_group and cgroup_path is None
        empty_live_group = cgroup_path is not None and (not cgroup_path.exists() or current == 0)
        if not inactive or not dead or not (no_live_group or empty_live_group):
            return False
        if not_found:
            return True
        try:
            result = self._control(
                [self.systemctl, "--user", "reset-failed", unit],
                check=False,
                capture_output=True,
                timeout=2,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return result.returncode == 0

    def status(self) -> dict[str, object]:
        total, host_available = self._host_memory_reader()
        cgroup_available = self._cgroup_memory_available_reader()
        process = _process_memory()
        budget = self.budget
        with self._active_lock:
            needs_group = self._available and self._slice_cgroup_group is None
        if needs_group:
            group = self._unit_properties(SLICE_NAME).get("ControlGroup") or None
            with self._active_lock:
                self._slice_cgroup_group = self._slice_cgroup_group or group
        with self._active_lock:
            active_values = list(self._active.values())[:1]
            active_count = len(self._active)
            slice_group = self._slice_cgroup_group
        active_jobs: list[dict[str, object]] = []
        for item in active_values:
            started = item.get("started_monotonic")
            active_job = {key: value for key, value in item.items() if key != "started_monotonic"}
            if isinstance(started, (int, float)) and not isinstance(started, bool):
                active_job["elapsed_seconds"] = round(time.monotonic() - float(started), 3)
            active_jobs.append(active_job)
        return {
            "state": "protected" if self._available else "unavailable",
            "mechanism": "systemd-user-service+cgroup-v2" if self._available else None,
            "supervisor_process_rss_bytes": process.get("rss_bytes"),
            "supervisor_process_rss_peak_bytes": process.get("rss_peak_bytes"),
            "host_memory_available_bytes": host_available,
            "cgroup_memory_available_bytes": cgroup_available,
            "host_memory_total_bytes": total,
            "aggregate_memory_current_bytes": self._slice_value("memory.current", slice_group),
            "aggregate_memory_peak_bytes": self._slice_value("memory.peak", slice_group),
            "aggregate_process_count": self._slice_value("pids.current", slice_group),
            "aggregate_process_count_peak": self._slice_value("pids.peak", slice_group),
            "configured_memory_high_bytes": budget.memory_high_bytes if budget else None,
            "configured_memory_max_bytes": budget.memory_max_bytes if budget else None,
            "configured_memory_swap_max_bytes": budget.memory_swap_max_bytes if budget else None,
            "configured_process_count_max": budget.tasks_max if budget else None,
            "configured_concurrency_max": budget.concurrency_max if budget else 1,
            "configured_runtime_max_seconds": budget.runtime_max_seconds if budget else None,
            "resource_envelope_identity": budget.envelope_identity if budget else None,
            "active_jobs": active_jobs,
            "active_job_capacity": 1,
            "concurrency": min(active_count, 1),
            "resource_exhaustion_events": self._resource_events,
            "last_resource_event": dict(self._last_resource_event)
            if self._last_resource_event
            else None,
            "deferred_jobs": self._deferred,
            "limitation": self._limitation,
            "limitations": [
                "cgroup-v2/systemd limits are host-kernel enforced, not hardware attestation",
                "host RSS and host MemAvailable are instantaneous observations",
                "Forge startup does not trigger adversarial memory or PID exhaustion probes",
            ],
        }

    def _slice_value(self, filename: str, group: str | None = None) -> int | None:
        selected_group = group if group is not None else self._slice_cgroup_group
        if not selected_group:
            return None
        try:
            return int(
                (self._cgroup_root / selected_group.lstrip("/") / filename)
                .read_text(encoding="ascii")
                .strip()
            )
        except (OSError, ValueError):
            return None


def _process_memory() -> dict[str, int]:
    values: dict[str, int] = {}
    try:
        for line in Path("/proc/self/status").read_text(encoding="ascii").splitlines():
            key, _, rest = line.partition(":")
            if key not in {"VmRSS", "VmHWM"}:
                continue
            number = int(rest.strip().split()[0]) * 1024
            values["rss_bytes" if key == "VmRSS" else "rss_peak_bytes"] = number
    except (OSError, ValueError, IndexError):
        pass
    return values


def sys_platform_linux() -> bool:
    return platform.system().lower() == "linux"
