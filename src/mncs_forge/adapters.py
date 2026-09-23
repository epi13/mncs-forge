"""Local adapters implementing inward-facing Forge application ports."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING

from .config import ForgeConfig, Provider
from .errors import ForgeError
from .execution import run_bounded, validate_argv, validate_limits
from .execution_observations import ExecutionObservationBuilder
from .identity import content_identity, file_identity, identity_map
from .paths import is_within, resolve_contained, validate_relative_path
from .ports import ExecutionObservation, ExecutionResult, ExecutionSession, RunnerCapabilities
from .resource_envelope import SystemdCgroupEnvelope
from .serialization import local_json_identity, read_json

if TYPE_CHECKING:
    from .podman_runner import PodmanRunner


class LocalProcessRunner:
    """Run declared commands locally while preserving the bounded subprocess contract."""

    runner_identity = "runner.local-process-v1"

    def __init__(self, resource_envelope: SystemdCgroupEnvelope | None = None) -> None:
        self.resource_envelope = resource_envelope

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
        return run_bounded(
            command,
            cwd=cwd,
            timeout=timeout,
            output_cap=output_cap,
            stderr_cap=stderr_cap,
            environment=environment,
            stdin=stdin,
            resource_envelope=self.resource_envelope,
        )

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
        """Execute through the same bounded path while retaining raw observations."""

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
        """Return retained output and observation facts from one local invocation."""

        argv = validate_argv(command)
        validate_limits(timeout, output_cap, stderr_cap)
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
            runner_version="1",
            executable_identity=self._executable_identity(argv, cwd),
            host_identity=self._host_identity(),
            filesystem_policy="unrestricted-process-workspace",
            network_policy="ambient-process-network",
            same_operator=True,
        )
        try:
            result = run_bounded(
                argv,
                cwd=cwd,
                timeout=timeout,
                output_cap=output_cap,
                stderr_cap=stderr_cap,
                environment=environment,
                stdin=stdin,
                _observation=builder,
                resource_envelope=self.resource_envelope,
            )
        except ForgeError as exc:
            resource_evidence = exc.details.get("resource_evidence")
            if isinstance(resource_evidence, Mapping):
                envelope_identity = resource_evidence.get("resource_envelope_identity")
                if envelope_identity is not None:
                    builder.resource_started({"identity": envelope_identity})
                builder.resource_finished(resource_evidence)
            builder.failed(exc)
            return builder.session(None, exc)
        builder.completed(result)
        return builder.session(result, None)

    @staticmethod
    def _host_identity() -> str:
        digest = hashlib.sha256(platform.node().encode("utf-8", errors="replace")).hexdigest()
        return f"host.local-{digest[:32]}"

    @staticmethod
    def _executable_identity(argv: list[str], cwd: Path) -> str | None:
        executable = Path(argv[0])
        if not executable.is_absolute() and (executable.parent != Path(".")):
            executable = cwd / executable
        else:
            resolved = shutil.which(argv[0])
            if resolved is None:
                return None
            executable = Path(resolved)
        try:
            identity = file_identity(executable)
        except ForgeError:
            return None
        return identity.removeprefix("sha256:")

    def inspect_capabilities(self) -> RunnerCapabilities:
        resource_capability = (
            self.resource_envelope.resource_limit_capability
            if self.resource_envelope is not None
            else "unknown"
        )
        return RunnerCapabilities(
            runner_kind="local-process",
            runner_version="1",
            os_family=platform.system().lower() or "unknown",
            architecture=platform.machine().lower() or "unknown",
            execution_scope="local",
            shell_execution="disabled",
            timeout_enforcement="enforced",
            stdout_limit="enforced",
            stderr_limit="enforced",
            process_group_termination=("enforced" if os.name == "posix" else "not-provided"),
            sandbox_isolation="not-provided",
            network_isolation="not-provided",
            filesystem_isolation="not-provided",
            memory_limit=resource_capability,
            process_count_limit=resource_capability,
            aggregate_concurrency_limit=resource_capability,
        )

    def resource_status(self) -> dict[str, object]:
        if self.resource_envelope is None:
            return {
                "state": "not-required",
                "mechanism": None,
                "limitation": "continuous resource envelope is not enabled for this Forge instance",
            }
        return self.resource_envelope.status()

    def set_job_context(self, value: Mapping[str, object] | None) -> None:
        if self.resource_envelope is not None:
            self.resource_envelope.set_job_context(value)


# Preserve the existing concrete adapter name while callers migrate to the runner vocabulary.
LocalCommandExecutor = LocalProcessRunner


def build_runner(config: ForgeConfig) -> LocalProcessRunner | PodmanRunner:
    """Construct the declared project runner, failing closed when unavailable."""

    settings = config.runner_settings
    kind = str(settings.get("kind", "local-process"))
    continuous_enabled = bool(config.continuous_settings.get("enabled", False))
    resource_envelope = (
        SystemdCgroupEnvelope(
            _mapping(config.continuous_settings.get("resource_envelope")), required=True
        )
        if continuous_enabled
        else None
    )
    if kind == "local-process":
        return LocalProcessRunner(resource_envelope)
    if kind == "podman-rootless":
        from .podman_runner import build_podman_runner

        return build_podman_runner(settings, resource_envelope=resource_envelope)
    raise ForgeError("CONFIG_INVALID", f"unsupported runner kind: {kind!r}")


def _mapping(value: object) -> dict[str, object]:
    return value if isinstance(value, dict) else {}


class LocalProjectObserver:
    """Observe project identities and prepare local copied workspaces."""

    def __init__(self, config: ForgeConfig) -> None:
        self.config = config

    def authority_paths(self) -> list[Path]:
        return [
            *self.config.paths("contracts"),
            *self.config.paths("references"),
            *self.config.paths("evaluators"),
            *self.config.paths("acceptance_policies"),
            *self.config.paths("protected"),
        ]

    def candidate_paths(self) -> list[Path]:
        return [*self.config.paths("candidates"), *self.config.paths("generated")]

    def current_candidate_identity(self) -> str:
        return content_identity(self.config.root, self.candidate_paths())

    def current_authority_identities(self) -> dict[str, str]:
        return identity_map(self.config.root, self.authority_paths())

    def content_identity(self, paths: list[Path]) -> str:
        return content_identity(self.config.root, paths)

    def identity_map(self, paths: list[Path]) -> dict[str, str]:
        return identity_map(self.config.root, paths)

    def selection_evidence_policy(self) -> tuple[str, tuple[str, ...], str | None]:
        policy_path = resolve_contained(
            self.config.root,
            str(self.config.raw["policies"]["selection"]),
            must_exist=False,
        )
        policy_identity = content_identity(self.config.root, [policy_path])
        try:
            value = read_json(policy_path, byte_cap=self.config.output_cap)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            return policy_identity, (), f"selection policy cannot be read: {exc}"
        if not isinstance(value, dict):
            return policy_identity, (), "selection policy must be a JSON object"
        raw = value.get("required_workflows", value.get("required"))
        if (
            not isinstance(raw, list)
            or not raw
            or not all(isinstance(item, str) and item for item in raw)
        ):
            return (
                policy_identity,
                (),
                "selection policy must declare a non-empty required_workflows or required list",
            )
        return policy_identity, tuple(dict.fromkeys(raw)), None

    def evidence_envelopes(
        self,
    ) -> tuple[dict[str, tuple[str, ...]], dict[str, str], dict[str, str]]:
        workflow_environment_keys = {
            name: tuple(sorted(self.config.environment(workflow)))
            for name, workflow in self.config.workflows.items()
        }
        verifier_environment_identities = {
            verifier_id: local_json_identity(
                self.config.environment(self.config.workflows[verifier.workflow])
            )
            for verifier_id, verifier in self.config.verifiers.items()
        }
        policy_paths = [
            *self.config.paths("acceptance_policies"),
            resolve_contained(
                self.config.root,
                str(self.config.raw["policies"]["selection"]),
                must_exist=False,
            ),
            resolve_contained(
                self.config.root,
                str(self.config.raw["policies"]["useful_benefit_objective"]),
                must_exist=False,
            ),
        ]
        policy_identity = content_identity(self.config.root, policy_paths)
        return (
            workflow_environment_keys,
            verifier_environment_identities,
            {verifier_id: policy_identity for verifier_id in self.config.verifiers},
        )

    def current_freeze_bindings(
        self,
        candidate_identity: str | None = None,
        freeze: Mapping[str, object] | None = None,
    ) -> dict[str, str]:
        bindings = {
            "candidate_identity": candidate_identity or self.current_candidate_identity(),
            "contract_identity": content_identity(self.config.root, self.config.paths("contracts")),
            "reference_identity": content_identity(
                self.config.root, self.config.paths("references")
            ),
            "evaluator_identity": content_identity(
                self.config.root, self.config.paths("evaluators")
            ),
            "acceptance_policy_identity": content_identity(
                self.config.root, self.config.paths("acceptance_policies")
            ),
            "protected_identity": content_identity(
                self.config.root, self.config.paths("protected")
            ),
        }
        plan = freeze.get("required_evidence_plan") if freeze is not None else None
        if isinstance(plan, str):
            plan_path = resolve_contained(self.config.root, plan, must_exist=False)
            bindings["required_evidence_plan_identity"] = content_identity(
                self.config.root, [plan_path]
            )
        return bindings

    def provider_executable(self, provider: Provider) -> tuple[Path, str]:
        value = provider.command[0]
        if "/" in value:
            path = Path(value)
            if path.is_absolute():
                try:
                    executable = path.resolve(strict=True)
                except OSError as exc:
                    raise ForgeError(
                        "PROVIDER_UNAVAILABLE",
                        f"provider {provider.provider_id} executable is unavailable: {exc}",
                    ) from exc
            else:
                executable = resolve_contained(self.config.root, value, must_exist=True)
        else:
            resolved = shutil.which(
                value, path=self.config.provider_environment(provider).get("PATH", "")
            )
            if resolved is None:
                raise ForgeError(
                    "PROVIDER_UNAVAILABLE",
                    f"provider {provider.provider_id} executable is not on the allowlisted PATH",
                )
            executable = Path(resolved).resolve(strict=True)
        if not executable.is_file() or not os.access(executable, os.X_OK):
            raise ForgeError(
                "PROVIDER_UNAVAILABLE",
                f"provider {provider.provider_id} executable is not an executable file",
            )
        identity = file_identity(executable)
        if provider.executable_identity and identity != provider.executable_identity:
            raise ForgeError(
                "PROVIDER_IDENTITY_DRIFT",
                f"provider {provider.provider_id} executable identity drifted",
            )
        return executable, identity

    def provider_workspace(self, *, evaluator: bool = False) -> tempfile.TemporaryDirectory[str]:
        workspace_byte_limit = int(self.config.verifier_limits["workspace_bytes"])
        workspace_entry_limit = int(self.config.verifier_limits["max_workspace_entries"])
        temporary = tempfile.TemporaryDirectory(prefix="mncs-forge-provider-")
        workspace = Path(temporary.name)
        visible_keys = [
            "candidates",
            "generated",
            "contracts",
            "references",
            "development_evidence",
            "evaluators",
            "acceptance_policies",
        ]
        if evaluator:
            visible_keys.append("protected")
        sources = [
            source
            for key in visible_keys
            for source in self.config.paths(key)
            if source.exists()
        ]
        try:
            self._check_workspace_bound(
                sources,
                byte_limit=workspace_byte_limit,
                entry_limit=workspace_entry_limit,
            )
            for source in sources:
                relative = source.relative_to(self.config.root)
                target = workspace / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                if source.is_dir():
                    shutil.copytree(source, target, symlinks=False, dirs_exist_ok=True)
                else:
                    shutil.copy2(source, target, follow_symlinks=True)
        except BaseException:
            temporary.cleanup()
            raise
        return temporary

    def _check_workspace_bound(
        self, sources: list[Path], *, byte_limit: int, entry_limit: int
    ) -> None:
        """Preflight copy size without materializing file contents in Forge memory."""

        entries = 0
        total_bytes = 0
        visited_directories: set[tuple[int, int]] = set()
        pending = list(sources)
        root = self.config.root.resolve()
        while pending:
            path = pending.pop()
            try:
                resolved = path.resolve(strict=True)
                resolved.relative_to(root)
                metadata = resolved.stat()
            except (OSError, ValueError) as error:
                raise ForgeError(
                    "WORKSPACE_SCOPE",
                    f"provider workspace input is unavailable or escapes the workspace: {path.name}",
                ) from error
            entries += 1
            if entries > entry_limit:
                raise ForgeError(
                    "WORKSPACE_LIMIT",
                    f"provider workspace exceeds the {entry_limit}-entry bound",
                    details={
                        "resource_evidence": {
                            "resource_metric": "workspace-entry-count",
                            "resource_bound": entry_limit,
                            "resource_observed": entries,
                            "resource_exhausted": True,
                        }
                    },
                )
            if resolved.is_dir():
                identity = (metadata.st_dev, metadata.st_ino)
                if identity in visited_directories:
                    continue
                visited_directories.add(identity)
                try:
                    with os.scandir(resolved) as children:
                        for child in children:
                            if entries + len(pending) >= entry_limit:
                                observed = entries + len(pending) + 1
                                raise ForgeError(
                                    "WORKSPACE_LIMIT",
                                    f"provider workspace exceeds the {entry_limit}-entry bound",
                                    details={
                                        "resource_evidence": {
                                            "resource_metric": "workspace-entry-count",
                                            "resource_bound": entry_limit,
                                            "resource_observed": observed,
                                            "resource_exhausted": True,
                                        }
                                    },
                                )
                            pending.append(Path(child.path))
                except ForgeError:
                    raise
                except OSError as error:
                    raise ForgeError(
                        "WORKSPACE_UNKNOWN",
                        f"provider workspace directory cannot be enumerated: {resolved.name}",
                    ) from error
            else:
                total_bytes += metadata.st_size
                if total_bytes > byte_limit:
                    raise ForgeError(
                        "WORKSPACE_LIMIT",
                        f"provider workspace exceeds the {byte_limit}-byte bound",
                        details={
                            "resource_evidence": {
                                "resource_metric": "workspace-bytes",
                                "resource_bound": byte_limit,
                                "resource_observed": total_bytes,
                                "resource_exhausted": True,
                            }
                        },
                    )

    def validate_changed_files(self, changed_files: list[str]) -> dict[str, str]:
        writable = self.config.relative_scopes("candidates", "generated")
        protected = self.config.relative_scopes(
            "contracts", "references", "evaluators", "acceptance_policies", "protected"
        )
        identities: dict[str, str] = {}
        for value in sorted(set(changed_files)):
            relative = validate_relative_path(value)
            if is_within(relative, protected):
                raise ForgeError(
                    "PROTECTED_MODIFICATION",
                    f"candidate change touches protected authority: {value}",
                )
            if not is_within(relative, writable):
                raise ForgeError(
                    "WRITE_BOUNDARY", f"candidate change is outside declared write paths: {value}"
                )
            resolved = resolve_contained(self.config.root, value, must_exist=True)
            if not resolved.is_file():
                raise ForgeError("INVALID_CHANGED_FILE", f"changed path is not a file: {value}")
            identities[value] = file_identity(resolved)
        return identities

    def command_path(self, command: list[str]) -> str | None:
        return shutil.which(command[0]) or (
            str(resolve_contained(self.config.root, command[0], must_exist=False))
            if "/" in command[0]
            else None
        )
