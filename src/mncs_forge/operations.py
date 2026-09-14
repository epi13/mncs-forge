"""Typed public-operation inventory and canonical Forge invocation boundary."""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import MISSING, dataclass, fields
from enum import StrEnum
from types import UnionType
from typing import Protocol, TypeVar, Union, get_args, get_origin, get_type_hints

from .errors import ForgeError

JsonObject = dict[str, object]
ALL_MODES = frozenset({"development", "evaluator"})
DEVELOPMENT_ONLY = frozenset({"development"})
EVALUATOR_ONLY = frozenset({"evaluator"})


class OperationInterface(StrEnum):
    CLI = "cli"
    MCP = "mcp"
    RESOURCE = "resource"
    INTERNAL = "internal"


class MutationClass(StrEnum):
    READ_ONLY = "read-only"
    MUTATING = "mutating"


class AuthorityRequirement(StrEnum):
    NONE = "none"
    LOCAL_CONFIGURATION = "local-configuration"
    LOCAL_STORAGE = "local-storage"
    DECLARED_PROVIDER = "declared-provider"
    DECLARED_VERIFIER = "declared-verifier"
    DEVELOPMENT = "development-authority"
    EVALUATOR = "evaluator-authority"
    PUBLIC_VALIDATOR = "declared-public-validator"


class LifecycleRequirement(StrEnum):
    NONE = "none"
    PROJECTION = "lifecycle-projection"
    ACTIVE_EPOCH = "active-epoch"
    CURRENT_CANDIDATE = "current-candidate"
    REQUIRED_EVIDENCE = "required-evidence-ready"
    SELECTED_CANDIDATE = "selected-current-candidate"
    VALID_FREEZE = "valid-current-freeze"
    RECONCILABLE_HISTORY = "reconcilable-history"
    BUNDLE_ELIGIBLE = "bundle-eligible-candidate"
    VERIFIER_BINDINGS = "verifier-action-and-subject-bindings"


class DisclosureClass(StrEnum):
    PUBLIC_METADATA = "public-metadata"
    LOCAL_PROJECT = "local-project"
    DEVELOPMENT_EVIDENCE = "development-evidence"
    POLICY_CONTROLLED = "policy-controlled"
    EVALUATOR_STATUS_ONLY = "evaluator-status-only"


class OutputContract(StrEnum):
    DIAGNOSTIC = "diagnostic-object"
    PROJECT = "project-inspection"
    LIFECYCLE = "lifecycle-inspection"
    CLAIM = "claim-report"
    INVENTORY = "inventory"
    RECORD = "versioned-record"
    RESULT_SET = "result-set"
    EXPLANATION = "evidence-explanation"
    RECONCILIATION = "reconciliation"
    BUNDLE = "bundle-result"


class CliDecoder(StrEnum):
    VALUE = "value"
    JSON_VALUE = "json-value"
    JSON_OBJECT = "json-object"
    DEPENDENCIES = "dependencies"


@dataclass(frozen=True, slots=True)
class CliBinding:
    input_name: str
    namespace_name: str
    decoder: CliDecoder = CliDecoder.VALUE


@dataclass(frozen=True, slots=True)
class CliExposure:
    command: tuple[str, ...]
    bindings: tuple[CliBinding, ...] = ()


@dataclass(frozen=True, slots=True)
class McpExposure:
    tool_name: str
    visible_modes: frozenset[str] = ALL_MODES


@dataclass(frozen=True, slots=True)
class ResourceExposure:
    uri: str
    projection: str = "identity"


@dataclass(frozen=True, slots=True)
class OperationInput:
    """Marker base for explicit frozen operation input models."""


@dataclass(frozen=True, slots=True)
class NoInput(OperationInput):
    pass


@dataclass(frozen=True, slots=True)
class ClaimBlockersInput(OperationInput):
    requested_claim: str = "promotion"


@dataclass(frozen=True, slots=True)
class ProviderProbeInput(OperationInput):
    provider_id: str


@dataclass(frozen=True, slots=True)
class LearnedSpecialistShadowInput(OperationInput):
    artifact_path: str
    provider_command: list[str]
    source_records: list[dict[str, object]]
    context_observations: list[dict[str, object]] | None = None
    lineage_identity: str | None = None
    timeout_seconds: float = 5.0


@dataclass(frozen=True, slots=True)
class CapabilityBlockersInput(OperationInput):
    required_capabilities: list[str] | None = None


@dataclass(frozen=True, slots=True)
class VerifierDescribeInput(OperationInput):
    verifier_id: str


@dataclass(frozen=True, slots=True)
class VerifierMatchInput(OperationInput):
    uncertainty_classes: list[str]
    language: str | None = None
    artifact_type: str | None = None
    changed_paths: list[str] | None = None
    scope: str | None = None
    maximum_cost: str = "high"
    required_category: str | None = None
    active_mode: str | None = None


@dataclass(frozen=True, slots=True)
class VerifierRunInput(OperationInput):
    verifier_id: str
    candidate_identity: str | None = None
    changed_paths: list[str] | None = None
    scope: str | None = None
    source_region: dict[str, object] | None = None
    contract_identity: str | None = None
    dependency_slice_identities: dict[str, str] | None = None
    prior_artifact_identity: str | None = None
    question_parameters: dict[str, object] | None = None


@dataclass(frozen=True, slots=True)
class VerifierBatchInput(OperationInput):
    verifier_ids: list[str]
    candidate_identity: str | None = None
    changed_paths: list[str] | None = None
    scope: str | None = None
    source_region: dict[str, object] | None = None
    contract_identity: str | None = None
    dependency_slice_identities: dict[str, str] | None = None
    prior_artifact_identity: str | None = None
    question_parameters: dict[str, object] | None = None


@dataclass(frozen=True, slots=True)
class VerifierExplainInput(OperationInput):
    output_identity: str


@dataclass(frozen=True, slots=True)
class EpochBeginInput(OperationInput):
    generator_identity: str
    evaluator_identity: str
    parent_epoch: str | None = None
    authority_overlap: list[str] | None = None


@dataclass(frozen=True, slots=True)
class CandidateRegisterInput(OperationInput):
    changed_files: list[str]
    hypothesis: str
    generator_identity: str
    generator_config_identity: str
    parent_candidate: str | None = None
    expected_identity: str | None = None


@dataclass(frozen=True, slots=True)
class CandidateRefreshInput(OperationInput):
    hypothesis: str
    generator_identity: str
    generator_config_identity: str
    changed_files: list[str] | None = None


@dataclass(frozen=True, slots=True)
class DevelopmentChecksInput(OperationInput):
    workflow_names: list[str]
    candidate_identity: str | None = None


@dataclass(frozen=True, slots=True)
class FailureExplainInput(OperationInput):
    output_identity: str | None = None


@dataclass(frozen=True, slots=True)
class MncsFailureLoopInput(OperationInput):
    manifest: str
    test_command: list[str] | None = None
    debug_command: list[str] | None = None
    mncs_binary: str | None = None
    library_paths: list[str] | None = None
    embed_library: str | None = None
    working_directory: str = "."
    test_result_file: str = ".mncs-forge/mncs-test-result.json"
    test_check_file: str = ".mncs-forge/mncs-test-check.json"
    test_artifacts_directory: str = ".mncs-forge/mncs-test-artifacts"
    debug_witness_file: str = ".mncs-forge/mncs-debug-witness.json"
    debug_artifacts_directory: str = ".mncs-forge/mncs-debug-artifacts"
    capture_policy: str = "failure-only"
    max_events: int = 256
    timeout_seconds: float | None = None
    minimize: bool = False
    test_id: str | None = None
    repair_path: str | None = None
    repair_from: str | None = None
    repair_to: str | None = None
    output_file: str | None = None


@dataclass(frozen=True, slots=True)
class CandidateCompareInput(OperationInput):
    candidate_identities: list[str]


@dataclass(frozen=True, slots=True)
class CandidateDispositionInput(OperationInput):
    candidate_identity: str
    reason: str


@dataclass(frozen=True, slots=True)
class CandidateFreezeInput(OperationInput):
    candidate_identity: str
    environment_identity: str
    required_evidence_plan: str


@dataclass(frozen=True, slots=True)
class FinalEvaluationInput(OperationInput):
    workflow_names: list[str]


@dataclass(frozen=True, slots=True)
class EvidenceReconcileInput(OperationInput):
    candidate_identity: str | None = None


@dataclass(frozen=True, slots=True)
class BundleBuildInput(OperationInput):
    workflow_name: str
    candidate_identity: str | None = None


@dataclass(frozen=True, slots=True)
class ExecutionReceiptListInput(OperationInput):
    candidate_identity: str | None = None
    action_identity: str | None = None


@dataclass(frozen=True, slots=True)
class ExecutionReceiptGetInput(OperationInput):
    binding_id: str


@dataclass(frozen=True, slots=True)
class CompilerExperimentRecordInput(OperationInput):
    language_record: dict[str, object]


@dataclass(frozen=True, slots=True)
class CompilerExperimentCompareInput(OperationInput):
    left_experiment_id: str
    right_experiment_id: str


@dataclass(frozen=True, slots=True)
class ConceptEvaluationRecordInput(OperationInput):
    evaluation: dict[str, object]


@dataclass(frozen=True, slots=True)
class ConceptEvaluationGetInput(OperationInput):
    evaluation_id: str


@dataclass(frozen=True, slots=True)
class CompilerCandidateRegisterInput(OperationInput):
    baseline_artifact_identity: str
    candidate_artifact_identity: str
    generator_identity: str
    declared_transformation: str
    claimed_relation: str
    expected_benefit: str
    protected_properties: list[str] | None = None
    target_envelope: str = "unspecified"
    required_validation: str = "translation-validation"


@dataclass(frozen=True, slots=True)
class CompilerCandidateCompareInput(OperationInput):
    left_candidate_id: str
    right_candidate_id: str


@dataclass(frozen=True, slots=True)
class CompilerCandidateAttachInput(OperationInput):
    candidate_id: str
    validator_identity: str
    judgement: str
    claimed_relation: str
    counterexample: dict[str, object] | None = None
    limitations: list[str] | None = None
    stale: bool = False
    expected_artifact_identity: str | None = None


@dataclass(frozen=True, slots=True)
class CompilerTournamentInput(OperationInput):
    candidate_ids: list[str]


@dataclass(frozen=True, slots=True)
class CompilerCandidateSelectInput(OperationInput):
    candidate_id: str
    policy: str = "explicit-protected-property-policy"


@dataclass(frozen=True, slots=True)
class CompilerCandidateInspectInput(OperationInput):
    candidate_id: str


@dataclass(frozen=True, slots=True)
class AssuranceAssessInput(OperationInput):
    binding_id: str
    requested_properties: list[str]
    policy_identity: str | None = None


@dataclass(frozen=True, slots=True)
class AssuranceListInput(OperationInput):
    binding_identity: str | None = None
    candidate_identity: str | None = None


@dataclass(frozen=True, slots=True)
class CellDocumentValidateInput(OperationInput):
    kind: str
    document: dict[str, object]


@dataclass(frozen=True, slots=True)
class CellExecutionAssessInput(OperationInput):
    policy: dict[str, object]
    record: dict[str, object]
    expected_nonce: str | None = None


class ForgeOperationTarget(Protocol):
    """Facade surface consumed by registry handlers without importing the concrete facade."""

    mode: str

    def doctor(self) -> JsonObject: ...
    def project_inspect(self) -> JsonObject: ...
    def state_inspect(self) -> JsonObject: ...
    def claim_status(self) -> JsonObject: ...
    def claim_blockers(self, requested_claim: str) -> JsonObject: ...
    def provider_list(self) -> JsonObject: ...
    def provider_probe(self, provider_id: str) -> JsonObject: ...
    def learned_specialist_shadow(
        self,
        artifact_path: str,
        provider_command: list[str],
        source_records: list[dict[str, object]],
        *,
        context_observations: list[dict[str, object]] | None = None,
        lineage_identity: str | None = None,
        timeout_seconds: float = 5.0,
    ) -> JsonObject: ...
    def capability_blockers(self, required_capabilities: list[str] | None = None) -> JsonObject: ...
    def verifier_list(self) -> JsonObject: ...
    def verifier_describe(self, verifier_id: str) -> JsonObject: ...
    def verifier_match(
        self,
        *,
        uncertainty_classes: list[str],
        language: str | None = None,
        artifact_type: str | None = None,
        changed_paths: list[str] | None = None,
        scope: str | None = None,
        maximum_cost: str = "high",
        required_category: str | None = None,
        active_mode: str | None = None,
    ) -> JsonObject: ...
    def verifier_run(
        self,
        verifier_id: str,
        *,
        candidate_identity: str | None = None,
        changed_paths: list[str] | None = None,
        scope: str | None = None,
        source_region: dict[str, object] | None = None,
        contract_identity: str | None = None,
        dependency_slice_identities: dict[str, str] | None = None,
        prior_artifact_identity: str | None = None,
        question_parameters: dict[str, object] | None = None,
    ) -> JsonObject: ...
    def verifier_batch(
        self,
        verifier_ids: list[str],
        *,
        candidate_identity: str | None = None,
        changed_paths: list[str] | None = None,
        scope: str | None = None,
        source_region: dict[str, object] | None = None,
        contract_identity: str | None = None,
        dependency_slice_identities: dict[str, str] | None = None,
        prior_artifact_identity: str | None = None,
        question_parameters: dict[str, object] | None = None,
    ) -> JsonObject: ...
    def verifier_explain(self, output_identity: str) -> JsonObject: ...
    def epoch_begin(
        self,
        *,
        generator_identity: str,
        evaluator_identity: str,
        parent_epoch: str | None = None,
        authority_overlap: list[str] | None = None,
    ) -> JsonObject: ...
    def candidate_register(
        self,
        *,
        changed_files: list[str],
        hypothesis: str,
        generator_identity: str,
        generator_config_identity: str,
        parent_candidate: str | None = None,
        expected_identity: str | None = None,
    ) -> JsonObject: ...
    def candidate_refresh(
        self,
        *,
        hypothesis: str,
        generator_identity: str,
        generator_config_identity: str,
        changed_files: list[str] | None = None,
    ) -> JsonObject: ...
    def development_checks_run(
        self, workflow_names: list[str], candidate_id: str | None = None
    ) -> JsonObject: ...
    def failure_explain(self, output_identity: str | None = None) -> JsonObject: ...
    def mncs_failure_loop(
        self,
        *,
        manifest: str,
        test_command: list[str] | None = None,
        debug_command: list[str] | None = None,
        mncs_binary: str | None = None,
        library_paths: list[str] | None = None,
        embed_library: str | None = None,
        working_directory: str = ".",
        test_result_file: str = ".mncs-forge/mncs-test-result.json",
        test_check_file: str = ".mncs-forge/mncs-test-check.json",
        test_artifacts_directory: str = ".mncs-forge/mncs-test-artifacts",
        debug_witness_file: str = ".mncs-forge/mncs-debug-witness.json",
        debug_artifacts_directory: str = ".mncs-forge/mncs-debug-artifacts",
        capture_policy: str = "failure-only",
        max_events: int = 256,
        timeout_seconds: float | None = None,
        test_id: str | None = None,
        repair_path: str | None = None,
        repair_from: str | None = None,
        repair_to: str | None = None,
        output_file: str | None = None,
    ) -> JsonObject: ...
    def candidate_compare(self, candidate_ids: list[str]) -> JsonObject: ...
    def candidate_disposition(
        self, candidate_id: str, *, disposition: str, reason: str
    ) -> JsonObject: ...
    def candidate_freeze(
        self, candidate_id: str, *, environment_identity: str, required_evidence_plan: str
    ) -> JsonObject: ...
    def final_evaluation_run(self, workflow_names: list[str]) -> JsonObject: ...
    def evidence_reconcile(self, candidate_id: str | None = None) -> JsonObject: ...
    def bundle_build(self, workflow_name: str, candidate_id: str | None = None) -> JsonObject: ...
    def license_evidence_scan(self) -> JsonObject: ...
    def execution_receipts_list(
        self,
        candidate_identity: str | None = None,
        action_identity: str | None = None,
    ) -> JsonObject: ...
    def execution_receipts_get(self, binding_id: str) -> JsonObject: ...
    def execution_assurance_assess(
        self,
        *,
        binding_id: str,
        requested_properties: list[str],
        policy_identity: str | None = None,
    ) -> JsonObject: ...
    def execution_assurance_list(
        self,
        binding_identity: str | None = None,
        candidate_identity: str | None = None,
    ) -> JsonObject: ...
    def cell_document_validate(self, kind: str, document: Mapping[str, object]) -> JsonObject: ...
    def cell_execution_assess(
        self,
        policy: Mapping[str, object],
        record: Mapping[str, object],
        expected_nonce: str | None = None,
    ) -> JsonObject: ...
    def compiler_experiment_record(self, language_record: Mapping[str, object]) -> JsonObject: ...
    def compiler_experiments_list(self) -> JsonObject: ...
    def compiler_experiments_compare(
        self, left_experiment_id: str, right_experiment_id: str
    ) -> JsonObject: ...
    def concept_evaluation_record(self, evaluation: Mapping[str, object]) -> JsonObject: ...
    def concept_evaluations_list(self) -> JsonObject: ...
    def concept_evaluation_get(self, evaluation_id: str) -> JsonObject: ...
    def compiler_candidate_register(
        self,
        *,
        baseline_artifact_identity: str,
        candidate_artifact_identity: str,
        generator_identity: str,
        declared_transformation: str,
        claimed_relation: str,
        expected_benefit: str,
        protected_properties: list[str] | None = None,
        target_envelope: str = "unspecified",
        required_validation: str = "translation-validation",
    ) -> JsonObject: ...
    def compiler_candidates_list(self) -> JsonObject: ...
    def compiler_candidates_compare(
        self, left_candidate_id: str, right_candidate_id: str
    ) -> JsonObject: ...
    def compiler_candidate_attach_validation(
        self,
        candidate_id: str,
        *,
        validator_identity: str,
        judgement: str,
        claimed_relation: str,
        counterexample: dict[str, object] | None = None,
        limitations: list[str] | None = None,
        stale: bool = False,
        expected_artifact_identity: str | None = None,
    ) -> JsonObject: ...
    def compiler_tournament(self, candidate_ids: list[str]) -> JsonObject: ...
    def compiler_candidate_select(self, candidate_id: str, policy: str) -> JsonObject: ...
    def compiler_candidate_inspect(self, candidate_id: str) -> JsonObject: ...
    def ledger_verify(self) -> JsonObject: ...
    def config_validate(self) -> JsonObject: ...


OperationHandler = Callable[[ForgeOperationTarget, OperationInput], JsonObject]


@dataclass(frozen=True, slots=True)
class OperationDefinition:
    operation_id: str
    modes: frozenset[str]
    mutation: MutationClass
    input_model: type[OperationInput]
    output: OutputContract
    handler: OperationHandler
    authority: AuthorityRequirement
    lifecycle: LifecycleRequirement
    disclosure: DisclosureClass
    description: str
    cli: CliExposure | None = None
    mcp: McpExposure | None = None
    resources: tuple[ResourceExposure, ...] = ()
    cli_exclusion: str | None = None
    mcp_exclusion: str | None = None

    def inventory(self) -> JsonObject:
        return {
            "operation_id": self.operation_id,
            "modes": sorted(self.modes),
            "mutation": self.mutation.value,
            "input_model": self.input_model.__name__,
            "output_contract": self.output.value,
            "authority": self.authority.value,
            "lifecycle": self.lifecycle.value,
            "disclosure": self.disclosure.value,
            "cli": (
                {
                    "exposed": True,
                    "command": list(self.cli.command),
                }
                if self.cli is not None
                else {"exposed": False, "reason": self.cli_exclusion}
            ),
            "mcp": (
                {
                    "exposed": True,
                    "tool_name": self.mcp.tool_name,
                    "visible_modes": sorted(self.mcp.visible_modes),
                }
                if self.mcp is not None
                else {"exposed": False, "reason": self.mcp_exclusion}
            ),
            "resources": [
                {"uri": item.uri, "projection": item.projection} for item in self.resources
            ],
        }


InputT = TypeVar("InputT", bound=OperationInput)


def _typed(value: OperationInput, expected: type[InputT]) -> InputT:
    if not isinstance(value, expected):  # pragma: no cover - registry construction enforces this
        raise ForgeError("OPERATION_INPUT", f"handler requires {expected.__name__}")
    return value


def _no_input(value: OperationInput) -> None:
    _typed(value, NoInput)


def _doctor(forge: ForgeOperationTarget, value: OperationInput) -> JsonObject:
    _no_input(value)
    return forge.doctor()


def _project_inspect(forge: ForgeOperationTarget, value: OperationInput) -> JsonObject:
    _no_input(value)
    return forge.project_inspect()


def _state_inspect(forge: ForgeOperationTarget, value: OperationInput) -> JsonObject:
    _no_input(value)
    return forge.state_inspect()


def _claim_status(forge: ForgeOperationTarget, value: OperationInput) -> JsonObject:
    _no_input(value)
    return forge.claim_status()


def _claim_blockers(forge: ForgeOperationTarget, value: OperationInput) -> JsonObject:
    request = _typed(value, ClaimBlockersInput)
    return forge.claim_blockers(request.requested_claim)


def _provider_list(forge: ForgeOperationTarget, value: OperationInput) -> JsonObject:
    _no_input(value)
    return forge.provider_list()


def _provider_probe(forge: ForgeOperationTarget, value: OperationInput) -> JsonObject:
    request = _typed(value, ProviderProbeInput)
    return forge.provider_probe(request.provider_id)


def _learned_specialist_shadow(forge: ForgeOperationTarget, value: OperationInput) -> JsonObject:
    request = _typed(value, LearnedSpecialistShadowInput)
    return forge.learned_specialist_shadow(
        request.artifact_path,
        request.provider_command,
        request.source_records,
        context_observations=request.context_observations,
        lineage_identity=request.lineage_identity,
        timeout_seconds=request.timeout_seconds,
    )


def _capability_blockers(forge: ForgeOperationTarget, value: OperationInput) -> JsonObject:
    request = _typed(value, CapabilityBlockersInput)
    return forge.capability_blockers(request.required_capabilities)


def _verifier_list(forge: ForgeOperationTarget, value: OperationInput) -> JsonObject:
    _no_input(value)
    return forge.verifier_list()


def _verifier_describe(forge: ForgeOperationTarget, value: OperationInput) -> JsonObject:
    request = _typed(value, VerifierDescribeInput)
    return forge.verifier_describe(request.verifier_id)


def _verifier_match(forge: ForgeOperationTarget, value: OperationInput) -> JsonObject:
    request = _typed(value, VerifierMatchInput)
    return forge.verifier_match(
        uncertainty_classes=request.uncertainty_classes,
        language=request.language,
        artifact_type=request.artifact_type,
        changed_paths=request.changed_paths,
        scope=request.scope,
        maximum_cost=request.maximum_cost,
        required_category=request.required_category,
        active_mode=request.active_mode,
    )


def _verifier_run(forge: ForgeOperationTarget, value: OperationInput) -> JsonObject:
    request = _typed(value, VerifierRunInput)
    return forge.verifier_run(
        request.verifier_id,
        candidate_identity=request.candidate_identity,
        changed_paths=request.changed_paths,
        scope=request.scope,
        source_region=request.source_region,
        contract_identity=request.contract_identity,
        dependency_slice_identities=request.dependency_slice_identities,
        prior_artifact_identity=request.prior_artifact_identity,
        question_parameters=request.question_parameters,
    )


def _verifier_batch(forge: ForgeOperationTarget, value: OperationInput) -> JsonObject:
    request = _typed(value, VerifierBatchInput)
    return forge.verifier_batch(
        request.verifier_ids,
        candidate_identity=request.candidate_identity,
        changed_paths=request.changed_paths,
        scope=request.scope,
        source_region=request.source_region,
        contract_identity=request.contract_identity,
        dependency_slice_identities=request.dependency_slice_identities,
        prior_artifact_identity=request.prior_artifact_identity,
        question_parameters=request.question_parameters,
    )


def _verifier_explain(forge: ForgeOperationTarget, value: OperationInput) -> JsonObject:
    request = _typed(value, VerifierExplainInput)
    return forge.verifier_explain(request.output_identity)


def _epoch_begin(forge: ForgeOperationTarget, value: OperationInput) -> JsonObject:
    request = _typed(value, EpochBeginInput)
    return forge.epoch_begin(
        generator_identity=request.generator_identity,
        evaluator_identity=request.evaluator_identity,
        parent_epoch=request.parent_epoch,
        authority_overlap=request.authority_overlap,
    )


def _candidate_register(forge: ForgeOperationTarget, value: OperationInput) -> JsonObject:
    request = _typed(value, CandidateRegisterInput)
    return forge.candidate_register(
        changed_files=request.changed_files,
        hypothesis=request.hypothesis,
        generator_identity=request.generator_identity,
        generator_config_identity=request.generator_config_identity,
        parent_candidate=request.parent_candidate,
        expected_identity=request.expected_identity,
    )


def _candidate_refresh(forge: ForgeOperationTarget, value: OperationInput) -> JsonObject:
    request = _typed(value, CandidateRefreshInput)
    return forge.candidate_refresh(
        hypothesis=request.hypothesis,
        generator_identity=request.generator_identity,
        generator_config_identity=request.generator_config_identity,
        changed_files=request.changed_files,
    )


def _development_checks(forge: ForgeOperationTarget, value: OperationInput) -> JsonObject:
    request = _typed(value, DevelopmentChecksInput)
    return forge.development_checks_run(request.workflow_names, request.candidate_identity)


def _failure_explain(forge: ForgeOperationTarget, value: OperationInput) -> JsonObject:
    request = _typed(value, FailureExplainInput)
    return forge.failure_explain(request.output_identity)


def _mncs_failure_loop(forge: ForgeOperationTarget, value: OperationInput) -> JsonObject:
    request = _typed(value, MncsFailureLoopInput)
    return forge.mncs_failure_loop(
        manifest=request.manifest,
        test_command=request.test_command,
        debug_command=request.debug_command,
        mncs_binary=request.mncs_binary,
        library_paths=request.library_paths,
        embed_library=request.embed_library,
        working_directory=request.working_directory,
        test_result_file=request.test_result_file,
        test_check_file=request.test_check_file,
        test_artifacts_directory=request.test_artifacts_directory,
        debug_witness_file=request.debug_witness_file,
        debug_artifacts_directory=request.debug_artifacts_directory,
        capture_policy=request.capture_policy,
        max_events=request.max_events,
        timeout_seconds=request.timeout_seconds,
        minimize=request.minimize,
        test_id=request.test_id,
        repair_path=request.repair_path,
        repair_from=request.repair_from,
        repair_to=request.repair_to,
        output_file=request.output_file,
    )


def _candidate_compare(forge: ForgeOperationTarget, value: OperationInput) -> JsonObject:
    request = _typed(value, CandidateCompareInput)
    return forge.candidate_compare(request.candidate_identities)


def _candidate_select(forge: ForgeOperationTarget, value: OperationInput) -> JsonObject:
    request = _typed(value, CandidateDispositionInput)
    return forge.candidate_disposition(
        request.candidate_identity, disposition="selected", reason=request.reason
    )


def _candidate_reject(forge: ForgeOperationTarget, value: OperationInput) -> JsonObject:
    request = _typed(value, CandidateDispositionInput)
    return forge.candidate_disposition(
        request.candidate_identity, disposition="rejected", reason=request.reason
    )


def _candidate_freeze(forge: ForgeOperationTarget, value: OperationInput) -> JsonObject:
    request = _typed(value, CandidateFreezeInput)
    return forge.candidate_freeze(
        request.candidate_identity,
        environment_identity=request.environment_identity,
        required_evidence_plan=request.required_evidence_plan,
    )


def _final_evaluation(forge: ForgeOperationTarget, value: OperationInput) -> JsonObject:
    request = _typed(value, FinalEvaluationInput)
    return forge.final_evaluation_run(request.workflow_names)


def _evidence_reconcile(forge: ForgeOperationTarget, value: OperationInput) -> JsonObject:
    request = _typed(value, EvidenceReconcileInput)
    return forge.evidence_reconcile(request.candidate_identity)


def _bundle_build(forge: ForgeOperationTarget, value: OperationInput) -> JsonObject:
    request = _typed(value, BundleBuildInput)
    return forge.bundle_build(request.workflow_name, request.candidate_identity)


def _license_evidence_scan(forge: ForgeOperationTarget, value: OperationInput) -> JsonObject:
    del value
    return forge.license_evidence_scan()


def _execution_receipts_list(forge: ForgeOperationTarget, value: OperationInput) -> JsonObject:
    request = _typed(value, ExecutionReceiptListInput)
    return forge.execution_receipts_list(request.candidate_identity, request.action_identity)


def _execution_receipts_get(forge: ForgeOperationTarget, value: OperationInput) -> JsonObject:
    request = _typed(value, ExecutionReceiptGetInput)
    return forge.execution_receipts_get(request.binding_id)


def _execution_assurance_assess(forge: ForgeOperationTarget, value: OperationInput) -> JsonObject:
    request = _typed(value, AssuranceAssessInput)
    return forge.execution_assurance_assess(
        binding_id=request.binding_id,
        requested_properties=request.requested_properties,
        policy_identity=request.policy_identity,
    )


def _execution_assurance_list(forge: ForgeOperationTarget, value: OperationInput) -> JsonObject:
    request = _typed(value, AssuranceListInput)
    return forge.execution_assurance_list(
        binding_identity=request.binding_identity,
        candidate_identity=request.candidate_identity,
    )


def _cell_document_validate(forge: ForgeOperationTarget, value: OperationInput) -> JsonObject:
    request = _typed(value, CellDocumentValidateInput)
    return forge.cell_document_validate(request.kind, request.document)


def _cell_execution_assess(forge: ForgeOperationTarget, value: OperationInput) -> JsonObject:
    request = _typed(value, CellExecutionAssessInput)
    return forge.cell_execution_assess(
        request.policy,
        request.record,
        expected_nonce=request.expected_nonce,
    )


def _compiler_experiment_record(forge: ForgeOperationTarget, value: OperationInput) -> JsonObject:
    request = _typed(value, CompilerExperimentRecordInput)
    return forge.compiler_experiment_record(request.language_record)


def _compiler_experiments_list(forge: ForgeOperationTarget, value: OperationInput) -> JsonObject:
    _no_input(value)
    return forge.compiler_experiments_list()


def _compiler_experiments_compare(forge: ForgeOperationTarget, value: OperationInput) -> JsonObject:
    request = _typed(value, CompilerExperimentCompareInput)
    return forge.compiler_experiments_compare(
        request.left_experiment_id,
        request.right_experiment_id,
    )


def _concept_evaluation_record(forge: ForgeOperationTarget, value: OperationInput) -> JsonObject:
    request = _typed(value, ConceptEvaluationRecordInput)
    return forge.concept_evaluation_record(request.evaluation)


def _concept_evaluations_list(forge: ForgeOperationTarget, value: OperationInput) -> JsonObject:
    _no_input(value)
    return forge.concept_evaluations_list()


def _concept_evaluation_get(forge: ForgeOperationTarget, value: OperationInput) -> JsonObject:
    request = _typed(value, ConceptEvaluationGetInput)
    return forge.concept_evaluation_get(request.evaluation_id)


def _compiler_candidate_register(forge: ForgeOperationTarget, value: OperationInput) -> JsonObject:
    request = _typed(value, CompilerCandidateRegisterInput)
    return forge.compiler_candidate_register(
        baseline_artifact_identity=request.baseline_artifact_identity,
        candidate_artifact_identity=request.candidate_artifact_identity,
        generator_identity=request.generator_identity,
        declared_transformation=request.declared_transformation,
        claimed_relation=request.claimed_relation,
        expected_benefit=request.expected_benefit,
        protected_properties=request.protected_properties,
        target_envelope=request.target_envelope,
        required_validation=request.required_validation,
    )


def _compiler_candidates_list(forge: ForgeOperationTarget, value: OperationInput) -> JsonObject:
    _no_input(value)
    return forge.compiler_candidates_list()


def _compiler_candidates_compare(forge: ForgeOperationTarget, value: OperationInput) -> JsonObject:
    request = _typed(value, CompilerCandidateCompareInput)
    return forge.compiler_candidates_compare(request.left_candidate_id, request.right_candidate_id)


def _compiler_candidate_attach(forge: ForgeOperationTarget, value: OperationInput) -> JsonObject:
    request = _typed(value, CompilerCandidateAttachInput)
    return forge.compiler_candidate_attach_validation(
        request.candidate_id,
        validator_identity=request.validator_identity,
        judgement=request.judgement,
        claimed_relation=request.claimed_relation,
        counterexample=request.counterexample,
        limitations=request.limitations,
        stale=request.stale,
        expected_artifact_identity=request.expected_artifact_identity,
    )


def _compiler_tournament(forge: ForgeOperationTarget, value: OperationInput) -> JsonObject:
    request = _typed(value, CompilerTournamentInput)
    return forge.compiler_tournament(request.candidate_ids)


def _compiler_candidate_select(forge: ForgeOperationTarget, value: OperationInput) -> JsonObject:
    request = _typed(value, CompilerCandidateSelectInput)
    return forge.compiler_candidate_select(request.candidate_id, request.policy)


def _compiler_candidate_inspect(forge: ForgeOperationTarget, value: OperationInput) -> JsonObject:
    request = _typed(value, CompilerCandidateInspectInput)
    return forge.compiler_candidate_inspect(request.candidate_id)


def _ledger_verify(forge: ForgeOperationTarget, value: OperationInput) -> JsonObject:
    _no_input(value)
    return forge.ledger_verify()


def _config_validate(forge: ForgeOperationTarget, value: OperationInput) -> JsonObject:
    _no_input(value)
    return forge.config_validate()


def _operation_inventory(_forge: ForgeOperationTarget, value: OperationInput) -> JsonObject:
    _no_input(value)
    return DEFAULT_OPERATION_REGISTRY.inventory()


def _cli(*command: str, bindings: Sequence[CliBinding] = ()) -> CliExposure:
    return CliExposure(command=tuple(command), bindings=tuple(bindings))


def _mcp(tool_name: str, modes: frozenset[str] = ALL_MODES) -> McpExposure:
    return McpExposure(tool_name=tool_name, visible_modes=modes)


def _binding(
    input_name: str, namespace_name: str | None = None, decoder: CliDecoder = CliDecoder.VALUE
) -> CliBinding:
    return CliBinding(input_name, namespace_name or input_name, decoder)


def _operation(
    operation_id: str,
    *,
    modes: frozenset[str] = ALL_MODES,
    mutation: MutationClass = MutationClass.READ_ONLY,
    input_model: type[OperationInput] = NoInput,
    output: OutputContract,
    handler: OperationHandler,
    authority: AuthorityRequirement = AuthorityRequirement.NONE,
    lifecycle: LifecycleRequirement = LifecycleRequirement.NONE,
    disclosure: DisclosureClass = DisclosureClass.LOCAL_PROJECT,
    description: str,
    cli: CliExposure | None = None,
    mcp: McpExposure | None = None,
    resources: Sequence[ResourceExposure] = (),
    cli_exclusion: str | None = None,
    mcp_exclusion: str | None = None,
) -> OperationDefinition:
    return OperationDefinition(
        operation_id=operation_id,
        modes=modes,
        mutation=mutation,
        input_model=input_model,
        output=output,
        handler=handler,
        authority=authority,
        lifecycle=lifecycle,
        disclosure=disclosure,
        description=description,
        cli=cli,
        mcp=mcp,
        resources=tuple(resources),
        cli_exclusion=cli_exclusion,
        mcp_exclusion=mcp_exclusion,
    )


_OPERATIONS = (
    _operation(
        "project.doctor",
        output=OutputContract.DIAGNOSTIC,
        handler=_doctor,
        authority=AuthorityRequirement.LOCAL_CONFIGURATION,
        description="Inspect Forge configuration, commands, ledger health, and limitations.",
        cli=_cli("doctor"),
        mcp_exclusion="diagnostic CLI utility is not an MCP tool",
    ),
    _operation(
        "project.inspect",
        output=OutputContract.PROJECT,
        handler=_project_inspect,
        authority=AuthorityRequirement.LOCAL_CONFIGURATION,
        lifecycle=LifecycleRequirement.PROJECTION,
        description="Inspect configured authority, mode, active state, commands, and limitations.",
        cli=_cli("inspect"),
        mcp=_mcp("mncs_forge_project_inspect"),
        resources=(
            ResourceExposure("mncs-forge://project/authority-map"),
            ResourceExposure("mncs-forge://state/active-epoch", "current_epoch"),
            ResourceExposure("mncs-forge://state/active-candidate", "active_candidate"),
        ),
    ),
    _operation(
        "lifecycle.inspect",
        output=OutputContract.LIFECYCLE,
        handler=_state_inspect,
        lifecycle=LifecycleRequirement.PROJECTION,
        description="Explain the lifecycle stage, legal next operations, and stable blockers.",
        cli=_cli("state"),
        mcp=_mcp("mncs_forge_state_inspect"),
        resources=(ResourceExposure("mncs-forge://state/lifecycle"),),
    ),
    _operation(
        "claims.status",
        output=OutputContract.CLAIM,
        handler=_claim_status,
        description="Report separate MNCS, MNCDS, assurance, evidence, and promotion statuses.",
        cli=_cli("status"),
        mcp=_mcp("mncs_forge_claim_status"),
        resources=(),
        disclosure=DisclosureClass.PUBLIC_METADATA,
    ),
    _operation(
        "claims.blockers",
        input_model=ClaimBlockersInput,
        output=OutputContract.CLAIM,
        handler=_claim_blockers,
        description="Explain absent, failed, stale, conflicting, or unsupported claim evidence.",
        cli=_cli("blockers", bindings=(_binding("requested_claim", "claim"),)),
        mcp=_mcp("mncs_forge_claim_blockers"),
        resources=(ResourceExposure("mncs-forge://claims/blockers", "promotion"),),
    ),
    _operation(
        "providers.list",
        output=OutputContract.INVENTORY,
        handler=_provider_list,
        authority=AuthorityRequirement.DECLARED_PROVIDER,
        description=(
            "List configured providers, declared capabilities, availability, and last probes."
        ),
        cli=_cli("providers", "list"),
        mcp=_mcp("mncs_forge_providers_list"),
        resources=(ResourceExposure("mncs-forge://providers/configured"),),
    ),
    _operation(
        "providers.probe",
        modes=DEVELOPMENT_ONLY,
        mutation=MutationClass.MUTATING,
        input_model=ProviderProbeInput,
        output=OutputContract.RECORD,
        handler=_provider_probe,
        authority=AuthorityRequirement.DECLARED_PROVIDER,
        disclosure=DisclosureClass.DEVELOPMENT_EVIDENCE,
        description="Explicitly probe one provider using bounded Provider Protocol capabilities.",
        cli=_cli("providers", "probe", bindings=(_binding("provider_id"),)),
        mcp=_mcp("mncs_forge_provider_probe"),
    ),
    _operation(
        "providers.learned-shadow",
        input_model=LearnedSpecialistShadowInput,
        output=OutputContract.RECORD,
        handler=_learned_specialist_shadow,
        authority=AuthorityRequirement.DECLARED_PROVIDER,
        disclosure=DisclosureClass.DEVELOPMENT_EVIDENCE,
        description=(
            "Invoke an identity-bound MNEL specialist through the bounded provider boundary "
            "and record a non-authoritative Forge shadow observation."
        ),
        cli=_cli(
            "providers",
            "learned-shadow",
            bindings=(
                _binding("artifact_path", "artifact"),
                _binding("provider_command", "provider_command"),
                _binding("source_records", "source_records", CliDecoder.JSON_VALUE),
                _binding(
                    "context_observations",
                    "context_observations",
                    CliDecoder.JSON_VALUE,
                ),
                _binding("lineage_identity", "lineage"),
                _binding("timeout_seconds", "timeout"),
            ),
        ),
        mcp=_mcp("mncs_forge_learned_specialist_shadow"),
    ),
    _operation(
        "providers.capability-blockers",
        input_model=CapabilityBlockersInput,
        output=OutputContract.CLAIM,
        handler=_capability_blockers,
        authority=AuthorityRequirement.DECLARED_PROVIDER,
        description=(
            "Report UNKNOWN blockers for required capabilities not established by a current probe."
        ),
        cli=_cli(
            "providers", "blockers", bindings=(_binding("required_capabilities", "capabilities"),)
        ),
        mcp=_mcp("mncs_forge_capability_blockers"),
        resources=(ResourceExposure("mncs-forge://providers/capability-blockers"),),
    ),
    _operation(
        "verifiers.list",
        output=OutputContract.INVENTORY,
        handler=_verifier_list,
        authority=AuthorityRequirement.DECLARED_VERIFIER,
        description="List declared micro-verifiers without executing providers.",
        cli=_cli("verifier", "list"),
        mcp=_mcp("mncs_forge_verifier_list"),
        resources=(ResourceExposure("mncs-forge://verifiers/declared"),),
    ),
    _operation(
        "verifiers.describe",
        input_model=VerifierDescribeInput,
        output=OutputContract.INVENTORY,
        handler=_verifier_describe,
        authority=AuthorityRequirement.DECLARED_VERIFIER,
        description="Describe one declared micro-verifier and its bounded authority.",
        cli=_cli("verifier", "describe", bindings=(_binding("verifier_id"),)),
        mcp=_mcp("mncs_forge_verifier_describe"),
    ),
    _operation(
        "verifiers.match",
        input_model=VerifierMatchInput,
        output=OutputContract.INVENTORY,
        handler=_verifier_match,
        authority=AuthorityRequirement.DECLARED_VERIFIER,
        description="Deterministically match declared verifiers; never execute a match.",
        cli=_cli(
            "verifier",
            "match",
            bindings=(
                _binding("uncertainty_classes", "uncertainty"),
                _binding("language"),
                _binding("artifact_type"),
                _binding("changed_paths", "changed"),
                _binding("scope"),
                _binding("maximum_cost"),
                _binding("required_category", "category"),
                _binding("active_mode"),
            ),
        ),
        mcp=_mcp("mncs_forge_verifier_match"),
    ),
    _operation(
        "verifiers.run",
        mutation=MutationClass.MUTATING,
        input_model=VerifierRunInput,
        output=OutputContract.RECORD,
        handler=_verifier_run,
        authority=AuthorityRequirement.DECLARED_VERIFIER,
        lifecycle=LifecycleRequirement.VERIFIER_BINDINGS,
        disclosure=DisclosureClass.POLICY_CONTROLLED,
        description="Run one declared verifier through bounded Provider Protocol execution.",
        cli=_cli(
            "verifier",
            "run",
            bindings=(
                _binding("verifier_id"),
                _binding("candidate_identity", "candidate"),
                _binding("changed_paths", "changed"),
                _binding("scope"),
                _binding("source_region", decoder=CliDecoder.JSON_OBJECT),
                _binding("contract_identity", "contract"),
                _binding("dependency_slice_identities", "dependency", CliDecoder.DEPENDENCIES),
                _binding("prior_artifact_identity", "prior_artifact"),
                _binding("question_parameters", "parameters", CliDecoder.JSON_OBJECT),
            ),
        ),
        mcp=_mcp("mncs_forge_verifier_run"),
    ),
    _operation(
        "verifiers.batch",
        mutation=MutationClass.MUTATING,
        input_model=VerifierBatchInput,
        output=OutputContract.RESULT_SET,
        handler=_verifier_batch,
        authority=AuthorityRequirement.DECLARED_VERIFIER,
        lifecycle=LifecycleRequirement.VERIFIER_BINDINGS,
        disclosure=DisclosureClass.POLICY_CONTROLLED,
        description="Run an explicit bounded verifier batch and retain every result.",
        cli=_cli(
            "verifier",
            "batch",
            bindings=(
                _binding("verifier_ids"),
                _binding("candidate_identity", "candidate"),
                _binding("changed_paths", "changed"),
                _binding("scope"),
                _binding("source_region", decoder=CliDecoder.JSON_OBJECT),
                _binding("contract_identity", "contract"),
                _binding("dependency_slice_identities", "dependency", CliDecoder.DEPENDENCIES),
                _binding("prior_artifact_identity", "prior_artifact"),
                _binding("question_parameters", "parameters", CliDecoder.JSON_OBJECT),
            ),
        ),
        mcp=_mcp("mncs_forge_verifier_batch"),
    ),
    _operation(
        "verifiers.explain",
        input_model=VerifierExplainInput,
        output=OutputContract.EXPLANATION,
        handler=_verifier_explain,
        authority=AuthorityRequirement.DECLARED_VERIFIER,
        disclosure=DisclosureClass.POLICY_CONTROLLED,
        description="Explain one verifier result and its current freshness limitations.",
        cli=_cli("verifier", "explain", bindings=(_binding("output_identity"),)),
        mcp=_mcp("mncs_forge_verifier_explain"),
    ),
    _operation(
        "epochs.begin",
        modes=DEVELOPMENT_ONLY,
        mutation=MutationClass.MUTATING,
        input_model=EpochBeginInput,
        output=OutputContract.RECORD,
        handler=_epoch_begin,
        authority=AuthorityRequirement.DEVELOPMENT,
        lifecycle=LifecycleRequirement.PROJECTION,
        disclosure=DisclosureClass.DEVELOPMENT_EVIDENCE,
        description="Begin an append-only development epoch without modifying earlier epochs.",
        cli=_cli(
            "epoch",
            "begin",
            bindings=(
                _binding("generator_identity", "generator"),
                _binding("evaluator_identity", "evaluator"),
                _binding("parent_epoch", "parent"),
                _binding("authority_overlap"),
            ),
        ),
        mcp=_mcp("mncs_forge_epoch_begin"),
    ),
    _operation(
        "candidates.register",
        modes=DEVELOPMENT_ONLY,
        mutation=MutationClass.MUTATING,
        input_model=CandidateRegisterInput,
        output=OutputContract.RECORD,
        handler=_candidate_register,
        authority=AuthorityRequirement.DEVELOPMENT,
        lifecycle=LifecycleRequirement.ACTIVE_EPOCH,
        disclosure=DisclosureClass.DEVELOPMENT_EVIDENCE,
        description="Register candidate content and lineage within declared writable paths.",
        cli=_cli(
            "candidate",
            "register",
            bindings=(
                _binding("changed_files", "changed"),
                _binding("hypothesis"),
                _binding("generator_identity", "generator"),
                _binding("generator_config_identity", "generator_config"),
                _binding("parent_candidate", "parent"),
                _binding("expected_identity"),
            ),
        ),
        mcp=_mcp("mncs_forge_candidate_register"),
    ),
    _operation(
        "candidates.refresh",
        modes=DEVELOPMENT_ONLY,
        mutation=MutationClass.MUTATING,
        input_model=CandidateRefreshInput,
        output=OutputContract.RECORD,
        handler=_candidate_refresh,
        authority=AuthorityRequirement.DEVELOPMENT,
        lifecycle=LifecycleRequirement.CURRENT_CANDIDATE,
        disclosure=DisclosureClass.DEVELOPMENT_EVIDENCE,
        description=(
            "Rebind the active development candidate to current content without "
            "reusing prior evidence as if it were still current."
        ),
        cli=_cli(
            "candidate",
            "refresh",
            bindings=(
                _binding("hypothesis"),
                _binding("generator_identity", "generator"),
                _binding("generator_config_identity", "generator_config"),
                _binding("changed_files", "changed"),
            ),
        ),
        mcp=_mcp("mncs_forge_candidate_refresh"),
    ),
    _operation(
        "development.checks.run",
        modes=DEVELOPMENT_ONLY,
        mutation=MutationClass.MUTATING,
        input_model=DevelopmentChecksInput,
        output=OutputContract.RESULT_SET,
        handler=_development_checks,
        authority=AuthorityRequirement.DEVELOPMENT,
        lifecycle=LifecycleRequirement.CURRENT_CANDIDATE,
        disclosure=DisclosureClass.DEVELOPMENT_EVIDENCE,
        description="Run only declared development workflows with bounded execution.",
        cli=_cli(
            "check",
            "development",
            bindings=(
                _binding("workflow_names", "workflows"),
                _binding("candidate_identity", "candidate"),
            ),
        ),
        mcp=_mcp("mncs_forge_development_checks_run"),
    ),
    _operation(
        "development.failure.explain",
        input_model=FailureExplainInput,
        output=OutputContract.EXPLANATION,
        handler=_failure_explain,
        disclosure=DisclosureClass.POLICY_CONTROLLED,
        description="Return compact decision-oriented FAIL or UNKNOWN information.",
        cli=_cli("explain", bindings=(_binding("output_identity", "result"),)),
        mcp=_mcp("mncs_forge_failure_explain"),
    ),
    _operation(
        "development.mncs.failure-loop",
        modes=DEVELOPMENT_ONLY,
        mutation=MutationClass.MUTATING,
        input_model=MncsFailureLoopInput,
        output=OutputContract.RECORD,
        handler=_mncs_failure_loop,
        authority=AuthorityRequirement.DEVELOPMENT,
        disclosure=DisclosureClass.DEVELOPMENT_EVIDENCE,
        description=(
            "Run canonical mncs-test, consume its structured failure through mncs-debug, "
            "and optionally apply one exact candidate replacement before verification."
        ),
        cli=_cli(
            "mncs",
            "failure-loop",
            bindings=(
                _binding("manifest"),
                _binding("test_command", "test_command", CliDecoder.JSON_VALUE),
                _binding("debug_command", "debug_command", CliDecoder.JSON_VALUE),
                _binding("mncs_binary"),
                _binding("library_paths", "library_paths", CliDecoder.JSON_VALUE),
                _binding("embed_library"),
                _binding("working_directory", "working_directory"),
                _binding("test_result_file", "test_result_file"),
                _binding("test_check_file", "test_check_file"),
                _binding("test_artifacts_directory", "test_artifacts_directory"),
                _binding("debug_witness_file", "debug_witness_file"),
                _binding("debug_artifacts_directory", "debug_artifacts_directory"),
                _binding("capture_policy", "capture_policy"),
                _binding("max_events", "max_events"),
                _binding("timeout_seconds", "timeout_seconds"),
                _binding("minimize", "minimize"),
                _binding("test_id", "test_id"),
                _binding("repair_path", "repair_path"),
                _binding("repair_from", "repair_from"),
                _binding("repair_to", "repair_to"),
                _binding("output_file", "output_file"),
            ),
        ),
        mcp=_mcp("mncs_forge_mncs_failure_loop", DEVELOPMENT_ONLY),
    ),
    _operation(
        "candidates.compare",
        modes=DEVELOPMENT_ONLY,
        input_model=CandidateCompareInput,
        output=OutputContract.RESULT_SET,
        handler=_candidate_compare,
        authority=AuthorityRequirement.DEVELOPMENT,
        lifecycle=LifecycleRequirement.CURRENT_CANDIDATE,
        disclosure=DisclosureClass.DEVELOPMENT_EVIDENCE,
        description="Compare candidates under the predeclared selection policy.",
        cli=_cli(
            "candidate", "compare", bindings=(_binding("candidate_identities", "candidate_ids"),)
        ),
        mcp=_mcp("mncs_forge_candidate_compare"),
    ),
    _operation(
        "candidates.select",
        modes=DEVELOPMENT_ONLY,
        mutation=MutationClass.MUTATING,
        input_model=CandidateDispositionInput,
        output=OutputContract.RECORD,
        handler=_candidate_select,
        authority=AuthorityRequirement.DEVELOPMENT,
        lifecycle=LifecycleRequirement.REQUIRED_EVIDENCE,
        disclosure=DisclosureClass.DEVELOPMENT_EVIDENCE,
        description="Select a candidate only when required evidence is comparable PASS.",
        cli=_cli(
            "candidate",
            "select",
            bindings=(
                _binding("candidate_identity", "candidate_id"),
                _binding("reason"),
            ),
        ),
        mcp=_mcp("mncs_forge_candidate_select"),
    ),
    _operation(
        "candidates.reject",
        modes=DEVELOPMENT_ONLY,
        mutation=MutationClass.MUTATING,
        input_model=CandidateDispositionInput,
        output=OutputContract.RECORD,
        handler=_candidate_reject,
        authority=AuthorityRequirement.DEVELOPMENT,
        lifecycle=LifecycleRequirement.CURRENT_CANDIDATE,
        disclosure=DisclosureClass.DEVELOPMENT_EVIDENCE,
        description="Reject a candidate while retaining its immutable history.",
        cli=_cli(
            "candidate",
            "reject",
            bindings=(
                _binding("candidate_identity", "candidate_id"),
                _binding("reason"),
            ),
        ),
        mcp=_mcp("mncs_forge_candidate_reject"),
    ),
    _operation(
        "candidates.freeze",
        modes=DEVELOPMENT_ONLY,
        mutation=MutationClass.MUTATING,
        input_model=CandidateFreezeInput,
        output=OutputContract.RECORD,
        handler=_candidate_freeze,
        authority=AuthorityRequirement.DEVELOPMENT,
        lifecycle=LifecycleRequirement.SELECTED_CANDIDATE,
        disclosure=DisclosureClass.DEVELOPMENT_EVIDENCE,
        description="Freeze candidate and authority identities for evaluator mode.",
        cli=_cli(
            "freeze",
            bindings=(
                _binding("candidate_identity", "candidate_id"),
                _binding("environment_identity", "environment"),
                _binding("required_evidence_plan", "evidence_plan"),
            ),
        ),
        mcp=_mcp("mncs_forge_candidate_freeze"),
    ),
    _operation(
        "evaluation.final.run",
        modes=EVALUATOR_ONLY,
        mutation=MutationClass.MUTATING,
        input_model=FinalEvaluationInput,
        output=OutputContract.RESULT_SET,
        handler=_final_evaluation,
        authority=AuthorityRequirement.EVALUATOR,
        lifecycle=LifecycleRequirement.VALID_FREEZE,
        disclosure=DisclosureClass.EVALUATOR_STATUS_ONLY,
        description="Run frozen evaluator workflows without repair feedback.",
        cli=_cli("evaluate", bindings=(_binding("workflow_names", "workflows"),)),
        mcp=_mcp("mncs_forge_final_evaluation_run", EVALUATOR_ONLY),
    ),
    _operation(
        "evidence.reconcile",
        input_model=EvidenceReconcileInput,
        output=OutputContract.RECONCILIATION,
        handler=_evidence_reconcile,
        lifecycle=LifecycleRequirement.RECONCILABLE_HISTORY,
        description="Aggregate validated local evidence with FAIL > UNKNOWN > PASS.",
        cli=_cli("reconcile", bindings=(_binding("candidate_identity", "candidate"),)),
        mcp=_mcp("mncs_forge_evidence_reconcile"),
        resources=(ResourceExposure("mncs-forge://evidence/latest-summary"),),
    ),
    _operation(
        "bundles.build",
        mutation=MutationClass.MUTATING,
        input_model=BundleBuildInput,
        output=OutputContract.BUNDLE,
        handler=_bundle_build,
        authority=AuthorityRequirement.PUBLIC_VALIDATOR,
        lifecycle=LifecycleRequirement.BUNDLE_ELIGIBLE,
        disclosure=DisclosureClass.POLICY_CONTROLLED,
        description="Orchestrate a declared public MNCS/MNCDS package workflow.",
        cli=_cli(
            "bundle",
            bindings=(
                _binding("workflow_name", "workflow"),
                _binding("candidate_identity", "candidate"),
            ),
        ),
        mcp=_mcp("mncs_forge_bundle_build"),
    ),
    _operation(
        "compiler.experiments.record",
        modes=DEVELOPMENT_ONLY,
        mutation=MutationClass.MUTATING,
        input_model=CompilerExperimentRecordInput,
        output=OutputContract.RECORD,
        handler=_compiler_experiment_record,
        authority=AuthorityRequirement.DEVELOPMENT,
        disclosure=DisclosureClass.DEVELOPMENT_EVIDENCE,
        description=(
            "Persist one language-owned compiler study as observation-only evolution evidence."
        ),
        cli=_cli(
            "compiler",
            "record",
            bindings=(_binding("language_record", "record", CliDecoder.JSON_OBJECT),),
        ),
        mcp=_mcp("mncs_forge_compiler_experiment_record", DEVELOPMENT_ONLY),
    ),
    _operation(
        "compiler.experiments.list",
        input_model=NoInput,
        output=OutputContract.INVENTORY,
        handler=_compiler_experiments_list,
        authority=AuthorityRequirement.LOCAL_STORAGE,
        lifecycle=LifecycleRequirement.PROJECTION,
        disclosure=DisclosureClass.DEVELOPMENT_EVIDENCE,
        description="List persisted compiler-study observations without creating a verdict.",
        cli=_cli("compiler", "list"),
        mcp=_mcp("mncs_forge_compiler_experiments_list"),
        resources=(ResourceExposure("mncs-forge://compiler/experiments"),),
    ),
    _operation(
        "compiler.experiments.compare",
        input_model=CompilerExperimentCompareInput,
        output=OutputContract.RESULT_SET,
        handler=_compiler_experiments_compare,
        authority=AuthorityRequirement.LOCAL_STORAGE,
        lifecycle=LifecycleRequirement.PROJECTION,
        disclosure=DisclosureClass.DEVELOPMENT_EVIDENCE,
        description=(
            "Compare persisted compiler stages and localize the earliest observed difference."
        ),
        cli=_cli(
            "compiler",
            "compare",
            bindings=(
                _binding("left_experiment_id", "left"),
                _binding("right_experiment_id", "right"),
            ),
        ),
        mcp=_mcp("mncs_forge_compiler_experiments_compare"),
    ),
    _operation(
        "concept.evaluations.record",
        modes=DEVELOPMENT_ONLY,
        mutation=MutationClass.MUTATING,
        input_model=ConceptEvaluationRecordInput,
        output=OutputContract.RECORD,
        handler=_concept_evaluation_record,
        authority=AuthorityRequirement.DEVELOPMENT,
        disclosure=DisclosureClass.DEVELOPMENT_EVIDENCE,
        description=(
            "Persist one bounded Forge concept evaluation for a Concept Experiment "
            "without candidate self-certification or conformance authority."
        ),
        cli=_cli(
            "evaluations",
            "record",
            bindings=(_binding("evaluation", "evaluation", CliDecoder.JSON_OBJECT),),
        ),
        mcp=_mcp("mncs_forge_concept_evaluation_record", DEVELOPMENT_ONLY),
    ),
    _operation(
        "concept.evaluations.list",
        input_model=NoInput,
        output=OutputContract.INVENTORY,
        handler=_concept_evaluations_list,
        authority=AuthorityRequirement.LOCAL_STORAGE,
        lifecycle=LifecycleRequirement.PROJECTION,
        disclosure=DisclosureClass.DEVELOPMENT_EVIDENCE,
        description="List persisted concept evaluations without strengthening any status.",
        cli=_cli("evaluations", "list"),
        mcp=_mcp("mncs_forge_concept_evaluations_list"),
        resources=(ResourceExposure("mncs-forge://concept/evaluations"),),
    ),
    _operation(
        "concept.evaluations.get",
        input_model=ConceptEvaluationGetInput,
        output=OutputContract.RECORD,
        handler=_concept_evaluation_get,
        authority=AuthorityRequirement.LOCAL_STORAGE,
        lifecycle=LifecycleRequirement.PROJECTION,
        disclosure=DisclosureClass.DEVELOPMENT_EVIDENCE,
        description="Fetch one persisted concept evaluation by record id, digest, or stable id.",
        cli=_cli(
            "evaluations",
            "get",
            bindings=(_binding("evaluation_id", "id"),),
        ),
        mcp=_mcp("mncs_forge_concept_evaluation_get"),
    ),
    _operation(
        "compiler.candidates.register",
        modes=DEVELOPMENT_ONLY,
        mutation=MutationClass.MUTATING,
        input_model=CompilerCandidateRegisterInput,
        output=OutputContract.RECORD,
        handler=_compiler_candidate_register,
        authority=AuthorityRequirement.DEVELOPMENT,
        disclosure=DisclosureClass.DEVELOPMENT_EVIDENCE,
        description=(
            "Register an isolated compiler-search candidate without treating "
            "generation as validity."
        ),
        cli=_cli(
            "compiler",
            "candidate-register",
            bindings=(
                _binding("baseline_artifact_identity", "baseline"),
                _binding("candidate_artifact_identity", "candidate_artifact"),
                _binding("generator_identity", "generator"),
                _binding("declared_transformation", "transformation"),
                _binding("claimed_relation", "relation"),
                _binding("expected_benefit", "benefit"),
                _binding("protected_properties", "protected"),
                _binding("target_envelope", "target"),
                _binding("required_validation", "required_validation"),
            ),
        ),
        mcp=_mcp("mncs_forge_compiler_candidate_register", DEVELOPMENT_ONLY),
    ),
    _operation(
        "compiler.candidates.list",
        input_model=NoInput,
        output=OutputContract.INVENTORY,
        handler=_compiler_candidates_list,
        authority=AuthorityRequirement.LOCAL_STORAGE,
        lifecycle=LifecycleRequirement.PROJECTION,
        disclosure=DisclosureClass.DEVELOPMENT_EVIDENCE,
        description="List isolated compiler-search candidates without creating a verdict.",
        cli=_cli("compiler", "candidate-list"),
        mcp=_mcp("mncs_forge_compiler_candidates_list"),
        resources=(ResourceExposure("mncs-forge://compiler/candidates"),),
    ),
    _operation(
        "compiler.candidates.compare",
        input_model=CompilerCandidateCompareInput,
        output=OutputContract.RESULT_SET,
        handler=_compiler_candidates_compare,
        authority=AuthorityRequirement.LOCAL_STORAGE,
        lifecycle=LifecycleRequirement.PROJECTION,
        disclosure=DisclosureClass.DEVELOPMENT_EVIDENCE,
        description="Compare two compiler-search candidates without promoting either.",
        cli=_cli(
            "compiler",
            "candidate-compare",
            bindings=(
                _binding("left_candidate_id", "left"),
                _binding("right_candidate_id", "right"),
            ),
        ),
        mcp=_mcp("mncs_forge_compiler_candidates_compare"),
    ),
    _operation(
        "compiler.candidates.attach-validation",
        modes=DEVELOPMENT_ONLY,
        mutation=MutationClass.MUTATING,
        input_model=CompilerCandidateAttachInput,
        output=OutputContract.RECORD,
        handler=_compiler_candidate_attach,
        authority=AuthorityRequirement.DEVELOPMENT,
        disclosure=DisclosureClass.DEVELOPMENT_EVIDENCE,
        description=("Attach an independent PASS/FAIL/UNKNOWN validation to a compiler candidate."),
        cli=_cli(
            "compiler",
            "candidate-attach",
            bindings=(
                _binding("candidate_id"),
                _binding("validator_identity", "validator"),
                _binding("judgement"),
                _binding("claimed_relation", "relation"),
                _binding("counterexample", "counterexample", CliDecoder.JSON_OBJECT),
                _binding("limitations"),
                _binding("stale"),
                _binding("expected_artifact_identity", "expected_artifact"),
            ),
        ),
        mcp=_mcp("mncs_forge_compiler_candidate_attach", DEVELOPMENT_ONLY),
    ),
    _operation(
        "compiler.tournament.run",
        modes=DEVELOPMENT_ONLY,
        mutation=MutationClass.READ_ONLY,
        input_model=CompilerTournamentInput,
        output=OutputContract.RESULT_SET,
        handler=_compiler_tournament,
        authority=AuthorityRequirement.DEVELOPMENT,
        disclosure=DisclosureClass.DEVELOPMENT_EVIDENCE,
        description=(
            "Rank isolated compiler candidates; FAIL loses and UNKNOWN cannot be promoted."
        ),
        cli=_cli(
            "compiler",
            "tournament",
            bindings=(_binding("candidate_ids", "candidates"),),
        ),
        mcp=_mcp("mncs_forge_compiler_tournament", DEVELOPMENT_ONLY),
    ),
    _operation(
        "compiler.candidates.select",
        modes=DEVELOPMENT_ONLY,
        mutation=MutationClass.READ_ONLY,
        input_model=CompilerCandidateSelectInput,
        output=OutputContract.RESULT_SET,
        handler=_compiler_candidate_select,
        authority=AuthorityRequirement.DEVELOPMENT,
        disclosure=DisclosureClass.DEVELOPMENT_EVIDENCE,
        description="Select a compiler candidate only under an explicit protected-property policy.",
        cli=_cli(
            "compiler",
            "candidate-select",
            bindings=(
                _binding("candidate_id"),
                _binding("policy"),
            ),
        ),
        mcp=_mcp("mncs_forge_compiler_candidate_select", DEVELOPMENT_ONLY),
    ),
    _operation(
        "compiler.candidates.inspect",
        input_model=CompilerCandidateInspectInput,
        output=OutputContract.RECORD,
        handler=_compiler_candidate_inspect,
        authority=AuthorityRequirement.LOCAL_STORAGE,
        lifecycle=LifecycleRequirement.PROJECTION,
        disclosure=DisclosureClass.DEVELOPMENT_EVIDENCE,
        description="Inspect unresolved compiler-candidate validation obligations.",
        cli=_cli(
            "compiler",
            "candidate-inspect",
            bindings=(_binding("candidate_id"),),
        ),
        mcp=_mcp("mncs_forge_compiler_candidate_inspect"),
    ),
    _operation(
        "execution.receipts.list",
        input_model=ExecutionReceiptListInput,
        output=OutputContract.INVENTORY,
        handler=_execution_receipts_list,
        authority=AuthorityRequirement.LOCAL_STORAGE,
        lifecycle=LifecycleRequirement.PROJECTION,
        description=(
            "List identity-bound execution-receipt bindings without treating them as evidence PASS."
        ),
        cli=_cli(
            "receipts",
            "list",
            bindings=(
                _binding("candidate_identity", "candidate"),
                _binding("action_identity", "action"),
            ),
        ),
        mcp=_mcp("mncs_forge_execution_receipts_list"),
        resources=(ResourceExposure("mncs-forge://execution/receipts"),),
    ),
    _operation(
        "rights.license-evidence.scan",
        input_model=NoInput,
        output=OutputContract.RECORD,
        handler=_license_evidence_scan,
        authority=AuthorityRequirement.LOCAL_STORAGE,
        lifecycle=LifecycleRequirement.PROJECTION,
        disclosure=DisclosureClass.DEVELOPMENT_EVIDENCE,
        description=(
            "Scan project license declarations into a rights/provenance evidence "
            "record. Unknown states are explicit; this is not legal review."
        ),
        cli=_cli(
            "license-evidence",
            "scan",
        ),
        mcp=_mcp("mncs_forge_license_evidence_scan"),
    ),
    _operation(
        "execution.receipts.get",
        input_model=ExecutionReceiptGetInput,
        output=OutputContract.RECORD,
        handler=_execution_receipts_get,
        authority=AuthorityRequirement.LOCAL_STORAGE,
        lifecycle=LifecycleRequirement.PROJECTION,
        description=(
            "Read one persisted execution-receipt binding and its referenced MNCS envelope."
        ),
        cli=_cli(
            "receipts",
            "get",
            bindings=(_binding("binding_id"),),
        ),
        mcp=_mcp("mncs_forge_execution_receipts_get"),
    ),
    _operation(
        "execution.assurance.assess",
        modes=DEVELOPMENT_ONLY,
        mutation=MutationClass.MUTATING,
        input_model=AssuranceAssessInput,
        output=OutputContract.RECORD,
        handler=_execution_assurance_assess,
        authority=AuthorityRequirement.DEVELOPMENT,
        lifecycle=LifecycleRequirement.PROJECTION,
        disclosure=DisclosureClass.DEVELOPMENT_EVIDENCE,
        description=(
            "Assess requested execution-assurance properties for one receipt binding "
            "fail-closed; a functional result never implies assurance."
        ),
        cli=_cli(
            "assessments",
            "request",
            bindings=(
                _binding("binding_id", "binding"),
                _binding("requested_properties", "requested"),
                _binding("policy_identity", "policy"),
            ),
        ),
        mcp=_mcp("mncs_forge_execution_assurance_assess", DEVELOPMENT_ONLY),
    ),
    _operation(
        "execution.assurance.list",
        input_model=AssuranceListInput,
        output=OutputContract.INVENTORY,
        handler=_execution_assurance_list,
        authority=AuthorityRequirement.LOCAL_STORAGE,
        lifecycle=LifecycleRequirement.PROJECTION,
        description=(
            "List persisted execution-assurance assessments with explicit disagreement retention."
        ),
        cli=_cli(
            "assessments",
            "list",
            bindings=(
                _binding("binding_identity", "binding"),
                _binding("candidate_identity", "candidate"),
            ),
        ),
        mcp=_mcp("mncs_forge_execution_assurance_list"),
        resources=(ResourceExposure("mncs-forge://execution/assessments"),),
    ),
    _operation(
        "cell.documents.validate",
        input_model=CellDocumentValidateInput,
        output=OutputContract.DIAGNOSTIC,
        handler=_cell_document_validate,
        authority=AuthorityRequirement.NONE,
        lifecycle=LifecycleRequirement.NONE,
        disclosure=DisclosureClass.PUBLIC_METADATA,
        description=(
            "Validate an inline Forge Cell policy, test-bundle, or execution-record "
            "document against its packaged schema without executing anything."
        ),
        cli=_cli(
            "cell",
            "validate",
            bindings=(
                _binding("kind"),
                _binding("document", CliDecoder.JSON_OBJECT),
            ),
        ),
        mcp=_mcp("mncs_forge_cell_document_validate"),
    ),
    _operation(
        "cell.execution.assess",
        input_model=CellExecutionAssessInput,
        output=OutputContract.RECORD,
        handler=_cell_execution_assess,
        authority=AuthorityRequirement.NONE,
        lifecycle=LifecycleRequirement.NONE,
        disclosure=DisclosureClass.PUBLIC_METADATA,
        description=(
            "Assess one inline Forge Cell execution record against one inline policy "
            "fail-closed, keeping assurance separate from any test result."
        ),
        cli=_cli(
            "cell",
            "assess",
            bindings=(
                _binding("policy", CliDecoder.JSON_OBJECT),
                _binding("record", CliDecoder.JSON_OBJECT),
                _binding("expected_nonce", "nonce"),
            ),
        ),
        mcp=_mcp("mncs_forge_cell_execution_assess"),
    ),
    _operation(
        "ledger.verify",
        output=OutputContract.DIAGNOSTIC,
        handler=_ledger_verify,
        authority=AuthorityRequirement.LOCAL_STORAGE,
        description="Verify the local hash-linked ledger and immutable record companions.",
        cli=_cli("ledger", "verify"),
        mcp_exclusion="direct local storage diagnostic is intentionally CLI-only",
    ),
    _operation(
        "config.validate",
        output=OutputContract.DIAGNOSTIC,
        handler=_config_validate,
        authority=AuthorityRequirement.LOCAL_CONFIGURATION,
        description="Validate the loaded Forge configuration and project root.",
        cli=_cli("config", "validate"),
        mcp_exclusion="startup already validates configuration; utility remains CLI-only",
    ),
    _operation(
        "operations.inventory",
        output=OutputContract.INVENTORY,
        handler=_operation_inventory,
        disclosure=DisclosureClass.PUBLIC_METADATA,
        description="Return the deterministic public operation and authority inventory.",
        cli=_cli("operations"),
        resources=(ResourceExposure("mncs-forge://operations"),),
        mcp_exclusion="inventory is exposed as an MCP resource rather than an executable tool",
    ),
)


def _matches(value: object, annotation: object) -> bool:
    if annotation is object:
        return True
    origin = get_origin(annotation)
    if origin in {Union, UnionType}:
        return any(_matches(value, item) for item in get_args(annotation))
    if origin is list:
        arguments = get_args(annotation)
        return isinstance(value, list) and (
            not arguments or all(_matches(item, arguments[0]) for item in value)
        )
    if origin is dict:
        arguments = get_args(annotation)
        return isinstance(value, dict) and (
            not arguments
            or all(
                _matches(key, arguments[0]) and _matches(item, arguments[1])
                for key, item in value.items()
            )
        )
    return isinstance(annotation, type) and isinstance(value, annotation)


def _build_input(
    input_model: type[OperationInput], payload: Mapping[str, object]
) -> OperationInput:
    model_fields = {item.name: item for item in fields(input_model)}
    unexpected = sorted(set(payload) - set(model_fields))
    if unexpected:
        raise ForgeError("OPERATION_INPUT", f"unexpected operation inputs: {unexpected}")
    missing = sorted(
        name
        for name, item in model_fields.items()
        if name not in payload and item.default is MISSING and item.default_factory is MISSING
    )
    if missing:
        raise ForgeError("OPERATION_INPUT", f"missing operation inputs: {missing}")
    hints = get_type_hints(input_model)
    invalid = sorted(
        name for name, value in payload.items() if not _matches(value, hints.get(name, object))
    )
    if invalid:
        raise ForgeError("OPERATION_INPUT", f"invalid operation input types: {invalid}")
    try:
        return input_model(**payload)
    except TypeError as exc:  # pragma: no cover - guarded above
        raise ForgeError("OPERATION_INPUT", str(exc)) from exc


class OperationRegistry:
    """Validated deterministic registry and the single interface invocation gate."""

    def __init__(self, operations: Sequence[OperationDefinition]) -> None:
        registration_order = tuple(operations)
        ordered = tuple(sorted(registration_order, key=lambda item: item.operation_id))
        self._registration_order = registration_order
        self._operations = ordered
        self._by_id = {item.operation_id: item for item in ordered}
        self._validate()

    @property
    def operations(self) -> tuple[OperationDefinition, ...]:
        return self._operations

    def _validate(self) -> None:
        if len(self._by_id) != len(self._operations):
            raise ValueError("duplicate canonical operation ID")
        cli_names: set[tuple[str, ...]] = set()
        mcp_names: set[str] = set()
        resource_names: set[str] = set()
        for operation in self._operations:
            if re.fullmatch(r"[a-z][a-z0-9.-]*", operation.operation_id) is None:
                raise ValueError(f"invalid canonical operation ID: {operation.operation_id}")
            if not operation.modes or not operation.modes <= ALL_MODES:
                raise ValueError(f"invalid modes for {operation.operation_id}")
            if not dataclasses.is_dataclass(operation.input_model):
                raise ValueError(f"input model is not a dataclass: {operation.operation_id}")
            if not issubclass(operation.input_model, OperationInput):
                raise ValueError(f"input model has wrong base: {operation.operation_id}")
            dataclass_parameters = operation.input_model.__dataclass_params__  # type: ignore[attr-defined]
            if not dataclass_parameters.frozen:
                raise ValueError(f"input model is not frozen: {operation.operation_id}")
            if not callable(operation.handler):
                raise ValueError(f"handler is not callable: {operation.operation_id}")
            if not operation.description.strip():
                raise ValueError(f"operation description is empty: {operation.operation_id}")
            if operation.mutation is MutationClass.MUTATING and operation.authority in {
                AuthorityRequirement.NONE,
                AuthorityRequirement.LOCAL_CONFIGURATION,
                AuthorityRequirement.LOCAL_STORAGE,
            }:
                raise ValueError(
                    f"mutating operation lacks authority metadata: {operation.operation_id}"
                )
            if operation.cli is None and not operation.cli_exclusion:
                raise ValueError(f"CLI asymmetry is undocumented: {operation.operation_id}")
            if operation.mcp is None and not operation.mcp_exclusion:
                raise ValueError(f"MCP asymmetry is undocumented: {operation.operation_id}")
            if operation.cli is not None:
                if not operation.cli.command or operation.cli.command in cli_names:
                    raise ValueError(f"duplicate or empty CLI command: {operation.operation_id}")
                cli_names.add(operation.cli.command)
                input_names = {item.name for item in fields(operation.input_model)}
                bound_names = {item.input_name for item in operation.cli.bindings}
                if input_names != bound_names:
                    raise ValueError(f"incomplete CLI input bindings: {operation.operation_id}")
            if operation.mcp is not None:
                if operation.mcp.tool_name in mcp_names:
                    raise ValueError(f"duplicate MCP tool name: {operation.mcp.tool_name}")
                if not operation.mcp.visible_modes <= ALL_MODES:
                    raise ValueError(f"invalid MCP visible modes: {operation.operation_id}")
                if (
                    operation.modes == EVALUATOR_ONLY
                    and operation.mcp.visible_modes != EVALUATOR_ONLY
                ):
                    raise ValueError(
                        f"evaluator-only tool visibility is unsafe: {operation.operation_id}"
                    )
                mcp_names.add(operation.mcp.tool_name)
            for resource in operation.resources:
                if resource.uri in resource_names:
                    raise ValueError(f"duplicate MCP resource URI: {resource.uri}")
                resource_names.add(resource.uri)

    def resolve(self, operation_id: str) -> OperationDefinition:
        try:
            return self._by_id[operation_id]
        except KeyError as exc:
            raise ForgeError("OPERATION_NOT_FOUND", f"unknown operation: {operation_id}") from exc

    def for_cli(self) -> tuple[OperationDefinition, ...]:
        return tuple(item for item in self._operations if item.cli is not None)

    def for_mcp(self, mode: str) -> tuple[OperationDefinition, ...]:
        return tuple(
            item
            for item in self._registration_order
            if item.mcp is not None and mode in item.mcp.visible_modes
        )

    def invoke(
        self,
        forge: ForgeOperationTarget,
        operation_id: str,
        payload: Mapping[str, object] | None = None,
        *,
        interface: OperationInterface = OperationInterface.INTERNAL,
        resource_uri: str | None = None,
    ) -> JsonObject:
        operation = self.resolve(operation_id)
        if interface is OperationInterface.CLI and operation.cli is None:
            raise ForgeError("OPERATION_NOT_EXPOSED", f"{operation_id} is not exposed through CLI")
        if interface is OperationInterface.MCP and (
            operation.mcp is None or forge.mode not in operation.mcp.visible_modes
        ):
            raise ForgeError("OPERATION_NOT_EXPOSED", f"{operation_id} is not visible in MCP")
        if interface is OperationInterface.RESOURCE:
            known_resources = {item.uri for item in operation.resources}
            if resource_uri is None or resource_uri not in known_resources:
                raise ForgeError(
                    "OPERATION_NOT_EXPOSED", f"{operation_id} is not exposed by that resource"
                )
        if forge.mode not in operation.modes:
            expected = " or ".join(sorted(operation.modes))
            raise ForgeError(
                "MODE_FORBIDDEN",
                f"operation {operation_id} requires {expected} mode; current mode is {forge.mode}",
            )
        request = _build_input(operation.input_model, payload or {})
        result = operation.handler(forge, request)
        if not isinstance(result, dict):  # pragma: no cover - typed handler contract
            raise ForgeError("OPERATION_OUTPUT", f"{operation_id} did not return a JSON object")
        return result

    def inventory(self) -> JsonObject:
        return {
            "schema_version": "1",
            "operations": [item.inventory() for item in self._operations],
        }


DEFAULT_OPERATION_REGISTRY = OperationRegistry(_OPERATIONS)


def canonical_operation_inventory() -> JsonObject:
    """Return deterministic JSON-compatible semantic metadata for compatibility auditing."""

    return DEFAULT_OPERATION_REGISTRY.inventory()
