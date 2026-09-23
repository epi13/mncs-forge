"""Adapter from Forge runner observations to the experimental MNCS envelope.

This module does not persist receipts, interpret assurance, or execute commands.  It
only binds a complete raw observation to identities supplied by the caller.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal

from .errors import ForgeError
from .execution_observations import canonical_sha256
from .ports import ExecutionObservation, RunnerCapabilities, StreamObservation

ReceiptStatus = Literal["PASS", "FAIL", "UNKNOWN"]
EnforcementState = Literal["enforced", "not-enforced", "unknown"]
SubjectFamily = Literal["MNCS", "MNCDS"]
SubjectKind = Literal["contract", "assurance", "threat", "measurement", "development-record"]

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_EXTENSION = re.compile(r"^[a-z][a-z0-9.-]*:[A-Za-z0-9][A-Za-z0-9._-]*$")


@dataclass(frozen=True, slots=True)
class ReceiptArtifact:
    identity: str
    kind: str
    size_bytes: int
    retained: bool


@dataclass(frozen=True, slots=True)
class ExecutionPlacementReference:
    record_id: str
    identity: str
    subject_identity: str
    environment_identity: str

    def to_dict(self) -> dict[str, str]:
        return {
            "record_id": self.record_id,
            "identity": self.identity,
            "subject_identity": self.subject_identity,
            "environment_identity": self.environment_identity,
        }


@dataclass(frozen=True, slots=True)
class ReceiptContext:
    """Context that a generic runner cannot truthfully invent."""

    record_id: str
    subject_family: SubjectFamily
    subject_kind: SubjectKind
    subject_record_id: str
    subject_canonical_sha256: str
    candidate_id: str | None
    test_bundle_identity: str
    harness_identity: str | None
    input_snapshot_identity: str | None
    execution_policy_identity: str
    placement_policy_identity: str | None
    result_semantics: str
    challenge_nonce: str
    challenge_issued_at: str
    challenge_expires_at: str
    observed_at: str
    harness_status: ReceiptStatus = "UNKNOWN"
    result_identity: str | None = None
    artifacts: tuple[ReceiptArtifact, ...] = ()
    command_binding: EnforcementState = "unknown"
    environment_binding: EnforcementState = "unknown"
    test_bundle_integrity: EnforcementState = "unknown"
    result_integrity: EnforcementState = "unknown"
    placement: ExecutionPlacementReference | None = None
    extensions: Mapping[str, object] = field(default_factory=dict)


def _require_id(value: str, field_name: str) -> str:
    if not isinstance(value, str) or _ID.fullmatch(value) is None:
        raise ForgeError("RECEIPT_CONTEXT", f"{field_name} must be a bounded MNCS identifier")
    return value


def _require_sha256(value: str, field_name: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ForgeError("RECEIPT_CONTEXT", f"{field_name} must be a lowercase SHA-256 identity")
    return value


def _optional_sha256(value: str | None, field_name: str) -> str | None:
    if value is None:
        return None
    return _require_sha256(value, field_name)


def _require_enforcement(value: str, field_name: str) -> EnforcementState:
    if value not in {"enforced", "not-enforced", "unknown"}:
        raise ForgeError("RECEIPT_CONTEXT", f"{field_name} has an invalid enforcement state")
    return value  # type: ignore[return-value]


def _require_subject(value: str, field_name: str, allowed: set[str]) -> str:
    if value not in allowed:
        raise ForgeError("RECEIPT_CONTEXT", f"{field_name} has an unsupported value")
    return value


def _stream(value: StreamObservation, field_name: str) -> dict[str, object]:
    if value.total_bytes is None or value.truncated is None:
        raise ForgeError(
            "RECEIPT_OBSERVATION",
            f"{field_name} does not establish a complete total byte count",
        )
    return {
        "total_bytes": value.total_bytes,
        "retained_bytes": value.retained_bytes,
        "retained_sha256": value.retained_sha256,
        "complete_sha256": value.complete_sha256,
        "truncated": value.truncated,
        "limit_hit": value.limit_hit,
        "limit_bytes": value.limit_bytes,
    }


def _capability_state(value: str) -> EnforcementState:
    if value == "enforced":
        return "enforced"
    if value == "not-provided":
        return "not-enforced"
    return "unknown"


def _resource_limit_state(capabilities: RunnerCapabilities) -> EnforcementState:
    values = (
        capabilities.timeout_enforcement,
        capabilities.stdout_limit,
        capabilities.stderr_limit,
        capabilities.memory_limit,
        capabilities.process_count_limit,
        capabilities.aggregate_concurrency_limit,
    )
    if all(value == "enforced" for value in values):
        return "enforced"
    if any(value == "unknown" for value in values):
        return "unknown"
    return "not-enforced"


def build_mncs_execution_receipt(
    observation: ExecutionObservation, context: ReceiptContext
) -> dict[str, object]:
    """Build one unpersisted ``mncs-execution-receipt`` observation envelope."""

    record_id = _require_id(context.record_id, "record_id")
    subject_family = _require_subject(context.subject_family, "subject_family", {"MNCS", "MNCDS"})
    subject_kind = _require_subject(
        context.subject_kind,
        "subject_kind",
        {"contract", "assurance", "threat", "measurement", "development-record"},
    )
    subject_record_id = _require_id(context.subject_record_id, "subject_record_id")
    subject_identity = _require_sha256(context.subject_canonical_sha256, "subject_canonical_sha256")
    bundle_identity = _require_sha256(context.test_bundle_identity, "test_bundle_identity")
    execution_policy_identity = _require_sha256(
        context.execution_policy_identity, "execution_policy_identity"
    )
    harness_identity = _optional_sha256(context.harness_identity, "harness_identity")
    input_snapshot_identity = _optional_sha256(
        context.input_snapshot_identity, "input_snapshot_identity"
    )
    placement_policy_identity = _optional_sha256(
        context.placement_policy_identity, "placement_policy_identity"
    )
    result_identity = _optional_sha256(context.result_identity, "result_identity")
    candidate_id = (
        _require_id(context.candidate_id, "candidate_id")
        if context.candidate_id is not None
        else None
    )
    if not context.result_semantics:
        raise ForgeError("RECEIPT_CONTEXT", "result_semantics is required")
    if not context.challenge_nonce:
        raise ForgeError("RECEIPT_CONTEXT", "challenge_nonce is required")
    if context.harness_status not in {"PASS", "FAIL", "UNKNOWN"}:
        raise ForgeError("RECEIPT_CONTEXT", "harness_status has an invalid status")
    command_binding = _require_enforcement(context.command_binding, "command_binding")
    environment_binding = _require_enforcement(context.environment_binding, "environment_binding")
    test_bundle_integrity = _require_enforcement(
        context.test_bundle_integrity, "test_bundle_integrity"
    )
    result_integrity = _require_enforcement(context.result_integrity, "result_integrity")
    if observation.started_at is None or observation.ended_at is None:
        raise ForgeError("RECEIPT_OBSERVATION", "execution lifecycle timestamps are incomplete")
    if observation.duration_seconds is None:
        raise ForgeError("RECEIPT_OBSERVATION", "execution duration is unavailable")
    stdout = _stream(observation.stdout, "stdout")
    stderr = _stream(observation.stderr, "stderr")
    aggregate = observation.aggregate_output
    if aggregate.total_bytes is None:
        raise ForgeError("RECEIPT_OBSERVATION", "aggregate output total is unavailable")
    if result_identity is not None and not any(
        artifact.identity == result_identity for artifact in context.artifacts
    ):
        raise ForgeError(
            "RECEIPT_CONTEXT",
            "result_identity must reference one of the retained artifacts",
        )
    artifacts = []
    for index, artifact in enumerate(context.artifacts):
        artifacts.append(
            {
                "identity": _require_sha256(artifact.identity, f"artifacts[{index}].identity"),
                "kind": _require_id(artifact.kind, f"artifacts[{index}].kind"),
                "size_bytes": artifact.size_bytes,
                "retained": artifact.retained,
            }
        )
    extension_values = dict(context.extensions)
    if any(_EXTENSION.fullmatch(key) is None for key in extension_values):
        raise ForgeError("RECEIPT_CONTEXT", "extension keys must be namespaced")
    extension_values["forge:local-process"] = {
        "cwd_identity": observation.cwd_identity,
        "stdin_identity": observation.stdin_identity,
        "stdout_limit_bytes": observation.stdout_limit,
        "stderr_limit_bytes": observation.stderr_limit,
        "capabilities": observation.capabilities.to_dict(),
        "termination_error_code": observation.error_code,
        "resource_envelope": dict(observation.resource_envelope),
        "resource_observations": dict(observation.resource_observations),
    }
    placement = None
    if context.placement is not None:
        placement_identity = _require_sha256(context.placement.identity, "placement.identity")
        placement_subject = _require_sha256(
            context.placement.subject_identity, "placement.subject_identity"
        )
        placement_environment = _require_id(
            context.placement.environment_identity, "placement.environment_identity"
        )
        if (
            placement_subject != subject_identity
            or placement_environment != observation.environment_identity
        ):
            raise ForgeError(
                "RECEIPT_CONTEXT", "placement reference is bound to another subject or environment"
            )
        placement = {
            "record_id": _require_id(context.placement.record_id, "placement.record_id"),
            "identity": placement_identity,
            "subject_identity": placement_subject,
            "environment_identity": placement_environment,
        }
    requested_output = max(observation.stdout_limit, observation.stderr_limit)
    receipt: dict[str, object] = {
        "schema_version": "0.1-experimental",
        "record_type": "mncs-execution-receipt",
        "record_id": record_id,
        "receipt_identity": None,
        "subject": {
            "family": subject_family,
            "kind": subject_kind,
            "record_id": subject_record_id,
            "canonical_sha256": subject_identity,
            "candidate_id": candidate_id,
        },
        "bundle": {
            "test_bundle_identity": bundle_identity,
            "harness_identity": harness_identity,
            "input_snapshot_identity": input_snapshot_identity,
        },
        "policy": {
            "execution_policy_identity": execution_policy_identity,
            "placement_policy_identity": placement_policy_identity,
            "requested_limits": [
                {"resource": "timeout", "value": observation.timeout_seconds, "unit": "seconds"},
                {"resource": "output", "value": requested_output, "unit": "bytes"},
                *(
                    [
                        {
                            "resource": "host-memory",
                            "value": observation.resource_envelope["memory_max_bytes"],
                            "unit": "bytes",
                        },
                        {
                            "resource": "concurrency",
                            "value": observation.resource_envelope["concurrency_max"],
                            "unit": "count",
                        },
                    ]
                    if observation.resource_envelope
                    else []
                ),
            ],
            "result_semantics": context.result_semantics,
        },
        "runner": {
            "runner_identity": _require_id(observation.runner_identity, "runner_identity"),
            "runner_version": observation.runner_version,
            "executable_identity": _optional_sha256(
                observation.executable_identity, "executable_identity"
            ),
            "runtime_identity": (
                _require_id(observation.runtime_identity, "runtime_identity")
                if observation.runtime_identity is not None
                else None
            ),
            "command_identity": _require_sha256(observation.command_identity, "command_identity"),
        },
        "environment": {
            "environment_identity": _require_id(
                observation.environment_identity, "environment_identity"
            )
        },
        "challenge": {
            "nonce": context.challenge_nonce,
            "issued_at": context.challenge_issued_at,
            "expires_at": context.challenge_expires_at,
        },
        "request": {"status": "accepted", "observed_at": context.observed_at},
        "lifecycle": {
            "started_at": observation.started_at,
            "ended_at": observation.ended_at,
            "duration_seconds": observation.duration_seconds,
            "termination_category": observation.termination_category,
        },
        "process": {
            "exit_code": observation.returncode,
            "signal": observation.signal,
            "harness_status": context.harness_status,
            "result_identity": result_identity,
        },
        "termination_observations": {
            "timeout_seconds": (
                observation.timeout_seconds
                if observation.termination_category == "timeout"
                else None
            ),
            "resource_name": None,
        },
        "streams": {"stdout": stdout, "stderr": stderr},
        "aggregate_output": {
            "total_bytes": aggregate.total_bytes,
            "retained_bytes": aggregate.retained_bytes,
            "limit_bytes": aggregate.limit_bytes,
            "limit_hit": aggregate.limit_hit,
        },
        "artifacts": artifacts,
        "resources": [
            {
                "metric": "wall-duration",
                "value": observation.duration_seconds,
                "unit": "seconds",
                "source_identity": "forge:wall-clock-v1",
                "phase": "whole-execution",
            }
        ],
        "enforcement": {
            "command_binding": command_binding,
            "environment_binding": environment_binding,
            "filesystem_restriction": _capability_state(
                observation.capabilities.filesystem_isolation
            ),
            "network_restriction": _capability_state(observation.capabilities.network_isolation),
            "process_restriction": _capability_state(
                observation.capabilities.process_count_limit
            ),
            "resource_limits": _resource_limit_state(observation.capabilities),
            "test_bundle_integrity": test_bundle_integrity,
            "result_integrity": result_integrity,
        },
        "placement": {"execution_placement_reference": placement},
        "claim_boundary": {
            "conformance": "not-asserted",
            "correctness": "not-asserted",
            "security": "not-asserted",
            "sandbox": "not-asserted",
            "independence": "not-asserted",
            "protected_custody": "not-asserted",
            "promotion": "not-asserted",
        },
        "extensions": extension_values,
    }
    resource_facts = observation.resource_observations.get("resource_observations")
    if isinstance(resource_facts, Mapping):
        for metric, key, unit in (
            (
                "host-memory-peak",
                "cgroup_memory_peak_bytes"
                if "cgroup_memory_peak_bytes" in resource_facts
                else "process_rss_peak_bytes",
                "bytes",
            ),
            ("process-count", "process_count_peak", "count"),
            ("cpu-time", "cpu_time_microseconds", "seconds"),
        ):
            value = resource_facts.get(key)
            if isinstance(value, int) and not isinstance(value, bool):
                measured = value / 1_000_000 if metric == "cpu-time" else value
                receipt["resources"].append(
                    {
                        "metric": metric,
                        "value": measured,
                        "unit": unit,
                        "source_identity": str(
                            observation.resource_envelope.get("identity", "forge:cgroup-v2")
                        ),
                        "phase": "whole-execution",
                    }
                )
    receipt["receipt_identity"] = canonical_sha256(
        {key: value for key, value in receipt.items() if key != "receipt_identity"}
    )
    return receipt
