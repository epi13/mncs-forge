"""Typed, fail-closed execution-assurance assessments over receipt bindings.

An assessment separates what an execution environment established from any
functional result the program produced. Requested-but-unestablished properties
remain ``UNKNOWN``; identity contradictions are ``FAIL``; a functional ``PASS``
never implies assurance ``PASS``. Assessments are immutable and append-only;
conflicting assessments are retained side by side.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

from ..config import ForgeConfig
from ..errors import ForgeError
from ..mncs_native import NativeExecutionAssuranceInput, NativeForgeAdapter
from ..ports import RecordCommitter, RecordReader
from ..records import (
    ASSURANCE_REQUEST_PROPERTIES,
    ESTABLISHED_PROPERTY_KEYS,
    ExecutionAssuranceRecord,
    RecordType,
    new_record,
)
from .support import now

AssuranceStatus = Literal["PASS", "FAIL", "UNKNOWN"]


def _binding(records: RecordReader, binding_id: str) -> Mapping[str, object]:
    for entry in reversed(records.records("execution_receipt_binding")):
        payload = entry.payload
        if payload.get("binding_id") == binding_id:
            return payload
    raise ForgeError("RECORD_NOT_FOUND", f"no execution receipt binding for {binding_id}")


def _property_map(binding: Mapping[str, object]) -> Mapping[str, object] | None:
    properties = binding.get("established_properties")
    return properties if isinstance(properties, Mapping) else None


def _native_property_state(value: object) -> str:
    """Translate persisted receipt facts to the finite MNCS input vocabulary."""

    return {
        "established": "Established",
        "not-established": "NotEstablished",
        "unknown": "Unknown",
    }.get(value, "Unreadable")


def assess_execution_receipt(
    *,
    config: ForgeConfig,
    records: RecordReader,
    record_store: RecordCommitter,
    native: NativeForgeAdapter,
    binding_id: str,
    requested_properties: list[str],
    policy_identity: str | None = None,
) -> dict[str, object]:
    """Assess and persist requested execution-assurance properties fail-closed."""

    try:
        request = native.execution_assurance_request_validate(requested_properties)
    except ForgeError as error:
        if error.code != "NATIVE_ASSURANCE_INPUT":
            raise
        raise ForgeError("ASSURANCE_REQUEST", error.message) from error
    if not request.valid:
        request_messages = {
            "Empty": "requested execution-assurance properties cannot be empty",
            "Duplicate": "requested execution-assurance properties must be unique",
            "UnknownProperty": "requested properties are outside the declared vocabulary",
        }
        message = request_messages.get(request.status)
        if message is None:
            raise ForgeError(
                "NATIVE_ABI_MISMATCH", "native assurance returned an unknown request disposition"
            )
        raise ForgeError("ASSURANCE_REQUEST", message)
    binding = _binding(records, binding_id)
    if binding.get("project_identity") != config.project_identity:
        raise ForgeError(
            "RECORD_MISMATCH",
            "execution receipt binding belongs to a different project",
        )
    properties = _property_map(binding)
    if properties is None:
        raise ForgeError("RECORD_MALFORMED", "execution receipt binding has no property map")

    missing_keys = sorted(set(ESTABLISHED_PROPERTY_KEYS) - set(properties))
    complete_receipt = binding.get("receipt_completeness") == "complete"

    def state_for(key: str) -> str:
        return _native_property_state(properties.get(key))

    decision = native.execution_assurance_assess(
        NativeExecutionAssuranceInput(
            local_process_runner=binding.get("runner_kind") == "local-process",
            property_map_complete=not missing_keys,
            complete_receipt_missing=(
                complete_receipt and not isinstance(binding.get("mncs_receipt"), Mapping)
            ),
            receipt_complete=complete_receipt,
            requested_runner_capability=request.requested_runner_capability,
            runner_capability_state=state_for("runner_capability"),
            requested_filesystem_isolation=request.requested_filesystem_isolation,
            filesystem_isolation_state=state_for("filesystem_isolation"),
            requested_network_isolation=request.requested_network_isolation,
            network_isolation_state=state_for("network_isolation"),
            requested_containerization=request.requested_containerization,
            containerization_state=state_for("containerization"),
            requested_same_operator_execution=request.requested_same_operator_execution,
            same_operator_execution_state=state_for("same_operator_execution"),
        )
    )
    if not decision.valid:
        raise ForgeError(
            "NATIVE_ASSURANCE_UNKNOWN", "native execution assurance rejected the request"
        )

    reasons: list[str] = []
    if decision.property_map_incomplete:
        reasons.append("binding property map is incomplete: " + ", ".join(missing_keys))
    if decision.complete_receipt_missing:
        reasons.append("complete binding is missing its MNCS execution-receipt envelope")
    conflict_keys = {
        "FilesystemIsolation": "filesystem_isolation",
        "NetworkIsolation": "network_isolation",
        "Containerization": "containerization",
    }
    if decision.isolation_conflict != "None":
        conflicted_key = conflict_keys.get(decision.isolation_conflict)
        if conflicted_key is None:
            raise ForgeError("NATIVE_ABI_MISMATCH", "native assurance returned an unknown conflict")
        runner_kind = binding.get("runner_kind")
        reasons.append(
            f"{conflicted_key} is claimed established by a {runner_kind} runner, "
            "which cannot enforce it"
        )

    findings = {
        "runner_capability": decision.runner_capability,
        "filesystem_isolation": decision.filesystem_isolation,
        "network_isolation": decision.network_isolation,
        "containerization": decision.containerization,
        "same_operator_execution": decision.same_operator_execution,
    }
    finding_messages = {
        "NotEstablished": "requested property was not established by the runner",
        "Unknown": "requested property could not be observed",
        "Unreadable": "requested property has an unreadable state",
    }
    unmet: list[str] = []
    for key in requested_properties:
        finding = findings[key]
        if finding == "Satisfied":
            continue
        if finding == "NotRequested":
            raise ForgeError("NATIVE_ABI_MISMATCH", "native assurance omitted a requested property")
        unmet.append(key)
        message = finding_messages.get(finding)
        if message is None:
            raise ForgeError("NATIVE_ABI_MISMATCH", "native assurance returned an unknown finding")
        reasons.append(f"{message}: {key}")

    if decision.receipt_incomplete:
        reasons.append(
            "the bound execution is incomplete, so no assurance property can be confirmed"
        )
        unmet.extend(key for key in requested_properties if key not in unmet)

    status_by_native = {"PASS": "PASS", "FAIL": "FAIL", "UNKNOWN": "UNKNOWN"}
    status_value = status_by_native.get(decision.status)
    if status_value is None:
        raise ForgeError("NATIVE_ABI_MISMATCH", "native assurance returned an unknown status")
    status: AssuranceStatus = status_value  # type: ignore[assignment]

    record = new_record(
        RecordType.EXECUTION_ASSURANCE,
        {
            "project_identity": config.project_identity,
            "candidate_identity": binding["candidate_identity"],
            "binding_identity": binding_id,
            "action_kind": binding["action_kind"],
            "action_identity": binding["action_identity"],
            "requested_properties": list(requested_properties),
            "unmet_properties": unmet,
            "reasons": reasons,
            "assurance_status": status,
            "policy_identity": policy_identity,
            "assessed_at": now(),
        },
    )
    if not isinstance(record, ExecutionAssuranceRecord):
        raise ForgeError("INTERNAL_RECORD", "execution assurance assessment model is invalid")
    record_store.commit("assessments", "execution_assurance", record)
    return summarize_assessment(record)


def summarize_assessment(record: Mapping[str, object]) -> dict[str, object]:
    return {
        "assessment_id": record.get("assessment_id"),
        "binding_identity": record.get("binding_identity"),
        "candidate_identity": record.get("candidate_identity"),
        "action_kind": record.get("action_kind"),
        "requested_properties": record.get("requested_properties"),
        "unmet_properties": record.get("unmet_properties"),
        "assurance_status": record.get("assurance_status"),
        "reasons": record.get("reasons"),
        "policy_identity": record.get("policy_identity"),
        "assessed_at": record.get("assessed_at"),
        "dominance": "FAIL > UNKNOWN > PASS",
        "note": (
            "Execution assurance is separate from the functional result. A PASS result "
            "never implies assurance PASS, and an unmet requested property remains UNKNOWN."
        ),
    }


def list_assessments(
    records: RecordReader,
    *,
    binding_identity: str | None = None,
    candidate_identity: str | None = None,
) -> dict[str, object]:
    assessments: list[dict[str, object]] = []
    for entry in records.records("execution_assurance"):
        payload = entry.payload
        if binding_identity is not None and payload.get("binding_identity") != binding_identity:
            continue
        if (
            candidate_identity is not None
            and payload.get("candidate_identity") != candidate_identity
        ):
            continue
        assessments.append(summarize_assessment(payload))
    return {
        "assessments": assessments,
        "count": len(assessments),
        "vocabulary": sorted(ASSURANCE_REQUEST_PROPERTIES),
        "disagreement_policy": (
            "Assessments are append-only. Conflicting assessments are retained side by "
            "side rather than overwritten or silently resolved."
        ),
    }
