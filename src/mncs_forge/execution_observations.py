"""Construction of bounded, non-authoritative local runner observations."""

from __future__ import annotations

import hashlib
import platform
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import rfc8785

from .errors import ForgeError
from .ports import (
    AggregateOutputObservation,
    ExecutionObservation,
    ExecutionResult,
    ExecutionSession,
    ExecutionTermination,
    RunnerCapabilities,
    StreamObservation,
)


def canonical_sha256(value: object) -> str:
    """Return the MNCS-compatible RFC 8785 SHA-256 identity for JSON data.

    This is the cross-implementation track (see ``serialization``): runner and
    host identities stay comparable with language-owned JCS digests. Forge
    persisted bytes and Forge-local content identities use
    :func:`mncs_forge.serialization.canonical_bytes` instead.
    """

    return hashlib.sha256(rfc8785.dumps(cast(Any, value))).hexdigest()


def bytes_sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _timestamp() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


class _StreamCapture:
    def __init__(self, limit_bytes: int) -> None:
        self.limit_bytes = limit_bytes
        self.observed_bytes = 0
        self._retained = bytearray()
        self._complete_digest = hashlib.sha256()
        self.limit_hit = False
        self._complete_possible = True

    def feed(self, data: bytes) -> None:
        self.observed_bytes += len(data)
        if self._complete_possible:
            self._complete_digest.update(data)
        remaining = max(0, self.limit_bytes - len(self._retained))
        retained = data[:remaining]
        if retained:
            self._retained.extend(retained)
        if self.observed_bytes > self.limit_bytes:
            self.limit_hit = True
            self._complete_possible = False

    def mark_limit(self) -> None:
        self.limit_hit = True
        self._complete_possible = False

    def retained(self) -> bytes:
        return bytes(self._retained)

    def snapshot(self, *, complete: bool) -> StreamObservation:
        retained_bytes = len(self._retained)
        retained_sha256 = bytes_sha256(bytes(self._retained)) if retained_bytes else None
        if complete and self._complete_possible:
            total_bytes: int | None = self.observed_bytes
            complete_sha256 = self._complete_digest.hexdigest() if self.observed_bytes else None
            truncated: bool | None = False
        else:
            total_bytes = None
            complete_sha256 = None
            truncated = True if self.limit_hit else None
        return StreamObservation(
            total_bytes=total_bytes,
            observed_bytes=self.observed_bytes,
            retained_bytes=retained_bytes,
            retained_sha256=retained_sha256,
            complete_sha256=complete_sha256,
            truncated=truncated,
            limit_hit=self.limit_hit,
            limit_bytes=self.limit_bytes,
        )


class ExecutionObservationBuilder:
    """Collect observations alongside the existing bounded execution path."""

    def __init__(
        self,
        *,
        argv: list[str],
        cwd: Path,
        timeout: float,
        stdout_limit: int,
        stderr_limit: int,
        environment: dict[str, str],
        stdin: bytes,
        capabilities: RunnerCapabilities,
        runner_identity: str,
        runner_version: str,
        executable_identity: str | None,
        worker_identity: str | None = None,
        host_identity: str | None = None,
        image_identity: str | None = None,
        placement_identity: str | None = None,
        filesystem_policy: str = "unrestricted-process-workspace",
        network_policy: str = "ambient-process-network",
        same_operator: bool | None = True,
    ) -> None:
        self.argv = tuple(argv)
        self.command_identity = canonical_sha256({"argv": list(argv)})
        self.cwd_identity = canonical_sha256({"cwd": str(cwd.resolve())})
        self.environment_identity = canonical_sha256(
            {"environment": {key: environment[key] for key in sorted(environment)}}
        )
        self.stdin_identity = bytes_sha256(stdin)
        self.timeout = timeout
        self.stdout_limit = stdout_limit
        self.stderr_limit = stderr_limit
        self.capabilities = capabilities
        self.runner_identity = runner_identity
        self.runner_version = runner_version
        self.executable_identity = executable_identity
        self.runtime_identity = f"runtime.python-{platform.python_version()}"
        self.worker_identity = worker_identity
        self.host_identity = host_identity
        self.image_identity = image_identity
        self.placement_identity = placement_identity
        self.filesystem_policy = filesystem_policy
        self.network_policy = network_policy
        self.same_operator = same_operator
        self._stdout = _StreamCapture(stdout_limit)
        self._stderr = _StreamCapture(stderr_limit)
        self._created_monotonic = time.monotonic()
        self._started_at: str | None = None
        self._finished = False
        self._observation: ExecutionObservation | None = None

    def feed(self, stream: str, data: bytes) -> None:
        if stream == "stdout":
            self._stdout.feed(data)
        elif stream == "stderr":
            self._stderr.feed(data)
        else:  # pragma: no cover - only bounded collector literals call this
            raise ValueError(f"unknown execution stream: {stream}")

    def mark_limit(self, stream: str, limit_bytes: int) -> None:
        if stream == "stdout":
            self._stdout.mark_limit()
        elif stream == "stderr":
            self._stderr.mark_limit()
        else:  # pragma: no cover - only bounded collector literals call this
            raise ValueError(f"unknown execution stream: {stream}")
        if limit_bytes <= 0:  # pragma: no cover - validated before execution
            raise ValueError("stream limit must be positive")

    def process_started(self) -> None:
        self._started_at = _timestamp()

    @staticmethod
    def _termination(returncode: int) -> tuple[ExecutionTermination, int | None]:
        if returncode == 0:
            return "completed", None
        if returncode < 0:
            return "signal", -returncode
        return "nonzero-exit", None

    @staticmethod
    def _failure_termination(code: str) -> ExecutionTermination:
        return cast(
            ExecutionTermination,
            {
                "TIMEOUT": "timeout",
                "OUTPUT_LIMIT": "output-limit",
            }.get(code, "internal-runner-error"),
        )

    def _aggregate(
        self, stdout: StreamObservation, stderr: StreamObservation, *, complete: bool
    ) -> AggregateOutputObservation:
        total = (
            stdout.total_bytes + stderr.total_bytes
            if complete and stdout.total_bytes is not None and stderr.total_bytes is not None
            else None
        )
        return AggregateOutputObservation(
            total_bytes=total,
            observed_bytes=stdout.observed_bytes + stderr.observed_bytes,
            retained_bytes=stdout.retained_bytes + stderr.retained_bytes,
            limit_hit=stdout.limit_hit or stderr.limit_hit,
            limit_bytes=None,
        )

    def completed(self, result: ExecutionResult) -> None:
        if self._finished:
            return
        stdout = self._stdout.snapshot(complete=True)
        stderr = self._stderr.snapshot(complete=True)
        termination, signal = self._termination(result.returncode)
        self._observation = ExecutionObservation(
            argv=self.argv,
            command_identity=self.command_identity,
            cwd_identity=self.cwd_identity,
            environment_identity=self.environment_identity,
            stdin_identity=self.stdin_identity,
            timeout_seconds=self.timeout,
            stdout_limit=self.stdout_limit,
            stderr_limit=self.stderr_limit,
            started_at=self._started_at,
            ended_at=_timestamp(),
            duration_seconds=result.duration_seconds,
            returncode=result.returncode,
            signal=signal,
            termination_category=termination,
            stdout=stdout,
            stderr=stderr,
            aggregate_output=self._aggregate(stdout, stderr, complete=True),
            capabilities=self.capabilities,
            runner_identity=self.runner_identity,
            runner_version=self.runner_version,
            executable_identity=self.executable_identity,
            runtime_identity=self.runtime_identity,
            error_code=None,
            worker_identity=self.worker_identity,
            host_identity=self.host_identity,
            image_identity=self.image_identity,
            placement_identity=self.placement_identity,
            filesystem_policy=self.filesystem_policy,
            network_policy=self.network_policy,
            same_operator=self.same_operator,
        )
        self._finished = True

    def failed(self, error: ForgeError) -> None:
        if self._finished:
            return
        stdout = self._stdout.snapshot(complete=False)
        stderr = self._stderr.snapshot(complete=False)
        self._observation = ExecutionObservation(
            argv=self.argv,
            command_identity=self.command_identity,
            cwd_identity=self.cwd_identity,
            environment_identity=self.environment_identity,
            stdin_identity=self.stdin_identity,
            timeout_seconds=self.timeout,
            stdout_limit=self.stdout_limit,
            stderr_limit=self.stderr_limit,
            started_at=self._started_at,
            ended_at=_timestamp(),
            duration_seconds=round(time.monotonic() - self._created_monotonic, 6),
            returncode=None,
            signal=None,
            termination_category=self._failure_termination(error.code),
            stdout=stdout,
            stderr=stderr,
            aggregate_output=self._aggregate(stdout, stderr, complete=False),
            capabilities=self.capabilities,
            runner_identity=self.runner_identity,
            runner_version=self.runner_version,
            executable_identity=self.executable_identity,
            runtime_identity=self.runtime_identity,
            error_code=error.code,
            worker_identity=self.worker_identity,
            host_identity=self.host_identity,
            image_identity=self.image_identity,
            placement_identity=self.placement_identity,
            filesystem_policy=self.filesystem_policy,
            network_policy=self.network_policy,
            same_operator=self.same_operator,
        )
        self._finished = True

    def build(self) -> ExecutionObservation:
        if self._observation is None:
            raise RuntimeError("execution observation is not complete")
        return self._observation

    def session(self, result: ExecutionResult | None, error: ForgeError | None) -> ExecutionSession:
        stdout = result.stdout if result is not None else self._stdout.retained()
        stderr = result.stderr if result is not None else self._stderr.retained()
        return ExecutionSession(
            observation=self.build(),
            stdout=stdout,
            stderr=stderr,
            result=result,
            error_code=None if error is None else error.code,
            error_message=None if error is None else str(error),
        )
