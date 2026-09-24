"""Local adapters implementing inward-facing Forge application ports."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import tempfile
import threading
import time
from collections import OrderedDict
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
from .process_effect import OwnedProcess, ProcessEffectClient
from .resource_envelope import ResourceSemantics, SystemdCgroupEnvelope
from .serialization import local_json_identity, read_json

if TYPE_CHECKING:
    from .podman_runner import PodmanRunner


class LocalProcessRunner:
    """Run declared commands locally while preserving the bounded subprocess contract."""

    runner_identity = "runner.local-process-v1"

    def __init__(
        self,
        resource_envelope: SystemdCgroupEnvelope | None = None,
        *,
        process_capability: object | None = None,
    ) -> None:
        self.resource_envelope = resource_envelope
        self.process_capability = process_capability
        self._job_context: dict[str, object] = {}
        self._owned_lock = threading.RLock()
        self._owned_changed = threading.Condition(self._owned_lock)
        self._owned: dict[int, tuple[ProcessEffectClient, OwnedProcess, dict[str, object]]] = {}
        self._launching: set[int] = set()
        self._cancellation_waiters: set[int] = set()
        self._cancelled_generations: OrderedDict[int, None] = OrderedDict()

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
        if self.process_capability is not None:
            return self._run_process_effect(
                argv,
                cwd=cwd,
                timeout=timeout,
                output_cap=output_cap,
                stderr_cap=stderr_cap or output_cap,
                environment=environment,
                stdin=stdin,
                builder=builder,
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

    def cancel_generation(self, generation: int) -> dict[str, object]:
        """Cancel and reap the exact generic process bound to one generation."""

        decision_at = time.monotonic()
        with self._owned_lock:
            self._cancelled_generations[generation] = None
            self._cancelled_generations.move_to_end(generation)
            while len(self._cancelled_generations) > 64:
                self._cancelled_generations.popitem(last=False)
            self._cancellation_waiters.add(generation)
            self._owned_changed.wait_for(
                lambda: generation in self._owned or generation not in self._launching,
                timeout=5.0,
            )
            active = self._owned.get(generation)
            launching = generation in self._launching
        if active is None:
            with self._owned_changed:
                self._cancellation_waiters.discard(generation)
                self._owned_changed.notify_all()
            if launching:
                return {
                    "execution_generation": generation,
                    "handle_found": False,
                    "cancellation_pending": True,
                    "cancellation_complete": False,
                    "tree_empty": None,
                    "cleanup_complete": False,
                }
            return {
                "execution_generation": generation,
                "handle_found": False,
                "cancellation_complete": True,
                "tree_empty": True,
                "cleanup_complete": True,
            }
        client, process, facts = active
        requested_at = time.monotonic()
        try:
            requested = client.cancel(process)
            facts["cancel_requested_at"] = requested_at
            facts["cancel_request_observation"] = requested
            tree_empty_at: float | None = None
            launcher_reaped_at: float | None = None
            cleanup_complete_at: float | None = None
            reap_deadline = time.monotonic() + 5.0
            while True:
                reaped = client.observe(process)
                observed_at = time.monotonic()
                if (
                    tree_empty_at is None
                    and reaped.get("has_tree_empty") is True
                    and reaped.get("tree_empty") is True
                ):
                    tree_empty_at = observed_at
                if launcher_reaped_at is None and reaped.get("launcher_reaped") is True:
                    launcher_reaped_at = observed_at
                if (
                    reaped.get("has_cleanup_result") is True
                    and reaped.get("cleanup_complete") is True
                ):
                    cleanup_complete_at = observed_at
                    break
                if observed_at >= reap_deadline:
                    reaped = client.reap(process)
                    observed_at = time.monotonic()
                    if (
                        tree_empty_at is None
                        and reaped.get("has_tree_empty") is True
                        and reaped.get("tree_empty") is True
                    ):
                        tree_empty_at = observed_at
                    if launcher_reaped_at is None and reaped.get("launcher_reaped") is True:
                        launcher_reaped_at = observed_at
                    if (
                        reaped.get("has_cleanup_result") is True
                        and reaped.get("cleanup_complete") is True
                    ):
                        cleanup_complete_at = observed_at
                    break
                time.sleep(0.005)
            completed_at = time.monotonic()
            facts["reap_observation"] = reaped
            facts["cancel_completed_at"] = completed_at
            facts["cancel_request_to_reaped_seconds"] = round(completed_at - requested_at, 6)
            if tree_empty_at is not None:
                facts["cancel_request_to_tree_empty_observed_seconds"] = round(
                    tree_empty_at - requested_at, 6
                )
            if launcher_reaped_at is not None:
                facts["cancel_request_to_launcher_reaped_observed_seconds"] = round(
                    launcher_reaped_at - requested_at, 6
                )
            if cleanup_complete_at is not None:
                facts["cancel_request_to_cleanup_complete_observed_seconds"] = round(
                    cleanup_complete_at - requested_at, 6
                )
            cleanup_complete = reaped.get("cleanup_complete") is True
            cancellation_complete = (
                reaped.get("status") == "Cancelled"
                and reaped.get("cancellation_complete") is True
                and cleanup_complete
                and reaped.get("launcher_reaped") is True
                and reaped.get("tree_empty") is True
            )
            return {
                "execution_generation": generation,
                "handle_found": True,
                "decision_to_cancel_request_seconds": round(requested_at - decision_at, 6),
                "cancel_request_to_reaped_seconds": facts["cancel_request_to_reaped_seconds"],
                "cancel_request_to_tree_empty_observed_seconds": facts.get(
                    "cancel_request_to_tree_empty_observed_seconds"
                ),
                "cancel_request_to_launcher_reaped_observed_seconds": facts.get(
                    "cancel_request_to_launcher_reaped_observed_seconds"
                ),
                "cancel_request_to_cleanup_complete_observed_seconds": facts.get(
                    "cancel_request_to_cleanup_complete_observed_seconds"
                ),
                "cancellation_complete": cancellation_complete,
                "tree_empty": reaped.get("tree_empty"),
                "launcher_reaped": reaped.get("launcher_reaped"),
                "cleanup_complete": cleanup_complete,
                "status": reaped.get("status"),
            }
        finally:
            with self._owned_changed:
                self._cancellation_waiters.discard(generation)
                self._owned_changed.notify_all()

    def _run_process_effect(
        self,
        argv: list[str],
        *,
        cwd: Path,
        timeout: float,
        output_cap: int,
        stderr_cap: int,
        environment: dict[str, str],
        stdin: bytes,
        builder: ExecutionObservationBuilder,
    ) -> ExecutionSession:
        provider = getattr(self.process_capability, "process_effect_client", None)
        if not callable(provider):
            error = ForgeError(
                "PROCESS_EFFECT_UNAVAILABLE",
                "Forge has no generic MNCS process capability client",
            )
            builder.failed(error)
            return builder.session(None, error)
        generation_value: object
        with self._owned_lock:
            generation_value = self._job_context.get("generation")
            generation = (
                generation_value
                if isinstance(generation_value, int) and not isinstance(generation_value, bool)
                else None
            )
            if generation is not None and generation in self._cancelled_generations:
                error = ForgeError(
                    "EXECUTION_CANCELLED",
                    "the semantic owner cancelled this generation before process launch",
                    details={"execution_generation": generation},
                )
                builder.failed(error)
                return builder.session(None, error)
        limits = self.resource_envelope.budget if self.resource_envelope is not None else None
        if self.resource_envelope is not None and not self.resource_envelope.available:
            error = ForgeError(
                "RESOURCE_ENVELOPE_UNAVAILABLE",
                str(self.resource_envelope.status().get("limitation") or "resource envelope unavailable"),
                details={"resource_evidence": self.resource_envelope.status()},
            )
            builder.failed(error)
            return builder.session(None, error)
        if generation is not None:
            with self._owned_changed:
                if generation in self._cancelled_generations:
                    error = ForgeError(
                        "EXECUTION_CANCELLED",
                        "the semantic owner cancelled this generation before process launch",
                        details={"execution_generation": generation},
                    )
                    builder.failed(error)
                    return builder.session(None, error)
                self._launching.add(generation)
        try:
            client: ProcessEffectClient = provider()
            process, started = client.start(
                argv,
                cwd=cwd,
                environment=environment,
                stdin=stdin,
                stdout_limit=min(output_cap, 1024),
                stderr_limit=min(stderr_cap, 1024),
                deadline_ms=max(1, int(timeout * 1000)),
                memory_high_bytes=limits.memory_high_bytes if limits else 0,
                memory_max_bytes=limits.memory_max_bytes if limits else 0,
                swap_max_bytes=limits.memory_swap_max_bytes if limits else 0,
                has_swap_max=limits is not None,
                process_max=limits.tasks_max if limits else 0,
            )
            facts: dict[str, object] = {
                "process_handle": process,
                "start_observation": started,
                "generation": generation,
            }
            if generation is not None:
                with self._owned_changed:
                    self._owned[generation] = (client, process, facts)
                    self._launching.discard(generation)
                    cancelled_during_start = (
                        generation in self._cancelled_generations
                        and generation not in self._cancellation_waiters
                    )
                    self._owned_changed.notify_all()
                if cancelled_during_start:
                    client.cancel(process)
            builder.process_started()
            if limits is not None:
                builder.resource_started(limits.to_dict())
            observation = started
            while observation.get("status") == "Running":
                observation = client.observe(process)
                if observation.get("status") == "Running":
                    time.sleep(0.01)
            if observation.get("cleanup_complete") is not True:
                observation = client.reap(process)
            facts["terminal_observation"] = observation
            resource_observations = self._native_resource_observations(
                observation, timeout=timeout, process=process
            )
            builder.resource_finished(resource_observations)
            stdout = observation.get("stdout")
            stderr = observation.get("stderr")
            if not isinstance(stdout, bytes) or not isinstance(stderr, bytes):
                raise ForgeError("PROCESS_EFFECT_ABI", "MNCS process output was malformed")
            status = observation.get("status")
            exit_code = observation.get("exit_code")
            if status == "Exited" and observation.get("cleanup_complete") is True:
                if observation.get("has_exit_code") is not True or not isinstance(exit_code, int):
                    raise ForgeError(
                        "PROCESS_CLEANUP_UNKNOWN",
                        "MNCS process exited without an established exit status",
                        details={"resource_evidence": resource_observations},
                    )
                result = ExecutionResult(
                    argv=argv,
                    returncode=exit_code,
                    stdout=stdout,
                    stderr=stderr,
                    duration_seconds=float(observation.get("duration_ms", 0)) / 1000,
                    resource_envelope=limits.to_dict() if limits else {},
                    resource_observations=resource_observations,
                )
                builder.feed("stdout", stdout)
                builder.feed("stderr", stderr)
                if observation.get("stdout_truncated") is True:
                    builder.mark_limit("stdout", output_cap)
                if observation.get("stderr_truncated") is True:
                    builder.mark_limit("stderr", stderr_cap)
                builder.completed(result)
                return builder.session(result, None)
            error_code = {
                "Cancelled": "EXECUTION_CANCELLED",
                "TimedOut": "TIMEOUT",
                "ResourceExhausted": "RESOURCE_LIMIT",
                "OutputExhausted": "OUTPUT_LIMIT",
            }.get(str(status), "PROCESS_CLEANUP_UNKNOWN")
            error = ForgeError(
                error_code,
                f"MNCS process lifecycle ended as {status}; cleanup must be established before accepting a result",
                details={
                    "resource_evidence": resource_observations,
                    "process_observation": observation,
                    "execution_generation": generation,
                },
            )
            builder.failed(error)
            return builder.session(None, error)
        except ForgeError as error:
            if isinstance(error.details.get("resource_evidence"), Mapping):
                builder.resource_finished(error.details["resource_evidence"])
            builder.failed(error)
            return builder.session(None, error)
        finally:
            if generation is not None:
                with self._owned_changed:
                    self._owned.pop(generation, None)
                    self._launching.discard(generation)
                    self._owned_changed.notify_all()

    def _native_resource_observations(
        self,
        observation: Mapping[str, object],
        *,
        timeout: float,
        process: OwnedProcess,
    ) -> dict[str, object]:
        classify = getattr(self.process_capability, "resource_outcome", None)
        duration_seconds = float(observation.get("duration_ms", 0)) / 1000
        raw = {
            "exit_status": observation.get("exit_code")
            if observation.get("has_exit_code") is True
            else None,
            "timed_out": observation.get("status") == "TimedOut",
            "output_limited": observation.get("status") == "OutputExhausted",
            "memory_high_events": observation.get("memory_high_events"),
            "memory_max_events": observation.get("memory_max_events"),
            "memory_oom_events": observation.get("oom_events"),
            "memory_oom_kill_events": observation.get("oom_kill_events"),
            "memory_oom_group_kill_events": 0,
            "process_limit_events": observation.get("process_limit_events"),
            "cleanup_known": observation.get("has_cleanup_result") is True,
            "cleanup_succeeded": observation.get("cleanup_complete") is True,
            "cancellation_requested": observation.get("cancellation_requested") is True,
            "superseded": False,
        }
        outcome = classify(raw) if callable(classify) else None
        metric = getattr(outcome, "metric", "Unknown")
        observed_value: object = None
        bound_value: object = None
        if metric == "WallTime":
            observed_value = duration_seconds
            bound_value = timeout
        elif metric == "Output":
            observed_value = max(
                len(observation.get("stdout", b""))
                if isinstance(observation.get("stdout"), bytes)
                else 0,
                len(observation.get("stderr", b""))
                if isinstance(observation.get("stderr"), bytes)
                else 0,
            )
        values: dict[str, object] = {
            "process_status": observation.get("status"),
            "process_observation_complete": observation.get("observation_complete"),
            "tree_empty": observation.get("tree_empty"),
            "launcher_reaped": observation.get("launcher_reaped"),
            "cleanup_complete": observation.get("cleanup_complete"),
            "cancellation_requested": observation.get("cancellation_requested"),
            "cancellation_complete": observation.get("cancellation_complete"),
            "resource_observations": dict(raw),
            "execution_handle_identity": process.value.get("record", {}).get("type_identity"),
            "resource_outcome": getattr(outcome, "status", "Unknown"),
            "resource_exhausted": getattr(outcome, "resource_exhausted", False),
            "verification_deferred": getattr(outcome, "deferred", False),
            "native_execution_returncode_available": getattr(
                outcome, "has_execution_exit_status", False
            ),
            "native_execution_returncode": getattr(outcome, "execution_exit_status", None),
            "resource_metric": metric,
            "resource_bound": bound_value,
            "resource_observed": observed_value,
        }
        return values

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
        with self._owned_lock:
            self._job_context = (
                {str(key): item for key, item in value.items()}
                if isinstance(value, Mapping)
                else {}
            )
        if self.resource_envelope is not None:
            self.resource_envelope.set_job_context(value)


# Preserve the existing concrete adapter name while callers migrate to the runner vocabulary.
LocalCommandExecutor = LocalProcessRunner


def build_runner(
    config: ForgeConfig, *, resource_semantics: ResourceSemantics | None = None
) -> LocalProcessRunner | PodmanRunner:
    """Construct the declared project runner, failing closed when unavailable."""

    settings = config.runner_settings
    kind = str(settings.get("kind", "local-process"))
    continuous_enabled = bool(config.continuous_settings.get("enabled", False))
    resource_envelope = (
        SystemdCgroupEnvelope(
            _mapping(config.continuous_settings.get("resource_envelope")),
            required=True,
            resource_semantics=resource_semantics,
        )
        if continuous_enabled
        else None
    )
    if kind == "local-process":
        return LocalProcessRunner(
            resource_envelope,
            process_capability=resource_semantics,
        )
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
