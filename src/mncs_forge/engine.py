"""Compatibility composition facade shared by the CLI and MCP interfaces."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from pathlib import Path

from .adapters import LocalProjectObserver, build_runner
from .application.assurance import assess_execution_receipt, list_assessments
from .application.candidates import CandidateService
from .application.compiler_candidates import CompilerCandidateService
from .application.compiler_studies import CompilerEvolutionService
from .application.concept_evaluations import ConceptEvaluationService
from .application.evaluation import EvaluationService
from .application.evidence import EvidenceService
from .application.execution_receipts import get_binding, list_bindings
from .application.license_evidence import scan_license_evidence as _scan_license_evidence
from .application.lifecycle import LifecycleContext
from .application.mncs_development import MncsDevelopmentService
from .application.project import ProjectService
from .application.providers import ProviderService
from .application.recovery import RecoveryService
from .application.support import aggregate_status as _aggregate_status
from .application.support import now, redact
from .application.workflows import DevelopmentWorkflowService, WorkflowExecutor
from .config import ForgeConfig, Provider
from .errors import ForgeError
from .forge_cell import (
    ForgeCellValidationError,
    validate_forge_cell_document,
)
from .forge_cell import (
    assess_execution_assurance as assess_cell_execution_assurance,
)
from .learned_specialists import invoke_shadow_provider, read_artifact
from .micro_verifiers import MicroVerifierService
from .mncs_native import NativeForgeAdapter
from .record_store import RecordStore
from .records import ForgeRecord, LedgerEntry
from .state_machine import ForgeStateMachine
from .store_record_store import StoreBackedRecordStore

_redact = redact
_now = now


def aggregate_status(statuses: Iterable[str]) -> str:
    """Retain the public status helper while application logic uses shared support."""

    return _aggregate_status(statuses)


class Forge:
    """Stable public facade that composes and delegates to explicit application services."""

    def __init__(
        self,
        config: ForgeConfig,
        mode: str = "development",
        *,
        record_store: RecordStore | None = None,
    ) -> None:
        if mode not in {"development", "evaluator"}:
            raise ForgeError("INVALID_MODE", "mode must be development or evaluator")
        self.config = config
        self.mode = mode

        # Intentional public compatibility attributes used by CLI diagnostics and callers.
        self.record_store = record_store or StoreBackedRecordStore(config.state_dir)
        # ``ledger`` is retained as the public Forge reader name, but the
        # normal implementation is now the Store-backed projection.  The
        # old JSONL Ledger is used only by explicit differential/migration
        # callers and is never constructed on this path.
        self.ledger = getattr(self.record_store, "ledger", self.record_store)

        self._executor = build_runner(config)
        self._observer = LocalProjectObserver(config)
        native = NativeForgeAdapter(config.root)
        native_mode = config.native_execution_mode
        if native_mode == "required":
            native.ensure_available()
        native_selected = native_mode == "required" or (
            native_mode == "prefer" and bool(native.status(native_mode).get("selected"))
        )
        self._native = native if native_selected else None
        self._lifecycle = LifecycleContext(
            mode=mode,
            records=self.ledger,
            observer=self._observer,
            native=self._native,
            native_mode=native_mode,
            root=config.root,
        )
        RecoveryService(records=self.ledger, record_store=self.record_store).recover(
            recover_storage=record_store is not None
            or isinstance(self.record_store, StoreBackedRecordStore)
        )

        self._workflow_executor = WorkflowExecutor(
            config=config,
            mode=mode,
            executor=self._executor,
            observer=self._observer,
        )
        self._mncs_development_service = MncsDevelopmentService(
            config=config,
            executor=self._executor,
            native=self._native,
            native_mode=native_mode,
        )
        self._development_service = DevelopmentWorkflowService(
            config=config,
            mode=mode,
            records=self.ledger,
            record_store=self.record_store,
            lifecycle=self._lifecycle,
            workflows=self._workflow_executor,
        )
        self._provider_service = ProviderService(
            config=config,
            mode=mode,
            records=self.ledger,
            record_store=self.record_store,
            executor=self._executor,
            observer=self._observer,
        )
        self._verifier_service = MicroVerifierService(
            config=config,
            mode=mode,
            records=self.ledger,
            record_store=self.record_store,
            lifecycle=self._lifecycle,
            observer=self._observer,
            executor=self._executor,
        )
        self._candidate_service = CandidateService(
            config=config,
            observer=self._observer,
            record_store=self.record_store,
            lifecycle=self._lifecycle,
            development=self._development_service,
        )
        self._evaluation_service = EvaluationService(
            config=config,
            observer=self._observer,
            record_store=self.record_store,
            lifecycle=self._lifecycle,
            workflows=self._workflow_executor,
        )
        self._evidence_service = EvidenceService(
            config=config,
            mode=mode,
            record_store=self.record_store,
            lifecycle=self._lifecycle,
            development=self._development_service,
            workflows=self._workflow_executor,
        )
        self._project_service = ProjectService(
            config=config,
            mode=mode,
            records=self.ledger,
            executor=self._executor,
            observer=self._observer,
            lifecycle=self._lifecycle,
            providers=self._provider_service,
            verifiers=self._verifier_service,
        )
        self._compiler_evolution_service = CompilerEvolutionService(
            records=self.ledger,
            record_store=self.record_store,
        )
        self._compiler_candidate_service = CompilerCandidateService(
            records=self.ledger,
            record_store=self.record_store,
        )
        self._concept_evaluation_service = ConceptEvaluationService(
            records=self.ledger,
            record_store=self.record_store,
        )

    # Compatibility observation helpers. Implementations live in typed collaborators.
    def _records(self, kind: str) -> list[LedgerEntry]:
        return self._lifecycle.records_of(kind)

    def _record_by_id(self, kind: str, identity: str, key: str) -> ForgeRecord:
        return self._lifecycle.record_by_id(kind, identity, key)

    def _current_candidate_identity(self) -> str:
        return self._observer.current_candidate_identity()

    def _current_authority_identities(self) -> dict[str, str]:
        return self._observer.current_authority_identities()

    def _current_freeze_bindings(
        self,
        candidate_identity: str | None = None,
        freeze: Mapping[str, object] | None = None,
    ) -> dict[str, str]:
        return self._observer.current_freeze_bindings(candidate_identity, freeze)

    def _provider_executable(self, provider: Provider) -> tuple[Path, str]:
        return self._observer.provider_executable(provider)

    def _provider_workspace(self, *, evaluator: bool = False):  # type: ignore[no-untyped-def]
        return self._observer.provider_workspace(evaluator=evaluator)

    def _state_machine(
        self,
        *,
        observe_epoch_authority: bool = True,
        observe_freeze_bindings: bool = True,
        observe_policy: bool = True,
        history_kinds: frozenset[str] | None = None,
    ) -> ForgeStateMachine:
        return self._lifecycle.machine(
            observe_epoch_authority=observe_epoch_authority,
            observe_freeze_bindings=observe_freeze_bindings,
            observe_policy=observe_policy,
            history_kinds=history_kinds,
        )

    def _verify_freeze(self, freeze: Mapping[str, object]) -> None:
        self._lifecycle.verify_freeze(freeze)

    def _result_records(self, candidate_id: str | None = None) -> list[ForgeRecord]:
        return self._development_service.result_records(candidate_id)

    # Project, state, and provider capabilities.
    def doctor(self) -> dict[str, object]:
        return self._project_service.doctor()

    def project_inspect(self) -> dict[str, object]:
        return self._project_service.inspect()

    def state_inspect(self) -> dict[str, object]:
        return self._project_service.state_inspect()

    def config_validate(self) -> dict[str, object]:
        """Retain the CLI configuration diagnostic behind the facade boundary."""

        return {
            "ok": True,
            "config": str(self.config.config_path),
            "project_root": str(self.config.root),
        }

    def ledger_verify(self) -> dict[str, object]:
        """Retain direct local ledger verification as an intentional CLI utility."""

        return self.ledger.verify()

    def license_evidence_scan(self) -> dict[str, object]:
        """Scan project license declarations into a rights evidence record."""
        return _scan_license_evidence(self.config)

    def provider_list(self) -> dict[str, object]:
        return self._provider_service.inventory()

    def provider_probe(self, provider_id: str) -> dict[str, object]:
        return self._provider_service.probe(provider_id)

    def learned_specialist_shadow(
        self,
        artifact_path: str,
        provider_command: list[str],
        source_records: list[dict[str, object]],
        *,
        context_observations: list[dict[str, object]] | None = None,
        lineage_identity: str | None = None,
        timeout_seconds: float = 5.0,
    ) -> dict[str, object]:
        """Run the learned specialist as a bounded, non-authoritative shadow."""

        artifact = read_artifact(artifact_path)
        return invoke_shadow_provider(
            provider_command,
            artifact,
            source_records,
            context_observations=context_observations or (),
            timeout_seconds=timeout_seconds,
            lineage_identity=lineage_identity,
        )

    def capability_blockers(
        self, required_capabilities: list[str] | None = None
    ) -> dict[str, object]:
        return self._provider_service.capability_blockers(required_capabilities)

    # Singular micro-verifier lifecycle.
    def verifier_list(self) -> dict[str, object]:
        return self._verifier_service.list_declared()

    def verifier_describe(self, verifier_id: str) -> dict[str, object]:
        return self._verifier_service.describe(verifier_id)

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
    ) -> dict[str, object]:
        return self._verifier_service.match(
            uncertainty_classes=uncertainty_classes,
            language=language,
            artifact_type=artifact_type,
            changed_paths=changed_paths,
            scope=scope,
            maximum_cost=maximum_cost,
            required_category=required_category,
            active_mode=active_mode,
        )

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
    ) -> dict[str, object]:
        return self._verifier_service.run(
            verifier_id,
            candidate_identity=candidate_identity,
            changed_paths=changed_paths,
            scope=scope,
            source_region=source_region,
            contract_identity=contract_identity,
            dependency_slice_identities=dependency_slice_identities,
            prior_artifact_identity=prior_artifact_identity,
            question_parameters=question_parameters,
        )

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
    ) -> dict[str, object]:
        return self._verifier_service.batch(
            verifier_ids,
            candidate_identity=candidate_identity,
            changed_paths=changed_paths,
            scope=scope,
            source_region=source_region,
            contract_identity=contract_identity,
            dependency_slice_identities=dependency_slice_identities,
            prior_artifact_identity=prior_artifact_identity,
            question_parameters=question_parameters,
        )

    def verifier_explain(self, output_identity: str) -> dict[str, object]:
        return self._verifier_service.explain(output_identity)

    # Epoch, candidate, and development workflow orchestration.
    def epoch_begin(
        self,
        *,
        generator_identity: str,
        evaluator_identity: str,
        parent_epoch: str | None = None,
        authority_overlap: list[str] | None = None,
    ) -> dict[str, object]:
        return self._candidate_service.begin_epoch(
            generator_identity=generator_identity,
            evaluator_identity=evaluator_identity,
            parent_epoch=parent_epoch,
            authority_overlap=authority_overlap,
        )

    def candidate_register(
        self,
        *,
        changed_files: list[str],
        hypothesis: str,
        generator_identity: str,
        generator_config_identity: str,
        parent_candidate: str | None = None,
        expected_identity: str | None = None,
    ) -> dict[str, object]:
        return self._candidate_service.register(
            changed_files=changed_files,
            hypothesis=hypothesis,
            generator_identity=generator_identity,
            generator_config_identity=generator_config_identity,
            parent_candidate=parent_candidate,
            expected_identity=expected_identity,
        )

    def candidate_refresh(
        self,
        *,
        hypothesis: str,
        generator_identity: str,
        generator_config_identity: str,
        changed_files: list[str] | None = None,
    ) -> dict[str, object]:
        return self._candidate_service.refresh(
            hypothesis=hypothesis,
            generator_identity=generator_identity,
            generator_config_identity=generator_config_identity,
            changed_files=changed_files,
        )

    def development_checks_run(
        self, workflow_names: list[str], candidate_id: str | None = None
    ) -> dict[str, object]:
        return self._development_service.run(workflow_names, candidate_id)

    def failure_explain(self, output_identity: str | None = None) -> dict[str, object]:
        return self._development_service.explain(output_identity)

    def continuous_run(
        self,
        *,
        once: bool = False,
        max_events: int | None = None,
        after_cursor: int | None = None,
        poll_interval_seconds: float | None = None,
    ) -> dict[str, object]:
        from .continuous import ContinuousSupervisor

        return ContinuousSupervisor(self).run(
            once=once,
            max_events=max_events,
            after_cursor=after_cursor,
            poll_interval_seconds=poll_interval_seconds,
        )

    def continuous_status(self) -> dict[str, object]:
        from .continuous import ContinuousSupervisor

        return ContinuousSupervisor(self).read_status()

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
        max_values: int = 1024,
        max_value_bytes: int = 4096,
        selected_operations: list[str] | None = None,
        timeout_seconds: float | None = None,
        minimize: bool = False,
        test_id: str | None = None,
        provider_mode: str = "invoke",
        debug_check_file: str | None = None,
        actions_evidence_files: list[str] | None = None,
        actions_command: list[str] | None = None,
        family_graph_file: str | None = None,
        family_workspace_root: str | None = None,
        family_proof_directory: str = ".mncs-forge/family-proof",
        repair_path: str | None = None,
        repair_from: str | None = None,
        repair_to: str | None = None,
        verification_plan_file: str | None = None,
        post_repair_verification_plan_file: str | None = None,
        ravel_command: list[str] | None = None,
        diagnostic_depth: str = "minimal",
        output_file: str | None = None,
    ) -> dict[str, object]:
        return self._mncs_development_service.failure_loop(
            manifest=manifest,
            test_command=test_command,
            debug_command=debug_command,
            mncs_binary=mncs_binary,
            library_paths=library_paths,
            embed_library=embed_library,
            working_directory=working_directory,
            test_result_file=test_result_file,
            test_check_file=test_check_file,
            test_artifacts_directory=test_artifacts_directory,
            debug_witness_file=debug_witness_file,
            debug_artifacts_directory=debug_artifacts_directory,
            capture_policy=capture_policy,
            max_events=max_events,
            max_values=max_values,
            max_value_bytes=max_value_bytes,
            selected_operations=selected_operations,
            timeout_seconds=timeout_seconds,
            minimize=minimize,
            test_id=test_id,
            provider_mode=provider_mode,
            debug_check_file=debug_check_file,
            actions_evidence_files=actions_evidence_files,
            actions_command=actions_command,
            family_graph_file=family_graph_file,
            family_workspace_root=family_workspace_root,
            family_proof_directory=family_proof_directory,
            repair_path=repair_path,
            repair_from=repair_from,
            repair_to=repair_to,
            verification_plan_file=verification_plan_file,
            post_repair_verification_plan_file=post_repair_verification_plan_file,
            ravel_command=ravel_command,
            diagnostic_depth=diagnostic_depth,
            output_file=output_file,
        )

    def candidate_compare(self, candidate_ids: list[str]) -> dict[str, object]:
        return self._candidate_service.compare(candidate_ids)

    def candidate_disposition(
        self, candidate_id: str, *, disposition: str, reason: str
    ) -> dict[str, object]:
        return self._candidate_service.dispose(candidate_id, disposition=disposition, reason=reason)

    # Freeze, evaluator, reconciliation, claim, and bundle operations.
    def candidate_freeze(
        self, candidate_id: str, *, environment_identity: str, required_evidence_plan: str
    ) -> dict[str, object]:
        return self._evaluation_service.freeze(
            candidate_id,
            environment_identity=environment_identity,
            required_evidence_plan=required_evidence_plan,
        )

    def final_evaluation_run(self, workflow_names: list[str]) -> dict[str, object]:
        return self._evaluation_service.run(workflow_names)

    def claim_status(self) -> dict[str, object]:
        return self._evidence_service.claim_status()

    def claim_blockers(self, requested_claim: str) -> dict[str, object]:
        return self._evidence_service.claim_blockers(requested_claim)

    def evidence_reconcile(self, candidate_id: str | None = None) -> dict[str, object]:
        return self._evidence_service.reconcile(candidate_id)

    def bundle_build(
        self, workflow_name: str, candidate_id: str | None = None
    ) -> dict[str, object]:
        return self._evidence_service.build_bundle(workflow_name, candidate_id)

    def execution_receipts_list(
        self,
        candidate_identity: str | None = None,
        action_identity: str | None = None,
    ) -> dict[str, object]:
        return list_bindings(
            self.ledger,
            candidate_identity=candidate_identity,
            action_identity=action_identity,
        )

    def execution_receipts_get(self, binding_id: str) -> dict[str, object]:
        return get_binding(self.ledger, binding_id)

    def execution_assurance_assess(
        self,
        *,
        binding_id: str,
        requested_properties: list[str],
        policy_identity: str | None = None,
    ) -> dict[str, object]:
        return assess_execution_receipt(
            config=self.config,
            records=self.ledger,
            record_store=self.record_store,
            binding_id=binding_id,
            requested_properties=requested_properties,
            policy_identity=policy_identity,
        )

    def execution_assurance_list(
        self,
        binding_identity: str | None = None,
        candidate_identity: str | None = None,
    ) -> dict[str, object]:
        return list_assessments(
            self.ledger,
            binding_identity=binding_identity,
            candidate_identity=candidate_identity,
        )

    def cell_document_validate(
        self, kind: str, document: Mapping[str, object]
    ) -> dict[str, object]:
        try:
            validate_forge_cell_document(kind, document)  # type: ignore[arg-type]
        except ForgeCellValidationError as exc:
            return {
                "ok": False,
                "kind": kind,
                "code": exc.code,
                "message": str(exc),
                "note": "Forge Cell validation is offline schema checking; it executes nothing.",
            }
        return {
            "ok": True,
            "kind": kind,
            "schema_version": "0.1",
            "note": "The document satisfies its packaged Forge Cell 0.1 schema.",
        }

    def cell_execution_assess(
        self,
        policy: Mapping[str, object],
        record: Mapping[str, object],
        expected_nonce: str | None = None,
    ) -> dict[str, object]:
        try:
            assessment = assess_cell_execution_assurance(
                dict(policy), dict(record), expected_nonce=expected_nonce
            )
        except ForgeCellValidationError as exc:
            raise ForgeError("CELL_DOCUMENT_INVALID", f"{exc.code}: {exc}") from exc
        return {
            "assurance_status": assessment.status,
            "reasons": list(assessment.reasons),
            "requested": list(assessment.requested),
            "enforced": list(assessment.enforced),
            "unmet": list(assessment.unmet),
            "dominance": "FAIL > UNKNOWN > PASS",
            "note": (
                "Assurance is assessed separately from any test result; a test PASS "
                "cannot establish isolation or custody properties."
            ),
        }

    def compiler_experiment_record(
        self, language_record: Mapping[str, object]
    ) -> dict[str, object]:
        return self._compiler_evolution_service.record(language_record)

    def compiler_experiments_list(self) -> dict[str, object]:
        return self._compiler_evolution_service.list()

    def compiler_experiments_compare(
        self, left_experiment_id: str, right_experiment_id: str
    ) -> dict[str, object]:
        return self._compiler_evolution_service.compare(
            left_experiment_id,
            right_experiment_id,
        )

    def concept_evaluation_record(self, evaluation: Mapping[str, object]) -> dict[str, object]:
        return self._concept_evaluation_service.record(evaluation)

    def concept_evaluations_list(self) -> dict[str, object]:
        return self._concept_evaluation_service.list()

    def concept_evaluation_get(self, evaluation_id: str) -> dict[str, object]:
        return self._concept_evaluation_service.get(evaluation_id)

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
    ) -> dict[str, object]:
        return self._compiler_candidate_service.register(
            baseline_artifact_identity=baseline_artifact_identity,
            candidate_artifact_identity=candidate_artifact_identity,
            generator_identity=generator_identity,
            declared_transformation=declared_transformation,
            claimed_relation=claimed_relation,
            expected_benefit=expected_benefit,
            protected_properties=protected_properties or [],
            target_envelope=target_envelope,
            required_validation=required_validation,
        )

    def compiler_candidates_list(self) -> dict[str, object]:
        return self._compiler_candidate_service.inventory()

    def compiler_candidates_compare(
        self, left_candidate_id: str, right_candidate_id: str
    ) -> dict[str, object]:
        return self._compiler_candidate_service.compare(left_candidate_id, right_candidate_id)

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
    ) -> dict[str, object]:
        return self._compiler_candidate_service.attach_validation(
            candidate_id,
            validator_identity=validator_identity,
            judgement=judgement,
            claimed_relation=claimed_relation,
            counterexample=counterexample,
            limitations=limitations,
            stale=stale,
            expected_artifact_identity=expected_artifact_identity,
        )

    def compiler_tournament(self, candidate_ids: list[str]) -> dict[str, object]:
        return self._compiler_candidate_service.tournament(candidate_ids)

    def compiler_candidate_select(self, candidate_id: str, policy: str) -> dict[str, object]:
        return self._compiler_candidate_service.select(candidate_id, policy=policy)

    def compiler_candidate_inspect(self, candidate_id: str) -> dict[str, object]:
        return self._compiler_candidate_service.inspect_unresolved(candidate_id)
