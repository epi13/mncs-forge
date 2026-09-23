"""Rootless Podman sandbox-capable runner reporting only enforced properties.

The adapter launches declared argument arrays inside a rootless container with
networking disabled, a read-only root filesystem, a read-only workspace mount,
explicitly declared writable mounts, and optional resource bounds. Capability
inspection reports only properties derived from flags this runner passes plus
confirmed runtime facts. An unavailable requested property is an unmet
requirement or ``UNKNOWN``; it is never a silent downgrade.

Trust boundary: the host kernel, the rootless container stack, and the host
root account remain part of the trusted computing base. The ``podman`` client
process itself runs with the operator's ambient environment as trusted launcher
code; only the declared allowlisted keys are forwarded into the container.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import time
from contextlib import suppress
from pathlib import Path

from .errors import ForgeError
from .execution import run_bounded, validate_argv, validate_limits
from .execution_observations import ExecutionObservationBuilder, canonical_sha256
from .identity import file_identity
from .paths import resolve_contained, validate_relative_path
from .ports import ExecutionObservation, ExecutionResult, ExecutionSession, RunnerCapabilities
from .resource_envelope import SystemdCgroupEnvelope
from .resource_process import CURRENT_CGROUP_PARENT_PLACEHOLDER

_MIN_PODMAN_MAJOR = 4
_PROBE_TIMEOUT_SECONDS = 60.0


def _parse_version(text: str) -> tuple[int, ...] | None:
    cleaned = "".join(
        character if character.isdigit() or character == "." else " " for character in text
    )
    parts = cleaned.split()
    if not parts:
        return None
    try:
        return tuple(int(piece) for piece in parts[0].split(".")[:3])
    except ValueError:
        return None


class PodmanRunner:
    """Execute declared commands inside a constrained rootless container."""

    runner_identity = "runner.podman-rootless-v1"

    def __init__(
        self,
        *,
        image: str,
        podman_path: str = "podman",
        writable_paths: list[str] | None = None,
        tmpfs_size: str | None = None,
        memory: str | None = None,
        cpus: str | None = None,
        pids_limit: int | None = None,
        resource_envelope: SystemdCgroupEnvelope | None = None,
    ) -> None:
        if os.name != "posix":
            raise ForgeError(
                "RUNNER_UNAVAILABLE",
                "the rootless Podman adapter requires a POSIX host",
            )
        resolved = shutil.which(podman_path)
        if resolved is None:
            raise ForgeError(
                "RUNNER_UNAVAILABLE",
                f"podman executable {podman_path!r} was not found on PATH",
            )
        self._podman = resolved
        self._image = image
        self._writable_paths = [
            str(validate_relative_path(value)) for value in writable_paths or []
        ]
        self._tmpfs_size = tmpfs_size
        self._memory = memory
        self._cpus = cpus
        self._pids_limit = pids_limit
        self._resource_envelope = resource_envelope
        version_text = self._probe(["--version"])
        parsed = _parse_version(version_text)
        if parsed is None or parsed[0] < _MIN_PODMAN_MAJOR:
            raise ForgeError(
                "RUNNER_UNAVAILABLE",
                f"unsupported podman client version: {version_text.strip()}",
            )
        self._client_version = version_text.strip()
        info = self._info()
        host = info.get("host")
        security = host.get("security") if isinstance(host, dict) else None
        if not isinstance(security, dict) or security.get("rootless") is not True:
            raise ForgeError(
                "RUNNER_UNAVAILABLE",
                "podman did not confirm rootless execution; refusing to claim isolation",
            )
        self._rootless_confirmed = True
        if self._image_exists() is False:
            raise ForgeError(
                "RUNNER_UNAVAILABLE",
                f"configured runner image {image!r} is not present; pull it before use",
            )
        self._image_digest = self._resolve_image_digest()

    # -- availability probes -------------------------------------------------

    def _probe(self, arguments: list[str]) -> str:
        try:
            result = run_bounded(
                [self._podman, *arguments],
                cwd=Path.cwd(),
                timeout=_PROBE_TIMEOUT_SECONDS,
                output_cap=1_048_576,
                environment=dict(os.environ),
                resource_envelope=self._resource_envelope,
            )
        except ForgeError as exc:
            raise ForgeError("RUNNER_UNAVAILABLE", f"podman probe failed: {exc.code}") from exc
        return result.stdout.decode("utf-8", errors="replace")

    def _info(self) -> dict[str, object]:
        text = self._probe(["info", "--format", "json"]).strip()
        try:
            value = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ForgeError(
                "RUNNER_UNAVAILABLE", f"podman info output is malformed: {exc}"
            ) from exc
        if not isinstance(value, dict):
            raise ForgeError("RUNNER_UNAVAILABLE", "podman info output must be an object")
        return value

    def _image_exists(self) -> bool | None:
        try:
            result = run_bounded(
                [self._podman, "image", "exists", self._image],
                cwd=Path.cwd(),
                timeout=_PROBE_TIMEOUT_SECONDS,
                output_cap=65536,
                environment=dict(os.environ),
                resource_envelope=self._resource_envelope,
            )
        except ForgeError as exc:
            if exc.code == "TIMEOUT":
                raise ForgeError("RUNNER_UNAVAILABLE", "podman image probe timed out") from exc
            return None
        return result.returncode == 0

    def _resolve_image_digest(self) -> str | None:
        text = self._probe(["image", "inspect", "--format", "{{json .}}", self._image]).strip()
        try:
            records = json.loads(text)
        except json.JSONDecodeError:
            return None
        if isinstance(records, dict):
            records = [records]
        if not isinstance(records, list) or not records or not isinstance(records[0], dict):
            return None
        record = records[0]
        digest = record.get("Digest")
        if isinstance(digest, str) and digest.startswith("sha256:") and len(digest) == 71:
            return digest[7:]
        repo_digests = record.get("RepoDigests")
        if isinstance(repo_digests, list):
            for entry in repo_digests:
                if isinstance(entry, str) and "@" in entry:
                    candidate = entry.rsplit("@", 1)[1]
                    if candidate.startswith("sha256:") and len(candidate) == 71:
                        return candidate[7:]
        # A tag alone is not an immutable image identity; report honestly.
        return None

    # -- Runner port ---------------------------------------------------------

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
    ) -> ExecutionResult:
        session = self.run(
            command,
            cwd=cwd,
            timeout=timeout,
            output_cap=output_cap,
            stderr_cap=stderr_cap,
            environment=environment,
            stdin=stdin,
        )
        if session.result is None:
            raise ForgeError(
                session.error_code or "COMMAND_START",
                session.error_message or "container execution produced no result",
            )
        return session.result

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
    ) -> ExecutionObservation:
        return self.run(
            command,
            cwd=cwd,
            timeout=timeout,
            output_cap=output_cap,
            stderr_cap=stderr_cap,
            environment=environment,
            stdin=stdin,
        ).observation

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
    ) -> ExecutionSession:
        """Run the declared argv inside the constrained container."""

        argv = validate_argv(command)
        validate_limits(timeout, output_cap, stderr_cap)
        mounts = self._writable_mounts(cwd)
        container_name = self._container_name(argv)
        container_argv = self._container_argv(
            argv,
            cwd=cwd,
            mounts=mounts,
            environment=environment,
            container_name=container_name,
        )
        builder = ExecutionObservationBuilder(
            argv=argv,
            cwd=cwd,
            timeout=timeout,
            stdout_limit=output_cap,
            stderr_limit=stderr_cap or output_cap,
            environment=environment,
            stdin=stdin,
            capabilities=self.inspect_capabilities(),
            runner_identity=self.runner_identity,
            runner_version=self._client_version,
            executable_identity=self._host_executable_identity(argv, cwd),
            host_identity=self._host_identity(),
            image_identity=self._image_digest,
            filesystem_policy="podman-read-only-rootfs+declared-mounts",
            network_policy="podman-network-none",
            same_operator=True,
        )
        builder.runtime_identity = f"runtime.podman-{self._client_version}"
        try:
            result = run_bounded(
                container_argv,
                cwd=cwd,
                timeout=timeout,
                output_cap=output_cap,
                stderr_cap=stderr_cap,
                # Podman resolves bare --env=NAME from its own environment.
                # Keep provider values out of transient-unit ExecStart argv;
                # the cgroup Runner stages this environment in its private file.
                environment={**os.environ, **environment},
                stdin=stdin,
                _observation=builder,
                resource_envelope=self._resource_envelope,
            )
        except ForgeError as exc:
            builder.failed(exc)
            self._cleanup(container_name)
            return builder.session(None, exc)
        builder.completed(result)
        return builder.session(result, None)

    def inspect_capabilities(self) -> RunnerCapabilities:
        envelope_capability = (
            self._resource_envelope.resource_limit_capability
            if self._resource_envelope is not None
            else "unknown"
        )
        return RunnerCapabilities(
            runner_kind="podman-rootless",
            runner_version=self._client_version,
            os_family=platform.system().lower() or "unknown",
            architecture=platform.machine().lower() or "unknown",
            execution_scope="local",
            shell_execution="disabled",
            timeout_enforcement="enforced",
            stdout_limit="enforced",
            stderr_limit="enforced",
            process_group_termination=("enforced" if os.name == "posix" else "not-provided"),
            sandbox_isolation=("enforced" if self._rootless_confirmed else "not-provided"),
            network_isolation="enforced",
            filesystem_isolation="enforced",
            memory_limit=("enforced" if self._memory else envelope_capability),
            process_count_limit=("enforced" if self._pids_limit else envelope_capability),
            aggregate_concurrency_limit=envelope_capability,
        )

    def resource_status(self) -> dict[str, object]:
        if self._resource_envelope is None:
            return {
                "state": "runner-limits-only",
                "mechanism": "podman-configured-limits",
                "configured_memory": self._memory,
                "configured_process_count": self._pids_limit,
                "configured_concurrency_max": None,
                "limitation": "aggregate concurrency and host headroom are not enforced",
            }
        return self._resource_envelope.status()

    def set_job_context(self, value: dict[str, object] | None) -> None:
        if self._resource_envelope is not None:
            self._resource_envelope.set_job_context(value)

    def inspect_runtime(self) -> dict[str, object]:
        """Report the exact enforced property set beyond the fixed port shape."""

        return {
            "runner_identity": self.runner_identity,
            "runner_version": self._client_version,
            "rootless_confirmed": self._rootless_confirmed,
            "network_isolation": "enforced (--network=none)",
            "filesystem_isolation": (
                "read-only root filesystem, read-only workspace mount, "
                "declared writable mounts only"
            ),
            "writable_mounts": list(self._writable_paths),
            "tmpfs_size": self._tmpfs_size,
            "resource_limits": {
                "memory": self._memory,
                "cpus": self._cpus,
                "pids_limit": self._pids_limit,
            },
            "image": self._image,
            "image_digest_sha256": self._image_digest,
            "capabilities": self.inspect_capabilities().to_dict(),
            "trusted_computing_base": [
                "host kernel",
                "rootless container runtime (podman, crun/runc)",
                "host root account",
                "container image contents",
            ],
        }

    # -- internals -----------------------------------------------------------

    def _container_argv(
        self,
        argv: list[str],
        *,
        cwd: Path,
        mounts: list[tuple[Path, str]],
        environment: dict[str, str],
        container_name: str,
    ) -> list[str]:
        container_argv = [
            self._podman,
            "run",
            "--rm",
            "-i",
            "--network=none",
            "--read-only",
            "--pull=never",
            "--cap-drop=all",
            f"--name={container_name}",
            f"--volume={cwd.resolve(strict=True)}:/workspace:ro",
        ]
        if self._resource_envelope is not None:
            # The transport helper substitutes the actual transient-service
            # cgroup path before exec.  Podman's complete container tree then
            # inherits this service's memory/PID limits and KillMode cleanup.
            container_argv.append(
                f"--cgroup-parent={CURRENT_CGROUP_PARENT_PLACEHOLDER}"
            )
        for source, target in mounts:
            # ``Z`` gives the container a private SELinux label so declared
            # writable mounts are usable on SELinux hosts; podman may relabel
            # the declared writable directory itself. Read-only inputs never
            # receive write labels.
            container_argv.append(f"--volume={source}:{target}:rw,Z")
        if self._tmpfs_size:
            container_argv.append(f"--tmpfs=/tmp:rw,size={self._tmpfs_size}")
        if self._memory:
            container_argv.append(f"--memory={self._memory}")
        if self._cpus:
            container_argv.append(f"--cpus={self._cpus}")
        if self._pids_limit is not None:
            container_argv.append(f"--pids-limit={self._pids_limit}")
        container_argv.append("--workdir=/workspace")
        for key in sorted(environment):
            container_argv.append(f"--env={key}")
        container_argv.extend(["--", self._image, *argv])
        return container_argv

    def _writable_mounts(self, cwd: Path) -> list[tuple[Path, str]]:
        resolved_cwd = cwd.resolve(strict=True)
        mounts: list[tuple[Path, str]] = []
        for relative in self._writable_paths:
            source = resolve_contained(resolved_cwd, relative, must_exist=True)
            if not source.is_dir():
                raise ForgeError(
                    "RUNNER_MOUNT_INVALID",
                    f"writable runner path is not a directory: {relative}",
                )
            mounts.append((source, f"/workspace/{relative}"))
        return mounts

    def _cleanup(self, container_name: str) -> None:
        """Best-effort removal of a container left by an interrupted run."""

        with suppress(ForgeError):
            run_bounded(
                [self._podman, "rm", "-f", "-t", "2", container_name],
                cwd=Path.cwd(),
                timeout=15.0,
                output_cap=65536,
                environment=dict(os.environ),
                resource_envelope=self._resource_envelope,
            )

    def _container_name(self, argv: list[str]) -> str:
        stamp = int(time.time() * 1000)
        return f"forge-{canonical_sha256({'argv': argv})[:12]}-{stamp}"

    @staticmethod
    def _host_executable_identity(argv: list[str], cwd: Path) -> str | None:
        executable = Path(argv[0])
        if not executable.is_absolute():
            executable = cwd / executable
        try:
            if not executable.is_file():
                return None
            identity = file_identity(executable)
        except (ForgeError, OSError):
            return None
        return identity.removeprefix("sha256:")

    @staticmethod
    def _host_identity() -> str:
        digest = canonical_sha256(
            {
                "node": platform.node(),
                "system": platform.system(),
                "machine": platform.machine(),
            }
        )
        return f"host.podman-{digest[:32]}"


def build_podman_runner(
    settings: dict[str, object], *, resource_envelope: SystemdCgroupEnvelope | None = None
) -> PodmanRunner:
    """Construct a validated runner from the declared [runner] configuration."""

    kind = settings.get("kind", "local-process")
    if kind != "podman-rootless":
        raise ForgeError("CONFIG_INVALID", f"unsupported runner kind: {kind!r}")
    image = settings.get("image")
    if not isinstance(image, str) or not image.strip():
        raise ForgeError("CONFIG_INVALID", "runner kind podman-rootless requires a declared image")
    writable_paths = settings.get("writable_paths", [])
    return PodmanRunner(
        image=image,
        podman_path=str(settings.get("podman_path", "podman")),
        writable_paths=[
            str(value) for value in (writable_paths if isinstance(writable_paths, list) else [])
        ],
        tmpfs_size=_optional_text(settings.get("tmpfs_size")),
        memory=_optional_text(settings.get("memory")),
        cpus=_optional_text(settings.get("cpus")),
        pids_limit=_optional_int(settings.get("pids_limit")),
        resource_envelope=resource_envelope,
    )


def _optional_text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _optional_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None
