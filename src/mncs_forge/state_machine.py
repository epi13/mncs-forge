"""Deterministic lifecycle projection and transition authorization.

The ledger's typed, append-only records are the lifecycle history.  This module
does not execute providers, create records, or write storage.
"""

from __future__ import annotations

import copy
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum

from .errors import ForgeError
from .mncs_native import NativeForgeAdapter, NativeReadinessRequirement
from .records import (
    BundleRecord,
    CandidateDispositionRecord,
    CandidateRecord,
    EpochRecord,
    FinalEvaluationRecord,
    ForgeRecord,
    FreezeRecord,
    LedgerEntry,
    RecordType,
    VerifierActionRecord,
    VerifierResultRecord,
    WorkflowResultRecord,
)


class LifecycleStage(StrEnum):
    NO_EPOCH = "no_epoch"
    EPOCH_ACTIVE = "epoch_active"
    CANDIDATE_REGISTERED = "candidate_registered"
    EVIDENCE_INCOMPLETE = "evidence_incomplete"
    CANDIDATE_READY = "candidate_ready"
    CANDIDATE_SELECTED = "candidate_selected"
    CANDIDATE_REJECTED = "candidate_rejected"
    CANDIDATE_FROZEN = "candidate_frozen"
    EVALUATION_COMPLETE = "evaluation_complete"
    BUNDLE_COMPLETE = "bundle_complete"
    AMBIGUOUS_HISTORY = "ambiguous_history"


@dataclass(frozen=True, slots=True)
class TransitionBlocker:
    code: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}


@dataclass(frozen=True, slots=True)
class EvidenceReadiness:
    required: tuple[str, ...]
    present: tuple[str, ...]
    missing: tuple[str, ...]
    unknown: tuple[str, ...]
    failed: tuple[str, ...]
    stale: tuple[str, ...]
    noncomparable: tuple[str, ...]
    records: Mapping[str, tuple[str, ...]]
    ready: bool
    status: str
    policy_identity: str
    policy_error: str | None

    @property
    def blocker(self) -> TransitionBlocker | None:
        if self.policy_error is not None:
            return TransitionBlocker("EVIDENCE_PLAN_INVALID", self.policy_error)
        if self.failed:
            return TransitionBlocker(
                "EVIDENCE_FAILED",
                "required candidate evidence is FAIL: " + ", ".join(self.failed),
            )
        if self.missing:
            return TransitionBlocker(
                "EVIDENCE_INCOMPLETE",
                "required candidate evidence is missing: " + ", ".join(self.missing),
            )
        if self.unknown:
            return TransitionBlocker(
                "EVIDENCE_UNKNOWN",
                "required candidate evidence is UNKNOWN: " + ", ".join(self.unknown),
            )
        if self.stale:
            return TransitionBlocker(
                "EVIDENCE_STALE",
                "required candidate evidence is stale: " + ", ".join(self.stale),
            )
        if self.noncomparable:
            return TransitionBlocker(
                "EVIDENCE_NOT_COMPARABLE",
                "required candidate evidence is not comparable: " + ", ".join(self.noncomparable),
            )
        return None

    def to_dict(self) -> dict[str, object]:
        return {
            "required": list(self.required),
            "present": list(self.present),
            "missing": list(self.missing),
            "unknown": list(self.unknown),
            "failed": list(self.failed),
            "stale": list(self.stale),
            "noncomparable": list(self.noncomparable),
            "records": {name: list(values) for name, values in sorted(self.records.items())},
            "ready": self.ready,
            "status": self.status,
            "policy_identity": self.policy_identity,
            "policy_error": self.policy_error,
        }


@dataclass(frozen=True, slots=True)
class LifecycleProjection:
    stage: LifecycleStage
    active_epoch: EpochRecord | None
    epoch_status: str
    superseded_epochs: tuple[str, ...]
    current_candidate: CandidateRecord | None
    candidate_status: str
    candidate_freshness: str
    candidate_lineage: tuple[str, ...]
    evidence: EvidenceReadiness
    disposition: CandidateDispositionRecord | None
    disposition_status: str
    freeze: FreezeRecord | None
    freeze_status: str
    evaluations: tuple[FinalEvaluationRecord, ...]
    evaluation_status: str
    reconciliation_status: str
    bundles: tuple[BundleRecord, ...]
    bundle_status: str
    limitations: tuple[TransitionBlocker, ...]


class ForgeStateMachine:
    """Project one coherent lifecycle view and authorize prospective transitions."""

    OPERATIONS = (
        "begin_epoch",
        "register_candidate",
        "run_project_check",
        "run_candidate_check",
        "compare_candidates",
        "select_candidate",
        "reject_candidate",
        "freeze_candidate",
        "run_evaluation",
        "run_development_verifier",
        "run_evaluator_verifier",
        "reconcile_evidence",
        "build_bundle",
        "record_verifier_result",
    )

    def __init__(
        self,
        *,
        mode: str,
        history: Sequence[LedgerEntry],
        current_candidate_identity: str,
        current_authority_identities: Mapping[str, str],
        current_freeze_bindings: Mapping[str, str],
        selection_policy_identity: str,
        required_evidence: Sequence[str],
        selection_policy_error: str | None = None,
        evidence_freshness: Mapping[str, str] | None = None,
        evidence_comparability: Mapping[str, bool | None] | None = None,
        evidence_environment_keys: Mapping[str, Sequence[str]] | None = None,
        evidence_environment_identities: Mapping[str, str] | None = None,
        evidence_policy_identities: Mapping[str, str] | None = None,
        native: NativeForgeAdapter | None = None,
    ) -> None:
        if mode not in {"development", "evaluator"}:
            raise ForgeError("INVALID_MODE", "mode must be development or evaluator")
        self.mode = mode
        self.history = tuple(history)
        self.current_candidate_identity = current_candidate_identity
        self.current_authority_identities = dict(current_authority_identities)
        self.current_freeze_bindings = dict(current_freeze_bindings)
        self.selection_policy_identity = selection_policy_identity
        self.required_evidence = tuple(dict.fromkeys(required_evidence))
        self.selection_policy_error = selection_policy_error
        self.evidence_freshness = dict(evidence_freshness or {})
        self.evidence_comparability = dict(evidence_comparability or {})
        self.evidence_environment_keys = {
            name: tuple(sorted(keys)) for name, keys in (evidence_environment_keys or {}).items()
        }
        self.evidence_environment_identities = dict(evidence_environment_identities or {})
        self.evidence_policy_identities = dict(evidence_policy_identities or {})
        self.native = native
        self._native_preflight_enabled = True
        self._inspection_cache: dict[str, object] | None = None
        self._indexed = tuple(enumerate(entry.payload for entry in self.history))
        self.projection = self._project()

    def _records(self, record_type: RecordType) -> list[tuple[int, ForgeRecord]]:
        return [
            (index, record) for index, record in self._indexed if record.record_type is record_type
        ]

    @staticmethod
    def _identity(record: ForgeRecord) -> str:
        return record.identity or f"{record.record_type.value}:without-identity"

    def _epoch_projection(
        self, limitations: list[TransitionBlocker]
    ) -> tuple[EpochRecord | None, str, tuple[str, ...]]:
        epoch_records = self._records(RecordType.EPOCH)
        if not epoch_records:
            return None, "absent", ()
        epochs: list[EpochRecord] = []
        coherent = True
        previous_id: str | None = None
        known: set[str] = set()
        for _, raw in epoch_records:
            if not isinstance(raw, EpochRecord):
                continue
            epoch_id = str(raw["epoch_id"])
            parent = raw.get("parent_epoch")
            if epoch_id in known:
                coherent = False
            if previous_id is None:
                if parent is not None:
                    coherent = False
            elif parent != previous_id:
                coherent = False
            known.add(epoch_id)
            previous_id = epoch_id
            epochs.append(raw)
        if not coherent:
            limitations.append(
                TransitionBlocker(
                    "EPOCH_LINEAGE_CONFLICT",
                    "historical epoch records do not form one unbranched parent lineage",
                )
            )
        active = epochs[-1] if epochs else None
        superseded = tuple(str(item["epoch_id"]) for item in epochs[:-1])
        return active, "active" if coherent else "ambiguous", superseded

    def _candidate_projection(
        self,
        active_epoch: EpochRecord | None,
        limitations: list[TransitionBlocker],
    ) -> tuple[CandidateRecord | None, str, str, tuple[str, ...]]:
        candidate_records = [
            (index, record)
            for index, record in self._records(RecordType.CANDIDATE)
            if isinstance(record, CandidateRecord)
        ]
        candidates = [record for _, record in candidate_records]
        if active_epoch is None:
            return None, "absent", "UNKNOWN", ()
        epoch_id = str(active_epoch["epoch_id"])
        active_epoch_index = next(
            index
            for index, record in self._records(RecordType.EPOCH)
            if record.get("epoch_id") == epoch_id
        )
        if any(
            index > active_epoch_index and record.get("source_epoch") != epoch_id
            for index, record in candidate_records
        ):
            limitations.append(
                TransitionBlocker(
                    "EPOCH_SUPERSEDED",
                    "historical candidate was recorded against a superseded epoch",
                )
            )
        active_candidates = [item for item in candidates if item.get("source_epoch") == epoch_id]
        known = {str(item["candidate_id"]): item for item in candidates}
        coherent = True
        previous_id: str | None = None
        for item in active_candidates:
            candidate_id = str(item["candidate_id"])
            parent = item.get("parent_candidate")
            if parent == candidate_id:
                coherent = False
            if previous_id is None:
                if parent is not None:
                    coherent = False
            elif parent != previous_id:
                coherent = False
            if parent is not None:
                parent_record = known.get(str(parent))
                if parent_record is None or parent_record.get("source_epoch") != epoch_id:
                    coherent = False
            previous_id = candidate_id
        if not coherent:
            limitations.append(
                TransitionBlocker(
                    "CANDIDATE_LINEAGE_CONFLICT",
                    "historical candidates do not form one active-epoch lineage",
                )
            )
        current = active_candidates[-1] if active_candidates else None
        if current is None:
            return None, "absent", "UNKNOWN", ()
        freshness = (
            "CURRENT" if current["candidate_id"] == self.current_candidate_identity else "STALE"
        )
        lineage: list[str] = []
        seen: set[str] = set()
        cursor: CandidateRecord | None = current
        while cursor is not None:
            candidate_id = str(cursor["candidate_id"])
            if candidate_id in seen:
                coherent = False
                limitations.append(
                    TransitionBlocker(
                        "CANDIDATE_LINEAGE_CONFLICT",
                        "historical candidate parentage contains a cycle",
                    )
                )
                break
            seen.add(candidate_id)
            lineage.append(candidate_id)
            parent = cursor.get("parent_candidate")
            parent_record = known.get(str(parent)) if parent is not None else None
            cursor = parent_record if isinstance(parent_record, CandidateRecord) else None
        return current, "current" if coherent else "ambiguous", freshness, tuple(reversed(lineage))

    def _evidence_readiness(
        self,
        candidate: CandidateRecord | None,
        *,
        required: Sequence[str] | None = None,
    ) -> EvidenceReadiness:
        """Project evidence readiness through MNCS when the native spine is selected."""

        if self.native is None:
            return self._compat_evidence_readiness(candidate, required=required)
        project_native = getattr(self.native, "readiness_projection", None)
        if not callable(project_native):
            return self._compat_evidence_readiness(candidate, required=required)
        requirements = (
            tuple(dict.fromkeys(required)) if required is not None else self.required_evidence
        )
        policy_error = None if required is not None else self.selection_policy_error
        candidate_id = str(candidate["candidate_id"]) if candidate is not None else None
        evidence: dict[str, list[ForgeRecord]] = defaultdict(list)
        for _, record in self._records(RecordType.WORKFLOW_RESULT):
            if (
                isinstance(record, WorkflowResultRecord)
                and candidate_id is not None
                and record.get("candidate_identity") == candidate_id
                and record.get("subject_type") != "project"
            ):
                evidence[str(record["workflow"])].append(record)
        for _, record in self._records(RecordType.VERIFIER_RESULT):
            if (
                isinstance(record, VerifierResultRecord)
                and candidate_id is not None
                and record.get("candidate_identity") == candidate_id
                and record.get("mode") == "development"
            ):
                evidence[str(record["verifier_id"])].append(record)

        candidate_epoch = (
            next(
                (
                    record
                    for _, record in reversed(self._records(RecordType.EPOCH))
                    if record.get("epoch_id") == candidate.get("source_epoch")
                ),
                None,
            )
            if candidate is not None
            else None
        )
        authority_match = True
        if candidate_epoch is not None and self.current_authority_identities:
            authority = candidate_epoch.get("authority_identities")
            authority_match = (
                isinstance(authority, Mapping)
                and dict(authority) == self.current_authority_identities
            )

        normalized: dict[str, dict[str, object]] = {}
        identities: dict[str, tuple[str, ...]] = {}
        for requirement in requirements:
            matches = evidence.get(requirement, [])
            identities[requirement] = tuple(self._identity(item) for item in matches)
            freshness = "CURRENT"
            comparable = True
            environment_match = True
            policy_match = True
            for evidence_record in matches:
                output_identity = self._identity(evidence_record)
                input_identities = evidence_record.get("input_identities")
                verifier_candidate_current = (
                    evidence_record.record_type is RecordType.VERIFIER_RESULT
                    and isinstance(input_identities, Mapping)
                    and input_identities.get("candidate_identity")
                    == self.current_candidate_identity
                )
                observed_freshness = self.evidence_freshness.get(
                    output_identity,
                    "CURRENT"
                    if candidate_id == self.current_candidate_identity
                    and (
                        evidence_record.record_type is RecordType.WORKFLOW_RESULT
                        or verifier_candidate_current
                    )
                    else "UNKNOWN",
                )
                if observed_freshness == "STALE":
                    freshness = "STALE"
                elif observed_freshness != "CURRENT" and freshness != "STALE":
                    freshness = "UNKNOWN"
                comparable = (
                    comparable and self.evidence_comparability.get(output_identity, True) is True
                )
                if evidence_record.record_type is RecordType.WORKFLOW_RESULT:
                    expected_keys = self.evidence_environment_keys.get(requirement)
                    recorded_environment = evidence_record.get("environment")
                    recorded_keys = (
                        recorded_environment.get("allowlisted_keys")
                        if isinstance(recorded_environment, Mapping)
                        else None
                    )
                    environment_match = environment_match and (
                        expected_keys is None
                        or (
                            isinstance(recorded_keys, Sequence)
                            and not isinstance(recorded_keys, (str, bytes))
                            and tuple(sorted(str(item) for item in recorded_keys)) == expected_keys
                        )
                    )
                elif evidence_record.record_type is RecordType.VERIFIER_RESULT:
                    expected_environment = self.evidence_environment_identities.get(requirement)
                    expected_policy = self.evidence_policy_identities.get(requirement)
                    environment_match = environment_match and (
                        expected_environment is None
                        or evidence_record.get("environment_identity") == expected_environment
                    )
                    policy_match = policy_match and (
                        expected_policy is None
                        or evidence_record.get("policy_identity") == expected_policy
                    )
            normalized[requirement] = {
                "records": matches,
                "freshness": freshness,
                "comparable": comparable,
                "environment_match": environment_match,
                "policy_match": policy_match,
                "authority_match": authority_match,
            }
        native = project_native(
            normalized,
            candidate_present=candidate is not None,
            policy_valid=policy_error is None,
        )
        if not native.valid or len(native.requirements) != len(requirements):
            raise ForgeError("NATIVE_READINESS_UNKNOWN", "native readiness projection is invalid")
        present: list[str] = []
        missing: list[str] = []
        unknown: list[str] = []
        failed: list[str] = []
        stale: list[str] = []
        noncomparable: list[str] = []
        classes = {
            "Present": present,
            "Missing": missing,
            "Unknown": unknown,
            "Failed": failed,
            "Stale": stale,
            "NonComparable": noncomparable,
        }
        projected_by_identity: dict[bytes, NativeReadinessRequirement] = {}
        for projected in native.requirements:
            if projected.identity in projected_by_identity:
                raise ForgeError(
                    "NATIVE_READINESS_MISMATCH",
                    "native readiness returned duplicate requirement identities",
                )
            projected_by_identity[projected.identity] = projected
        if len(projected_by_identity) != len(requirements):
            raise ForgeError(
                "NATIVE_READINESS_MISMATCH",
                "native readiness returned an unexpected requirement count",
            )
        for requirement in requirements:
            identity = NativeForgeAdapter.readiness_requirement_identity(requirement)
            projected = projected_by_identity.get(identity)
            if projected is None:
                raise ForgeError(
                    "NATIVE_READINESS_MISMATCH",
                    f"native readiness omitted requirement {requirement}",
                )
            # ``present`` is the historical host-facing meaning: an
            # observation exists, regardless of whether its native
            # classification is PASS, FAIL, UNKNOWN, or stale.  Keep that
            # envelope separate from the language-owned classification so
            # stage selection remains equivalent to the compatibility path.
            if projected.observed_count > 0:
                present.append(requirement)
            try:
                if projected.classification != "Present":
                    classes[projected.classification].append(requirement)
            except KeyError as exc:
                raise ForgeError(
                    "NATIVE_READINESS_UNKNOWN",
                    "native readiness returned an unknown classification",
                ) from exc
            if projected.stale:
                stale.append(requirement)
            if projected.noncomparable:
                noncomparable.append(requirement)
        return EvidenceReadiness(
            required=requirements,
            present=tuple(sorted(present)),
            missing=tuple(sorted(missing)),
            unknown=tuple(sorted(unknown)),
            failed=tuple(sorted(failed)),
            stale=tuple(sorted(set(stale))),
            noncomparable=tuple(sorted(set(noncomparable))),
            records=identities if candidate is not None else {},
            ready=native.ready,
            status=native.status,
            policy_identity=self.selection_policy_identity,
            policy_error=policy_error,
        )

    def _compat_evidence_readiness(
        self,
        candidate: CandidateRecord | None,
        *,
        required: Sequence[str] | None = None,
    ) -> EvidenceReadiness:
        requirements = (
            tuple(dict.fromkeys(required)) if required is not None else self.required_evidence
        )
        policy_error = None if required is not None else self.selection_policy_error
        if candidate is None:
            return EvidenceReadiness(
                required=requirements,
                present=(),
                missing=requirements,
                unknown=(),
                failed=(),
                stale=(),
                noncomparable=(),
                records={},
                ready=False,
                status="UNKNOWN",
                policy_identity=self.selection_policy_identity,
                policy_error=policy_error,
            )
        candidate_id = str(candidate["candidate_id"])
        evidence: dict[str, list[ForgeRecord]] = defaultdict(list)
        for _, record in self._records(RecordType.WORKFLOW_RESULT):
            if (
                isinstance(record, WorkflowResultRecord)
                and record.get("candidate_identity") == candidate_id
                and record.get("subject_type") != "project"
            ):
                evidence[str(record["workflow"])].append(record)
        for _, record in self._records(RecordType.VERIFIER_RESULT):
            if (
                isinstance(record, VerifierResultRecord)
                and record.get("candidate_identity") == candidate_id
                and record.get("mode") == "development"
            ):
                evidence[str(record["verifier_id"])].append(record)
        present: list[str] = []
        missing: list[str] = []
        unknown: list[str] = []
        failed: list[str] = []
        stale: list[str] = []
        noncomparable: list[str] = []
        identities: dict[str, tuple[str, ...]] = {}
        for requirement in requirements:
            matches = evidence.get(requirement, [])
            identities[requirement] = tuple(self._identity(item) for item in matches)
            if not matches:
                missing.append(requirement)
                continue
            present.append(requirement)
            statuses = {str(item.status or "UNKNOWN") for item in matches}
            if "FAIL" in statuses:
                failed.append(requirement)
            elif "UNKNOWN" in statuses:
                unknown.append(requirement)
            for evidence_record in matches:
                output_identity = self._identity(evidence_record)
                input_identities = evidence_record.get("input_identities")
                verifier_candidate_current = (
                    evidence_record.record_type is RecordType.VERIFIER_RESULT
                    and isinstance(input_identities, Mapping)
                    and input_identities.get("candidate_identity")
                    == self.current_candidate_identity
                )
                freshness = self.evidence_freshness.get(
                    output_identity,
                    "CURRENT"
                    if candidate_id == self.current_candidate_identity
                    and (
                        evidence_record.record_type is RecordType.WORKFLOW_RESULT
                        or verifier_candidate_current
                    )
                    else "UNKNOWN",
                )
                if freshness == "STALE":
                    stale.append(requirement)
                elif freshness != "CURRENT":
                    noncomparable.append(requirement)
                comparable = self.evidence_comparability.get(output_identity, True)
                if comparable is not True:
                    noncomparable.append(requirement)
                if evidence_record.record_type is RecordType.WORKFLOW_RESULT:
                    expected_keys = self.evidence_environment_keys.get(requirement)
                    recorded_environment = evidence_record.get("environment")
                    recorded_keys = (
                        recorded_environment.get("allowlisted_keys")
                        if isinstance(recorded_environment, Mapping)
                        else None
                    )
                    if expected_keys is not None and (
                        not isinstance(recorded_keys, Sequence)
                        or isinstance(recorded_keys, (str, bytes))
                        or tuple(sorted(str(item) for item in recorded_keys)) != expected_keys
                    ):
                        noncomparable.append(requirement)
                elif evidence_record.record_type is RecordType.VERIFIER_RESULT:
                    expected_environment = self.evidence_environment_identities.get(requirement)
                    expected_policy = self.evidence_policy_identities.get(requirement)
                    if (
                        expected_environment is not None
                        and evidence_record.get("environment_identity") != expected_environment
                    ) or (
                        expected_policy is not None
                        and evidence_record.get("policy_identity") != expected_policy
                    ):
                        noncomparable.append(requirement)
        candidate_epoch = next(
            (
                record
                for _, record in reversed(self._records(RecordType.EPOCH))
                if record.get("epoch_id") == candidate.get("source_epoch")
            ),
            None,
        )
        if candidate_epoch is not None and self.current_authority_identities:
            authority = candidate_epoch.get("authority_identities")
            if (
                not isinstance(authority, Mapping)
                or dict(authority) != self.current_authority_identities
            ):
                noncomparable.extend(present)
        missing = sorted(set(missing))
        unknown = sorted(set(unknown))
        failed = sorted(set(failed))
        stale = sorted(set(stale))
        noncomparable = sorted(set(noncomparable))
        ready = not (policy_error or missing or unknown or failed or stale or noncomparable)
        status = "FAIL" if failed else "PASS" if ready else "UNKNOWN"
        return EvidenceReadiness(
            required=requirements,
            present=tuple(sorted(present)),
            missing=tuple(missing),
            unknown=tuple(unknown),
            failed=tuple(failed),
            stale=tuple(stale),
            noncomparable=tuple(noncomparable),
            records=identities,
            ready=ready,
            status=status,
            policy_identity=self.selection_policy_identity,
            policy_error=policy_error,
        )

    def _native_history_events(self) -> list[dict[str, object]]:
        """Reduce typed ledger records to the native lifecycle event ABI."""

        events: list[dict[str, object]] = []
        for entry in self.history:
            record = entry.payload
            if isinstance(record, EpochRecord):
                events.append(
                    {
                        "kind": "EpochStarted",
                        "epoch": str(record["epoch_id"]),
                        "parent_epoch": record.get("parent_epoch"),
                    }
                )
            elif isinstance(record, CandidateRecord):
                events.append(
                    {
                        "kind": "CandidateRegistered",
                        "epoch": str(record["source_epoch"]),
                        "candidate": str(record["candidate_id"]),
                        "parent_candidate": record.get("parent_candidate"),
                    }
                )
            elif isinstance(record, WorkflowResultRecord):
                if (
                    record.get("candidate_identity") is not None
                    and record.get("subject_type") != "project"
                    and record.get("workflow") in self.required_evidence
                ):
                    events.append(
                        {
                            "kind": "EvidenceObserved",
                            "candidate": str(record["candidate_identity"]),
                            "status": str(record.get("status") or "UNKNOWN"),
                        }
                    )
            elif isinstance(record, VerifierResultRecord):
                if (
                    record.get("candidate_identity") is not None
                    and record.get("mode") == "development"
                    and record.get("verifier_id") in self.required_evidence
                ):
                    events.append(
                        {
                            "kind": "EvidenceObserved",
                            "candidate": str(record["candidate_identity"]),
                            "status": str(record.get("status") or "UNKNOWN"),
                        }
                    )
            elif isinstance(record, CandidateDispositionRecord):
                disposition = str(record.get("disposition") or "")
                if disposition in {"selected", "rejected"}:
                    events.append(
                        {
                            "kind": "CandidateSelected"
                            if disposition == "selected"
                            else "CandidateRejected",
                            "candidate": str(record["candidate_identity"]),
                        }
                    )
            elif isinstance(record, FreezeRecord):
                events.append(
                    {
                        "kind": "CandidateFrozen",
                        "candidate": str(record["candidate_identity"]),
                    }
                )
            elif (
                isinstance(record, FinalEvaluationRecord)
                and record.get("candidate_identity") is not None
            ):
                events.append(
                    {
                        "kind": "EvaluationRecorded",
                        "candidate": str(record["candidate_identity"]),
                        "status": str(record.get("status") or "UNKNOWN"),
                    }
                )
        return events

    @staticmethod
    def _identity_digest(identity: object, *, context: str) -> bytes:
        if identity is None or identity == "":
            return bytes(32)
        if not isinstance(identity, str):
            raise ForgeError("NATIVE_LIFECYCLE_UNKNOWN", f"{context} is not a digest identity")
        try:
            digest = bytes.fromhex(identity.rsplit(":", 1)[-1])
        except ValueError as exc:
            raise ForgeError(
                "NATIVE_LIFECYCLE_UNKNOWN", f"{context} is not a digest identity"
            ) from exc
        if len(digest) != 32:
            raise ForgeError("NATIVE_LIFECYCLE_UNKNOWN", f"{context} is not a 32-byte digest")
        return digest

    def _apply_native_projection(self, projection: LifecycleProjection) -> LifecycleProjection:
        if self.native is None:
            return projection
        project_native = getattr(self.native, "lifecycle_projection", None)
        if not callable(project_native):
            return projection
        native = project_native(
            self._native_history_events(),
            current_candidate=self.current_candidate_identity,
            required_evidence=len(self.required_evidence),
        )
        active_identity = (
            str(projection.active_epoch["epoch_id"])
            if projection.active_epoch is not None
            else None
        )
        candidate_identity = (
            str(projection.current_candidate["candidate_id"])
            if projection.current_candidate is not None
            else None
        )
        if active_identity is not None and native.active_epoch != self._identity_digest(
            active_identity, context="native active epoch"
        ):
            raise ForgeError(
                "NATIVE_LIFECYCLE_MISMATCH",
                "native lifecycle projection disagrees with the active epoch identity",
            )
        if candidate_identity is not None and native.current_candidate != self._identity_digest(
            candidate_identity, context="native current candidate"
        ):
            raise ForgeError(
                "NATIVE_LIFECYCLE_MISMATCH",
                "native lifecycle projection disagrees with the current candidate identity",
            )
        if native.parent_epoch != bytes(32) and projection.active_epoch is not None:
            parent_identity = projection.active_epoch.get("parent_epoch")
            if native.parent_epoch != self._identity_digest(
                parent_identity, context="native parent epoch"
            ):
                raise ForgeError(
                    "NATIVE_LIFECYCLE_MISMATCH",
                    "native lifecycle projection disagrees with epoch parentage",
                )
        if native.parent_candidate != bytes(32) and projection.current_candidate is not None:
            parent_identity = projection.current_candidate.get("parent_candidate")
            if native.parent_candidate != self._identity_digest(
                parent_identity, context="native parent candidate"
            ):
                raise ForgeError(
                    "NATIVE_LIFECYCLE_MISMATCH",
                    "native lifecycle projection disagrees with candidate parentage",
                )
        if not native.lineage_ok:
            limitations = list(projection.limitations)
            if not any(item.code == "NATIVE_LINEAGE_CONFLICT" for item in limitations):
                limitations.append(
                    TransitionBlocker(
                        "NATIVE_LINEAGE_CONFLICT",
                        "MNCS lifecycle projection found an invalid parent relationship",
                    )
                )
            return replace(
                projection,
                stage=LifecycleStage.AMBIGUOUS_HISTORY,
                candidate_status="ambiguous",
                candidate_freshness=native.freshness.upper(),
                limitations=tuple(limitations),
                reconciliation_status="unknown",
            )
        if projection.bundles:
            stage = projection.stage
        else:
            stage = LifecycleStage(
                {
                    "NoEpoch": LifecycleStage.NO_EPOCH,
                    "EpochActive": LifecycleStage.EPOCH_ACTIVE,
                    "CandidateRegistered": LifecycleStage.CANDIDATE_REGISTERED,
                    "EvidenceIncomplete": LifecycleStage.EVIDENCE_INCOMPLETE,
                    "CandidateReady": LifecycleStage.CANDIDATE_READY,
                    "CandidateSelected": LifecycleStage.CANDIDATE_SELECTED,
                    "CandidateRejected": LifecycleStage.CANDIDATE_REJECTED,
                    "CandidateFrozen": LifecycleStage.CANDIDATE_FROZEN,
                    "EvaluationComplete": LifecycleStage.EVALUATION_COMPLETE,
                    "AmbiguousHistory": LifecycleStage.AMBIGUOUS_HISTORY,
                }[native.stage]
            )
        native_freshness = {
            "NotApplicable": "NOT_APPLICABLE",
            "Current": "CURRENT",
            "Stale": "STALE",
            "Unknown": "UNKNOWN",
        }[native.freshness]
        native_disposition = {
            "Undisposed": "undisposed",
            "Selected": "selected",
            "Rejected": "rejected",
            "Conflict": "conflict",
        }[native.disposition]
        return replace(
            projection,
            stage=stage,
            candidate_status=(
                projection.candidate_status
                if stage is not LifecycleStage.AMBIGUOUS_HISTORY
                else "ambiguous"
            ),
            candidate_freshness=native_freshness,
            disposition_status=native_disposition,
            reconciliation_status=(
                "unknown"
                if stage is LifecycleStage.AMBIGUOUS_HISTORY
                else projection.reconciliation_status
            ),
        )

    def _project(self) -> LifecycleProjection:
        limitations: list[TransitionBlocker] = []
        active_epoch, epoch_status, superseded = self._epoch_projection(limitations)
        candidate, candidate_status, freshness, lineage = self._candidate_projection(
            active_epoch, limitations
        )
        evidence = self._evidence_readiness(candidate)
        if evidence.present:
            limitations.append(
                TransitionBlocker(
                    "EVIDENCE_COMPARABILITY_LIMITED",
                    "workflow evidence compares recorded candidate, workflow, and environment-key "
                    "bindings; environment values are not disclosed",
                )
            )
        candidate_id = str(candidate["candidate_id"]) if candidate is not None else None
        dispositions = [
            record
            for _, record in self._records(RecordType.CANDIDATE_DISPOSITION)
            if isinstance(record, CandidateDispositionRecord)
            and record.get("candidate_identity") == candidate_id
        ]
        disposition = dispositions[-1] if len(dispositions) == 1 else None
        if len(dispositions) > 1:
            disposition_status = "conflict"
            limitations.append(
                TransitionBlocker(
                    "CANDIDATE_DISPOSITION_CONFLICT",
                    "historical candidate has multiple terminal dispositions",
                )
            )
        elif disposition is None:
            disposition_status = "undisposed"
        else:
            disposition_status = str(disposition["disposition"])
        freezes = [
            record
            for _, record in self._records(RecordType.FREEZE)
            if isinstance(record, FreezeRecord) and record.get("candidate_identity") == candidate_id
        ]
        freeze = freezes[-1] if len(freezes) == 1 else None
        if len(freezes) > 1:
            freeze_status = "conflict"
            limitations.append(
                TransitionBlocker(
                    "FREEZE_CONFLICT", "historical candidate has multiple freeze records"
                )
            )
        elif freeze is None:
            freeze_status = "absent"
        else:
            drift = self._freeze_drift(freeze)
            freeze_status = "current" if not drift else "drifted"
        evaluations = tuple(
            record
            for _, record in self._records(RecordType.FINAL_EVALUATION)
            if isinstance(record, FinalEvaluationRecord)
            and record.get("candidate_identity") == candidate_id
        )
        bundles = tuple(
            record
            for _, record in self._records(RecordType.BUNDLE)
            if isinstance(record, BundleRecord) and record.get("candidate_identity") == candidate_id
        )
        evaluation_status = "complete" if evaluations else "not_started"
        bundle_status = "complete" if bundles else "not_started"
        if limitations and any(
            item.code
            in {
                "EPOCH_LINEAGE_CONFLICT",
                "EPOCH_SUPERSEDED",
                "CANDIDATE_LINEAGE_CONFLICT",
                "CANDIDATE_DISPOSITION_CONFLICT",
                "FREEZE_CONFLICT",
            }
            for item in limitations
        ):
            stage = LifecycleStage.AMBIGUOUS_HISTORY
        elif active_epoch is None:
            stage = LifecycleStage.NO_EPOCH
        elif candidate is None:
            stage = LifecycleStage.EPOCH_ACTIVE
        elif bundles:
            stage = LifecycleStage.BUNDLE_COMPLETE
        elif evaluations:
            stage = LifecycleStage.EVALUATION_COMPLETE
        elif freeze is not None:
            stage = LifecycleStage.CANDIDATE_FROZEN
        elif disposition_status == "selected":
            stage = LifecycleStage.CANDIDATE_SELECTED
        elif disposition_status == "rejected":
            stage = LifecycleStage.CANDIDATE_REJECTED
        elif evidence.ready:
            stage = LifecycleStage.CANDIDATE_READY
        elif evidence.present:
            stage = LifecycleStage.EVIDENCE_INCOMPLETE
        else:
            stage = LifecycleStage.CANDIDATE_REGISTERED
        reconciliation_status = (
            "unknown" if stage is LifecycleStage.AMBIGUOUS_HISTORY else "derived_on_request"
        )
        projection = LifecycleProjection(
            stage=stage,
            active_epoch=active_epoch,
            epoch_status=epoch_status,
            superseded_epochs=superseded,
            current_candidate=candidate,
            candidate_status=candidate_status,
            candidate_freshness=freshness,
            candidate_lineage=lineage,
            evidence=evidence,
            disposition=disposition,
            disposition_status=disposition_status,
            freeze=freeze,
            freeze_status=freeze_status,
            evaluations=evaluations,
            evaluation_status=evaluation_status,
            reconciliation_status=reconciliation_status,
            bundles=bundles,
            bundle_status=bundle_status,
            limitations=tuple(limitations),
        )
        return self._apply_native_projection(projection)

    def _require_mode(self, expected: str) -> None:
        if self.mode != expected:
            raise ForgeError(
                "MODE_FORBIDDEN",
                f"operation requires {expected} mode; current mode is {self.mode}",
            )

    def _native_transition(
        self,
        *,
        operation: str,
        next_stage: str,
        status: str,
        reason: int,
        evidence: str = "UNKNOWN",
    ) -> None:
        """Require the covered transition to agree with the MNCS lifecycle kernel.

        The Python projection remains responsible for Forge-specific history,
        evidence, identity, and authority rules. The native call is a narrow
        semantic gate for the transitions represented by the MNCS source. A
        missing adapter is the explicit compatibility path; once an adapter is
        selected, an execution or response problem is fail-closed.
        """

        if self.native is None or not self._native_preflight_enabled:
            return
        stage_names = {
            LifecycleStage.NO_EPOCH: "NoEpoch",
            LifecycleStage.EPOCH_ACTIVE: "EpochActive",
            LifecycleStage.CANDIDATE_REGISTERED: "CandidateRegistered",
            LifecycleStage.EVIDENCE_INCOMPLETE: "EvidenceIncomplete",
            LifecycleStage.CANDIDATE_READY: "CandidateReady",
            LifecycleStage.CANDIDATE_SELECTED: "CandidateSelected",
            LifecycleStage.CANDIDATE_REJECTED: "CandidateRejected",
            LifecycleStage.CANDIDATE_FROZEN: "CandidateFrozen",
            LifecycleStage.EVALUATION_COMPLETE: "EvaluationComplete",
            LifecycleStage.AMBIGUOUS_HISTORY: "AmbiguousHistory",
        }
        stage = stage_names.get(self.projection.stage)
        if stage is None:
            return
        try:
            observed = self.native.lifecycle_preflight(stage, operation, evidence)
        except ForgeError as exc:
            raise ForgeError(
                "NATIVE_LIFECYCLE_UNKNOWN",
                f"MNCS lifecycle preflight is unavailable: {exc.message}",
            ) from exc
        if (
            observed.next_stage != next_stage
            or observed.status != status
            or observed.reason != reason
        ):
            raise ForgeError(
                "NATIVE_LIFECYCLE_MISMATCH",
                "MNCS lifecycle preflight disagrees with the Forge transition "
                f"({stage} + {operation} -> {observed.next_stage}/{observed.status}/"
                f"{observed.reason}, expected {next_stage}/{status}/{reason})",
            )

    def _require_unambiguous(self) -> None:
        if self.projection.stage is LifecycleStage.AMBIGUOUS_HISTORY:
            blocker = self.projection.limitations[0]
            raise ForgeError(blocker.code, blocker.message)

    def _require_candidate(self, candidate_id: str | None = None) -> CandidateRecord:
        candidate = self.projection.current_candidate
        if candidate is None:
            raise ForgeError("NO_CANDIDATE", "no candidate exists in the active epoch")
        if candidate_id is not None and candidate["candidate_id"] != candidate_id:
            known = self.candidate_record(candidate_id)
            if known is not None:
                raise ForgeError(
                    "CANDIDATE_NOT_CURRENT",
                    "candidate is superseded within the active development lineage",
                )
            raise ForgeError("RECORD_NOT_FOUND", f"no candidate record for {candidate_id}")
        if self.projection.candidate_freshness != "CURRENT":
            raise ForgeError("STALE_CANDIDATE", "candidate no longer matches current content")
        return candidate

    def candidate_record(self, candidate_id: str) -> CandidateRecord | None:
        for _, record in reversed(self._records(RecordType.CANDIDATE)):
            if isinstance(record, CandidateRecord) and record.get("candidate_id") == candidate_id:
                return record
        return None

    def authorize_epoch_begin(self, parent_epoch: str | None) -> None:
        self._require_mode("development")
        self._require_unambiguous()
        active = self.projection.active_epoch
        if active is None:
            if parent_epoch is not None:
                raise ForgeError("EPOCH_PARENT_INVALID", "the first epoch cannot name a parent")
            self._native_transition(
                operation="BeginEpoch",
                next_stage="EpochActive",
                status="PASS",
                reason=0,
            )
            return
        active_id = str(active["epoch_id"])
        if parent_epoch is None:
            raise ForgeError(
                "EPOCH_PARENT_REQUIRED", "a successor epoch must name the active epoch as parent"
            )
        if parent_epoch in self.projection.superseded_epochs:
            raise ForgeError("EPOCH_SUPERSEDED", "parent epoch has already been superseded")
        if parent_epoch != active_id:
            raise ForgeError("EPOCH_PARENT_INVALID", "parent epoch is not the active epoch")

    def authorize_candidate_register(
        self,
        *,
        parent_candidate: str | None,
        proposed_identity: str,
        epoch_identity: str | None = None,
    ) -> EpochRecord:
        self._require_mode("development")
        self._require_unambiguous()
        epoch = self.projection.active_epoch
        if epoch is None:
            raise ForgeError("NO_ACTIVE_EPOCH", "begin an epoch before registering a candidate")
        active_epoch_id = str(epoch["epoch_id"])
        if epoch_identity in self.projection.superseded_epochs:
            raise ForgeError("EPOCH_SUPERSEDED", "candidate epoch has been superseded")
        if epoch_identity is not None and epoch_identity != active_epoch_id:
            raise ForgeError("EPOCH_PARENT_INVALID", "candidate epoch is not the active epoch")
        authority_identities = epoch["authority_identities"]
        if not isinstance(authority_identities, Mapping):
            raise ForgeError("EPOCH_LINEAGE_CONFLICT", "epoch authority identities are malformed")
        if dict(authority_identities) != self.current_authority_identities:
            raise ForgeError("STALE_BASELINE", "protected authority drifted since the epoch began")
        if self.projection.freeze is not None:
            raise ForgeError("EPOCH_FROZEN", "begin a successor epoch after candidate freeze")
        current = self.projection.current_candidate
        if current is None:
            if parent_candidate is not None:
                parent = self.candidate_record(parent_candidate)
                if parent is None:
                    raise ForgeError("CANDIDATE_PARENT_INVALID", "parent candidate does not exist")
                raise ForgeError(
                    "CANDIDATE_LINEAGE_CONFLICT",
                    "first candidate in an epoch cannot inherit from another epoch",
                )
            self._native_transition(
                operation="RegisterCandidate",
                next_stage="CandidateRegistered",
                status="UNKNOWN",
                reason=0,
            )
        else:
            current_id = str(current["candidate_id"])
            if parent_candidate is None:
                raise ForgeError(
                    "CANDIDATE_PARENT_REQUIRED",
                    "a successor candidate must name the current candidate as parent",
                )
            if parent_candidate == proposed_identity:
                raise ForgeError(
                    "CANDIDATE_PARENT_INVALID", "candidate cannot name itself as parent"
                )
            if parent_candidate != current_id:
                parent = self.candidate_record(parent_candidate)
                if parent is None:
                    raise ForgeError("CANDIDATE_PARENT_INVALID", "parent candidate does not exist")
                raise ForgeError(
                    "CANDIDATE_LINEAGE_CONFLICT",
                    "parent candidate is not current in the active epoch lineage",
                )
        return epoch

    def authorize_development_work(
        self, candidate_id: str | None, *, project_scoped: bool
    ) -> CandidateRecord | None:
        self._require_mode("development")
        if project_scoped:
            if candidate_id is not None:
                raise ForgeError("WORKFLOW_SUBJECT", "project workflow does not accept a candidate")
            return None
        self._require_unambiguous()
        if self.projection.freeze is not None:
            raise ForgeError(
                "EPOCH_FROZEN", "candidate-scoped development work cannot continue after freeze"
            )
        return self._require_candidate(candidate_id)

    def authorize_candidate_comparison(self, candidate_ids: Sequence[str]) -> None:
        self._require_mode("development")
        self._require_unambiguous()
        active = self.projection.active_epoch
        if active is None:
            raise ForgeError("NO_ACTIVE_EPOCH", "candidate comparison requires an active epoch")
        active_id = str(active["epoch_id"])
        for candidate_id in candidate_ids:
            candidate = self.candidate_record(candidate_id)
            if candidate is None:
                raise ForgeError("RECORD_NOT_FOUND", f"no candidate record for {candidate_id}")
            if candidate.get("source_epoch") != active_id:
                raise ForgeError(
                    "CANDIDATE_LINEAGE_CONFLICT",
                    "compared candidates must belong to the active epoch lineage",
                )

    def authorize_candidate_disposition(
        self, candidate_id: str, disposition: str
    ) -> tuple[CandidateRecord, EvidenceReadiness]:
        self._require_mode("development")
        self._require_unambiguous()
        if disposition not in {"selected", "rejected"}:
            raise ForgeError("INVALID_DISPOSITION", "disposition must be selected or rejected")
        candidate = self._require_candidate(candidate_id)
        if self.projection.disposition_status != "undisposed":
            raise ForgeError(
                "CANDIDATE_ALREADY_DISPOSED",
                f"candidate already has terminal disposition {self.projection.disposition_status}",
            )
        if disposition == "selected":
            blocker = self.projection.evidence.blocker
            if blocker is not None:
                raise ForgeError(blocker.code, blocker.message)
            self._native_transition(
                operation="SelectCandidate",
                next_stage="CandidateSelected",
                status="PASS",
                reason=0,
                evidence="PASS",
            )
        else:
            self._native_transition(
                operation="RejectCandidate",
                next_stage="CandidateRejected",
                status="FAIL",
                reason=9,
                evidence=self.projection.evidence.status,
            )
        return candidate, self.projection.evidence

    def authorize_candidate_freeze(
        self,
        candidate_id: str,
        *,
        evidence_plan_requirements: Sequence[str] | None,
    ) -> tuple[CandidateRecord, CandidateDispositionRecord]:
        self._require_mode("development")
        self._require_unambiguous()
        candidate = self._require_candidate(candidate_id)
        active_epoch = self.projection.active_epoch
        if active_epoch is None:
            raise ForgeError("NO_ACTIVE_EPOCH", "freeze requires an active epoch")
        authority_identities = active_epoch["authority_identities"]
        if (
            not isinstance(authority_identities, Mapping)
            or dict(authority_identities) != self.current_authority_identities
        ):
            raise ForgeError(
                "FREEZE_AUTHORITY_STALE",
                "protected authority differs from the active epoch at freeze time",
            )
        if self.projection.disposition_status == "rejected":
            raise ForgeError("CANDIDATE_REJECTED", "a rejected candidate cannot be frozen")
        if self.projection.disposition_status != "selected" or self.projection.disposition is None:
            raise ForgeError("FREEZE_NOT_SELECTED", "candidate is not currently selected")
        if self.projection.freeze is not None:
            raise ForgeError("FREEZE_ALREADY_EXISTS", "candidate already has a freeze record")
        blocker = self.projection.evidence.blocker
        if blocker is not None:
            raise ForgeError(blocker.code, blocker.message)
        if (
            self.projection.disposition.get("selection_policy_identity")
            != self.selection_policy_identity
        ):
            raise ForgeError(
                "FREEZE_SELECTION_STALE", "selection policy changed after candidate selection"
            )
        if self.projection.disposition.get("evidence_status") != "PASS":
            raise ForgeError("FREEZE_SELECTION_STALE", "selection does not bind PASS evidence")
        if not evidence_plan_requirements:
            raise ForgeError(
                "FREEZE_EVIDENCE_PLAN_INVALID", "required evidence plan is missing or malformed"
            )
        plan_blocker = self._evidence_readiness(
            candidate, required=evidence_plan_requirements
        ).blocker
        if plan_blocker is not None:
            raise ForgeError(plan_blocker.code, plan_blocker.message)
        self._native_transition(
            operation="FreezeCandidate",
            next_stage="CandidateFrozen",
            status="PASS",
            reason=0,
            evidence="PASS",
        )
        return candidate, self.projection.disposition

    def _freeze_drift(self, freeze: FreezeRecord) -> list[str]:
        return sorted(
            name
            for name, current in self.current_freeze_bindings.items()
            if freeze.get(name) != current
        )

    def authorize_evaluator_entry(
        self, candidate_id: str | None = None
    ) -> tuple[FreezeRecord, CandidateRecord]:
        self._require_mode("evaluator")
        self._require_unambiguous()
        freeze = self.projection.freeze
        if freeze is None:
            raise ForgeError("NO_FREEZE", "evaluator mode requires a frozen candidate")
        if candidate_id is not None and candidate_id != freeze["candidate_identity"]:
            raise ForgeError(
                "EVALUATOR_CANDIDATE_MISMATCH",
                "evaluator candidate differs from the frozen candidate",
            )
        drift = self._freeze_drift(freeze)
        if drift:
            raise ForgeError("FREEZE_DRIFT", "frozen identities drifted: " + ", ".join(drift))
        candidate = self._require_candidate(str(freeze["candidate_identity"]))
        if self.projection.disposition_status != "selected" or self.projection.disposition is None:
            raise ForgeError(
                "FREEZE_NOT_SELECTED", "freeze no longer belongs to a selected candidate"
            )
        if freeze.get("selection_record") != self.projection.disposition.get("disposition_id"):
            raise ForgeError(
                "FREEZE_SELECTION_STALE", "freeze does not bind the current selection record"
            )
        return freeze, candidate

    def authorize_reconciliation(self, candidate_id: str | None) -> str | None:
        self._require_unambiguous()
        if candidate_id is not None:
            candidate = self.candidate_record(candidate_id)
            if candidate is None:
                raise ForgeError("RECORD_NOT_FOUND", f"no candidate record for {candidate_id}")
            return candidate_id
        current = self.projection.current_candidate
        return str(current["candidate_id"]) if current is not None else None

    def authorize_bundle(
        self, candidate_id: str | None, *, request_valid: bool = True
    ) -> CandidateRecord:
        self._require_unambiguous()
        if self.native is not None:
            return self._authorize_bundle_native(candidate_id, request_valid=request_valid)
        if self.mode == "evaluator":
            _, candidate = self.authorize_evaluator_entry(candidate_id)
            return candidate
        self._require_mode("development")
        return self._require_candidate(candidate_id)

    def _authorize_bundle_native(
        self, candidate_id: str | None, *, request_valid: bool
    ) -> CandidateRecord:
        """Authorize a bundle only after the typed MNCS precondition projection passes."""

        if self.mode not in {"development", "evaluator"}:
            raise ForgeError("INVALID_MODE", "mode must be development or evaluator")
        candidate = self.projection.current_candidate
        current_identity = str(candidate["candidate_id"]) if candidate is not None else None
        candidate_matches = candidate_id is None or candidate_id == current_identity
        candidate_known = candidate_id is None or self.candidate_record(candidate_id) is not None
        native = self.native
        if native is None:
            raise ForgeError("NATIVE_BUNDLE_UNKNOWN", "native bundle adapter is unavailable")
        result = native.bundle_precondition_projection(
            requested_candidate=candidate_id,
            current_candidate=current_identity,
            candidate_present=candidate is not None,
            candidate_matches=candidate_matches,
            candidate_current=candidate is not None
            and self.projection.candidate_freshness == "CURRENT",
            selected=(
                self.projection.disposition is not None
                and self.projection.disposition_status == "selected"
            ),
            frozen=self.projection.freeze is not None,
            freeze_current=self.projection.freeze_status == "current",
            mode=self.mode,
            request_valid=request_valid,
            evidence_status=self.projection.evidence.status,
            evidence_ready=self.projection.evidence.ready,
        )
        if not result.valid:
            raise ForgeError(
                "NATIVE_BUNDLE_UNKNOWN",
                "native bundle precondition projection is invalid",
            )
        if result.ready:
            if candidate is None:
                raise ForgeError(
                    "NATIVE_BUNDLE_UNKNOWN", "native bundle returned ready without candidate"
                )
            return candidate
        if result.reason == "InvalidRequest":
            raise ForgeError("WORKFLOW_CATEGORY", "bundle requires an MNCS or MNCDS workflow")
        if result.reason == "NoCandidate":
            raise ForgeError("NO_CANDIDATE", "no candidate exists in the active epoch")
        if result.reason == "CandidateMismatch":
            if self.mode == "evaluator":
                raise ForgeError(
                    "EVALUATOR_CANDIDATE_MISMATCH",
                    "evaluator candidate differs from the frozen candidate",
                )
            if candidate_known:
                raise ForgeError(
                    "CANDIDATE_NOT_CURRENT",
                    "candidate is superseded within the active development lineage",
                )
            raise ForgeError("RECORD_NOT_FOUND", f"no candidate record for {candidate_id}")
        if result.reason == "CandidateStale":
            raise ForgeError("STALE_CANDIDATE", "candidate no longer matches current content")
        if result.reason == "FreezeMissing":
            raise ForgeError("NO_FREEZE", "evaluator mode requires a frozen candidate")
        if result.reason == "SelectionMissing":
            raise ForgeError(
                "FREEZE_NOT_SELECTED", "freeze no longer belongs to a selected candidate"
            )
        if result.reason == "FreezeStale":
            raise ForgeError("FREEZE_DRIFT", "frozen identities drifted")
        raise ForgeError(
            "NATIVE_BUNDLE_UNKNOWN",
            f"native bundle precondition rejected with unknown reason {result.reason}",
        )

    def authorize_terminal_result(
        self,
        *,
        action_id: str,
        candidate_id: str,
        freeze_id: str | None,
        mode: str,
    ) -> VerifierActionRecord:
        return self.authorize_terminal_result_from_history(
            self.history,
            action_id=action_id,
            candidate_id=candidate_id,
            freeze_id=freeze_id,
            mode=mode,
        )

    @staticmethod
    def authorize_terminal_result_from_history(
        history: Sequence[LedgerEntry],
        *,
        action_id: str,
        candidate_id: str,
        freeze_id: str | None,
        mode: str,
    ) -> VerifierActionRecord:
        actions = [
            entry.payload
            for entry in history
            if isinstance(entry.payload, VerifierActionRecord)
            and entry.payload.get("action_id") == action_id
        ]
        if not actions:
            raise ForgeError(
                "ACTION_NOT_RECORDED", "terminal result has no recorded verifier action"
            )
        results = [entry for entry in history if isinstance(entry.payload, VerifierResultRecord)]
        return ForgeStateMachine.authorize_terminal_result_for_recorded_action(
            actions[-1],
            results,
            action_id=action_id,
            candidate_id=candidate_id,
            freeze_id=freeze_id,
            mode=mode,
        )

    @staticmethod
    def authorize_terminal_result_for_recorded_action(
        action: VerifierActionRecord,
        existing_results: Sequence[LedgerEntry],
        *,
        action_id: str,
        candidate_id: str,
        freeze_id: str | None,
        mode: str,
    ) -> VerifierActionRecord:
        if action.get("action_id") != action_id:
            raise ForgeError(
                "ACTION_BINDING_MISMATCH", "recorded verifier action identity mismatches result"
            )
        results = [
            entry.payload
            for entry in existing_results
            if isinstance(entry.payload, VerifierResultRecord)
            and entry.payload.get("action_id") == action_id
        ]
        if results:
            raise ForgeError(
                "ACTION_ALREADY_TERMINATED", "verifier action already has a terminal result"
            )
        expected = {
            "candidate_identity": candidate_id,
            "freeze_identity": freeze_id,
            "mode": mode,
        }
        mismatched = [name for name, value in expected.items() if action.get(name) != value]
        if mismatched:
            raise ForgeError(
                "ACTION_BINDING_MISMATCH",
                "terminal verifier result mismatches action bindings: "
                + ", ".join(sorted(mismatched)),
            )
        return action

    @staticmethod
    def _blocked(code: str, message: str) -> list[TransitionBlocker]:
        return [TransitionBlocker(code, message)]

    def _operation_blockers(self) -> dict[str, list[TransitionBlocker]]:
        blockers: dict[str, list[TransitionBlocker]] = {}
        candidate = self.projection.current_candidate
        candidate_id = str(candidate["candidate_id"]) if candidate is not None else None
        active_epoch = self.projection.active_epoch
        active_epoch_id = str(active_epoch["epoch_id"]) if active_epoch is not None else None
        lineage = list(self.projection.candidate_lineage)

        def inspect_transition(name: str, transition: Callable[[], object]) -> None:
            try:
                transition()
            except ForgeError as exc:
                blockers[name] = self._blocked(exc.code, exc.message)
            else:
                blockers[name] = []

        inspect_transition("begin_epoch", lambda: self.authorize_epoch_begin(active_epoch_id))
        inspect_transition(
            "register_candidate",
            lambda: self.authorize_candidate_register(
                parent_candidate=candidate_id,
                proposed_identity="prospective-candidate-identity",
            ),
        )
        inspect_transition(
            "run_project_check",
            lambda: self.authorize_development_work(None, project_scoped=True),
        )
        inspect_transition(
            "run_candidate_check",
            lambda: self.authorize_development_work(candidate_id, project_scoped=False),
        )
        blockers["run_development_verifier"] = list(blockers["run_candidate_check"])

        def compare() -> None:
            if len(lineage) < 2:
                raise ForgeError("COMPARE_INPUT", "at least two active-lineage candidates required")
            self.authorize_candidate_comparison(lineage)

        inspect_transition("compare_candidates", compare)
        inspect_transition(
            "select_candidate",
            lambda: self.authorize_candidate_disposition(candidate_id or "", "selected"),
        )
        inspect_transition(
            "reject_candidate",
            lambda: self.authorize_candidate_disposition(candidate_id or "", "rejected"),
        )
        inspect_transition(
            "freeze_candidate",
            lambda: self.authorize_candidate_freeze(
                candidate_id or "", evidence_plan_requirements=self.required_evidence
            ),
        )
        inspect_transition("run_evaluation", lambda: self.authorize_evaluator_entry(candidate_id))
        blockers["run_evaluator_verifier"] = list(blockers["run_evaluation"])
        inspect_transition(
            "reconcile_evidence", lambda: self.authorize_reconciliation(candidate_id)
        )
        inspect_transition("build_bundle", lambda: self.authorize_bundle(candidate_id))

        terminal_ids = {
            str(record["action_id"])
            for _, record in self._records(RecordType.VERIFIER_RESULT)
            if isinstance(record, VerifierResultRecord)
        }
        unterminated = next(
            (
                record
                for _, record in self._records(RecordType.VERIFIER_ACTION)
                if isinstance(record, VerifierActionRecord)
                and str(record["action_id"]) not in terminal_ids
            ),
            None,
        )

        def record_terminal_result() -> None:
            if unterminated is None:
                raise ForgeError("ACTION_NOT_RECORDED", "no unterminated verifier action exists")
            self.authorize_terminal_result(
                action_id=str(unterminated["action_id"]),
                candidate_id=str(unterminated["candidate_identity"]),
                freeze_id=(
                    str(unterminated["freeze_identity"])
                    if unterminated["freeze_identity"] is not None
                    else None
                ),
                mode=str(unterminated["mode"]),
            )

        inspect_transition("record_verifier_result", record_terminal_result)
        return blockers

    def inspect(self) -> dict[str, object]:
        if self._inspection_cache is not None:
            return copy.deepcopy(self._inspection_cache)
        projection = self.projection
        # Inspection probes every prospective operation to explain blockers.
        # Running a subprocess for each hypothetical transition would make a
        # read-only query mutate latency and would not validate a real change.
        # Actual mutation authorizers keep the native preflight enabled.
        native_preflight_enabled = self._native_preflight_enabled
        self._native_preflight_enabled = False
        try:
            blockers = self._operation_blockers()
        finally:
            self._native_preflight_enabled = native_preflight_enabled
        epoch = projection.active_epoch
        candidate = projection.current_candidate
        disposition = projection.disposition
        freeze = projection.freeze
        result = {
            "stage": projection.stage.value,
            "mode": self.mode,
            "epoch": {
                "status": projection.epoch_status,
                "active_identity": str(epoch["epoch_id"]) if epoch is not None else None,
                "superseded_identities": list(projection.superseded_epochs),
            },
            "candidate": {
                "status": projection.candidate_status,
                "identity": str(candidate["candidate_id"]) if candidate is not None else None,
                "freshness": projection.candidate_freshness,
                "epoch_identity": str(candidate["source_epoch"]) if candidate is not None else None,
                "lineage": list(projection.candidate_lineage),
            },
            "evidence": projection.evidence.to_dict(),
            "disposition": {
                "status": projection.disposition_status,
                "identity": (
                    str(disposition["disposition_id"]) if disposition is not None else None
                ),
            },
            "freeze": {
                "status": projection.freeze_status,
                "identity": str(freeze["freeze_id"]) if freeze is not None else None,
                "candidate_identity": (
                    str(freeze["candidate_identity"]) if freeze is not None else None
                ),
            },
            "evaluation": {
                "status": projection.evaluation_status,
                "record_identities": [self._identity(item) for item in projection.evaluations],
            },
            "reconciliation": {
                "status": projection.reconciliation_status,
                "persistent": False,
                "candidate_identity": (
                    str(candidate["candidate_id"]) if candidate is not None else None
                ),
                "limitation": (
                    "reconciliation is a deterministic derived view; invocation is not recorded"
                ),
            },
            "bundle": {
                "status": projection.bundle_status,
                "record_identities": [self._identity(item) for item in projection.bundles],
            },
            "allowed_operations": sorted(name for name, values in blockers.items() if not values),
            "blocked_operations": {
                name: [item.to_dict() for item in values]
                for name, values in sorted(blockers.items())
                if values
            },
            "limitations": [item.to_dict() for item in projection.limitations],
        }
        self._inspection_cache = result
        return copy.deepcopy(result)
