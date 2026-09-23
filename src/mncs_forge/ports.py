"""Inward-facing application ports for records, execution, and local observations."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol

from .records import ForgeRecord, LedgerEntry

if TYPE_CHECKING:
    from .config import Provider


@dataclass(frozen=True)
class ExecutionResult:
    argv: list[str]
    returncode: int
    stdout: bytes
    stderr: bytes
    duration_seconds: float
    resource_envelope: Mapping[str, object] = field(default_factory=dict)
    resource_observations: Mapping[str, object] = field(default_factory=dict)


ExecutionTermination = Literal[
    "completed",
    "nonzero-exit",
    "timeout",
    "signal",
    "crash",
    "resource-limit",
    "output-limit",
    "policy-rejected",
    "cancelled",
    "internal-runner-error",
]


@dataclass(frozen=True)
class StreamObservation:
    """Bounded stream facts; unknown totals are represented explicitly."""

    total_bytes: int | None
    observed_bytes: int
    retained_bytes: int
    retained_sha256: str | None
    complete_sha256: str | None
    truncated: bool | None
    limit_hit: bool
    limit_bytes: int | None

    def to_dict(self) -> dict[str, object]:
        return {
            "total_bytes": self.total_bytes,
            "observed_bytes": self.observed_bytes,
            "retained_bytes": self.retained_bytes,
            "retained_sha256": self.retained_sha256,
            "complete_sha256": self.complete_sha256,
            "truncated": self.truncated,
            "limit_hit": self.limit_hit,
            "limit_bytes": self.limit_bytes,
        }


@dataclass(frozen=True)
class AggregateOutputObservation:
    total_bytes: int | None
    observed_bytes: int
    retained_bytes: int
    limit_hit: bool
    limit_bytes: int | None

    def to_dict(self) -> dict[str, object]:
        return {
            "total_bytes": self.total_bytes,
            "observed_bytes": self.observed_bytes,
            "retained_bytes": self.retained_bytes,
            "limit_hit": self.limit_hit,
            "limit_bytes": self.limit_bytes,
        }


RunnerCapability = Literal["enforced", "not-provided", "unknown"]
RunnerKind = str
RunnerExecutionScope = Literal["local", "remote", "unknown"]
RunnerShellExecution = Literal["disabled", "enabled", "unknown"]


@dataclass(frozen=True)
class RunnerCapabilities:
    """Facts a runner can establish without making an assurance claim."""

    runner_kind: RunnerKind
    runner_version: str
    os_family: str
    architecture: str
    execution_scope: RunnerExecutionScope
    shell_execution: RunnerShellExecution
    timeout_enforcement: RunnerCapability
    stdout_limit: RunnerCapability
    stderr_limit: RunnerCapability
    process_group_termination: RunnerCapability
    sandbox_isolation: RunnerCapability
    network_isolation: RunnerCapability
    filesystem_isolation: RunnerCapability
    memory_limit: RunnerCapability = "unknown"
    process_count_limit: RunnerCapability = "unknown"
    aggregate_concurrency_limit: RunnerCapability = "unknown"

    def to_dict(self) -> dict[str, str]:
        return {
            "runner_kind": self.runner_kind,
            "runner_version": self.runner_version,
            "os_family": self.os_family,
            "architecture": self.architecture,
            "execution_scope": self.execution_scope,
            "shell_execution": self.shell_execution,
            "timeout_enforcement": self.timeout_enforcement,
            "stdout_limit": self.stdout_limit,
            "stderr_limit": self.stderr_limit,
            "process_group_termination": self.process_group_termination,
            "sandbox_isolation": self.sandbox_isolation,
            "network_isolation": self.network_isolation,
            "filesystem_isolation": self.filesystem_isolation,
            "memory_limit": self.memory_limit,
            "process_count_limit": self.process_count_limit,
            "aggregate_concurrency_limit": self.aggregate_concurrency_limit,
        }


@dataclass(frozen=True)
class ExecutionObservation:
    """Raw runner facts, intentionally separate from semantic harness status."""

    argv: tuple[str, ...]
    command_identity: str
    cwd_identity: str
    environment_identity: str
    stdin_identity: str
    timeout_seconds: float
    stdout_limit: int
    stderr_limit: int
    started_at: str | None
    ended_at: str | None
    duration_seconds: float | None
    returncode: int | None
    signal: int | None
    termination_category: ExecutionTermination
    stdout: StreamObservation
    stderr: StreamObservation
    aggregate_output: AggregateOutputObservation
    capabilities: RunnerCapabilities
    runner_identity: str
    runner_version: str
    executable_identity: str | None
    runtime_identity: str | None
    error_code: str | None
    worker_identity: str | None
    host_identity: str | None
    image_identity: str | None
    placement_identity: str | None
    filesystem_policy: str
    network_policy: str
    same_operator: bool | None
    resource_envelope: Mapping[str, object] = field(default_factory=dict)
    resource_observations: Mapping[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {
            "argv": list(self.argv),
            "command_identity": self.command_identity,
            "cwd_identity": self.cwd_identity,
            "environment_identity": self.environment_identity,
            "stdin_identity": self.stdin_identity,
            "timeout_seconds": self.timeout_seconds,
            "stdout_limit": self.stdout_limit,
            "stderr_limit": self.stderr_limit,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "duration_seconds": self.duration_seconds,
            "returncode": self.returncode,
            "signal": self.signal,
            "termination_category": self.termination_category,
            "stdout": self.stdout.to_dict(),
            "stderr": self.stderr.to_dict(),
            "aggregate_output": self.aggregate_output.to_dict(),
            "capabilities": self.capabilities.to_dict(),
            "runner_identity": self.runner_identity,
            "runner_version": self.runner_version,
            "executable_identity": self.executable_identity,
            "runtime_identity": self.runtime_identity,
            "error_code": self.error_code,
            "worker_identity": self.worker_identity,
            "host_identity": self.host_identity,
            "image_identity": self.image_identity,
            "placement_identity": self.placement_identity,
            "filesystem_policy": self.filesystem_policy,
            "network_policy": self.network_policy,
            "same_operator": self.same_operator,
            "resource_envelope": dict(self.resource_envelope),
            "resource_observations": dict(self.resource_observations),
        }


@dataclass(frozen=True)
class ExecutionSession:
    """One runner invocation: retained output plus raw observation facts."""

    observation: ExecutionObservation
    stdout: bytes
    stderr: bytes
    result: ExecutionResult | None
    error_code: str | None
    error_message: str | None


class ExecutionObservationSink(Protocol):
    """Low-level callback used by bounded collectors without a second execution path."""

    def process_started(self) -> None: ...

    def feed(self, stream: str, data: bytes) -> None: ...

    def mark_limit(self, stream: str, limit_bytes: int) -> None: ...


class Runner(Protocol):
    """Execute one already-declared bounded command and inspect runner guarantees."""

    def execute(
        self,
        command: object,
        *,
        cwd: Path,
        timeout: float,
        output_cap: int,
        stderr_cap: int | None = None,
        environment: dict[str, str],
        stdin: bytes = b"",
    ) -> ExecutionResult: ...

    def observe(
        self,
        command: object,
        *,
        cwd: Path,
        timeout: float,
        output_cap: int,
        stderr_cap: int | None = None,
        environment: dict[str, str],
        stdin: bytes = b"",
    ) -> ExecutionObservation: ...

    def run(
        self,
        command: object,
        *,
        cwd: Path,
        timeout: float,
        output_cap: int,
        stderr_cap: int | None = None,
        environment: dict[str, str],
        stdin: bytes = b"",
    ) -> ExecutionSession: ...

    def inspect_capabilities(self) -> RunnerCapabilities: ...


# Retain the Task 5 port name for callers that imported the inward-facing protocol directly.
CommandExecutor = Runner


class RecordReader(Protocol):
    """Read verified typed history without exposing a concrete ledger implementation."""

    def records(self, kind: str | None = None) -> list[LedgerEntry]: ...

    def records_for(self, kinds: frozenset[str]) -> list[LedgerEntry]: ...

    def verify(self) -> dict[str, object]: ...


class RecordCommitter(Protocol):
    """Transactional record persistence needed by application services."""

    def commit(self, record_group: str, ledger_kind: str, record: ForgeRecord) -> LedgerEntry: ...

    def recover(self) -> dict[str, int]: ...

    def action_execution(
        self, action_id: str, *, timeout: float = 30
    ) -> AbstractContextManager[None]: ...


class ProjectObserver(Protocol):
    """Observe filesystem-bound identities and construct isolated local workspaces."""

    def authority_paths(self) -> list[Path]: ...

    def candidate_paths(self) -> list[Path]: ...

    def current_candidate_identity(self) -> str: ...

    def current_authority_identities(self) -> dict[str, str]: ...

    def content_identity(self, paths: list[Path]) -> str: ...

    def identity_map(self, paths: list[Path]) -> dict[str, str]: ...

    def selection_evidence_policy(self) -> tuple[str, tuple[str, ...], str | None]: ...

    def evidence_envelopes(
        self,
    ) -> tuple[dict[str, tuple[str, ...]], dict[str, str], dict[str, str]]: ...

    def current_freeze_bindings(
        self,
        candidate_identity: str | None = None,
        freeze: Mapping[str, object] | None = None,
    ) -> dict[str, str]: ...

    def provider_executable(self, provider: Provider) -> tuple[Path, str]: ...

    def provider_workspace(self, *, evaluator: bool = False) -> AbstractContextManager[str]: ...

    def validate_changed_files(self, changed_files: list[str]) -> dict[str, str]: ...

    def command_path(self, command: list[str]) -> str | None: ...


class VerifierCatalog(Protocol):
    def list_declared(self) -> dict[str, object]: ...


def record_by_id(records: RecordReader, kind: str, identity: str, key: str) -> ForgeRecord:
    for entry in reversed(records.records(kind)):
        if entry.payload.get(key) == identity:
            return entry.payload
    from .errors import ForgeError

    raise ForgeError("RECORD_NOT_FOUND", f"no {kind} record for {identity}")


def payloads(records: RecordReader, kind: str) -> Iterator[ForgeRecord]:
    return (entry.payload for entry in records.records(kind))
