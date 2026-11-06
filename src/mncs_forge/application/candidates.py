"""Epoch, candidate-lineage, comparison, and disposition application service."""

from __future__ import annotations

from ..config import ForgeConfig
from ..errors import ForgeError
from ..paths import resolve_contained
from ..ports import ProjectObserver, RecordCommitter
from ..records import RecordType, new_record
from .lifecycle import LifecycleContext
from .rights_provenance import enforce_rights_provenance_selection, rights_provenance_status
from .support import aggregate_status, now
from .workflows import DevelopmentWorkflowService


class CandidateService:
    def __init__(
        self,
        *,
        config: ForgeConfig,
        observer: ProjectObserver,
        record_store: RecordCommitter,
        lifecycle: LifecycleContext,
        development: DevelopmentWorkflowService,
    ) -> None:
        self.config = config
        self.observer = observer
        self.record_store = record_store
        self.lifecycle = lifecycle
        self.development = development

    def begin_epoch(
        self,
        *,
        generator_identity: str,
        evaluator_identity: str,
        parent_epoch: str | None = None,
        authority_overlap: list[str] | None = None,
    ) -> dict[str, object]:
        self.lifecycle.machine().authorize_epoch_begin(parent_epoch)
        contract = self.observer.content_identity(self.config.paths("contracts"))
        objective_path = resolve_contained(
            self.config.root,
            str(self.config.raw["policies"]["useful_benefit_objective"]),
            must_exist=False,
        )
        evaluator = self.observer.content_identity(self.config.paths("evaluators"))
        record = new_record(
            RecordType.EPOCH,
            {
                "baseline_identity": self.observer.content_identity(
                    [*self.observer.candidate_paths(), *self.observer.authority_paths()],
                ),
                "generator_identity": generator_identity,
                "evaluator_identity": evaluator_identity or evaluator,
                "contract_identity": contract,
                "objective_identity": self.observer.content_identity([objective_path]),
                "visible_partition_identities": self.observer.identity_map(
                    [
                        *self.config.paths("contracts"),
                        *self.config.paths("references"),
                        *self.config.paths("development_evidence"),
                    ],
                ),
                "authority_identities": self.observer.current_authority_identities(),
                "declared_authority_overlap": sorted(authority_overlap or []),
                "parent_epoch": parent_epoch,
                "created_at": now(),
            },
        )
        self.record_store.commit("epochs", "epoch", record)
        return record.to_object_dict()

    def register(
        self,
        *,
        changed_files: list[str],
        hypothesis: str,
        generator_identity: str,
        generator_config_identity: str,
        parent_candidate: str | None = None,
        expected_identity: str | None = None,
    ) -> dict[str, object]:
        current = self.observer.current_candidate_identity()
        if expected_identity is not None and expected_identity != current:
            raise ForgeError("STALE_CANDIDATE", "candidate identity does not match current content")
        epoch = self.lifecycle.machine().authorize_candidate_register(
            parent_candidate=parent_candidate,
            proposed_identity=current,
        )
        current_files = self.observer.validate_changed_files(changed_files)
        objective_path = resolve_contained(
            self.config.root,
            str(self.config.raw["policies"]["useful_benefit_objective"]),
            must_exist=False,
        )
        record = new_record(
            RecordType.CANDIDATE,
            {
                "candidate_id": current,
                "parent_candidate": parent_candidate,
                "changed_files": sorted(current_files),
                "declared_hypothesis": hypothesis,
                "generator_identity": generator_identity,
                "generator_configuration_identity": generator_config_identity,
                "source_epoch": epoch["epoch_id"],
                "registered_at": now(),
                "current_file_identities": current_files,
                "useful_benefit_objective": str(
                    self.config.raw["policies"]["useful_benefit_objective"]
                ),
                "objective_identity": self.observer.content_identity([objective_path]),
                "supersedes": None,
            },
        )
        self.record_store.commit("candidates", "candidate", record)
        return record.to_object_dict()

    def refresh(
        self,
        *,
        hypothesis: str,
        generator_identity: str,
        generator_config_identity: str,
        changed_files: list[str] | None = None,
    ) -> dict[str, object]:
        """Register a successor candidate when working-tree content drifted.

        Prior evidence remains bound to the previous candidate identity. A
        current, matching candidate is returned unchanged so callers can always
        rebind before evaluation without inventing a new identity.
        """

        state_machine = self.lifecycle.machine()
        existing = state_machine.projection.current_candidate
        if existing is None:
            raise ForgeError("NO_CANDIDATE", "no candidate exists in the active epoch")
        current = self.observer.current_candidate_identity()
        existing_id = str(existing["candidate_id"])
        if state_machine.projection.candidate_freshness == "CURRENT" and existing_id == current:
            return {
                "refreshed": False,
                "reason": "candidate already matches current content",
                "previous_candidate_identity": existing_id,
                "candidate_identity": existing_id,
                "candidate": existing.to_object_dict(),
                "note": "prior evidence remains bound to this candidate identity",
            }
        files = list(changed_files or [])
        if not files:
            previous = existing.get("changed_files")
            if isinstance(previous, list):
                files = [str(item) for item in previous if isinstance(item, str) and item]
        if not files:
            raise ForgeError(
                "INVALID_CHANGED_FILE",
                "candidate refresh requires changed files when none were previously declared",
            )
        record = self.register(
            changed_files=files,
            hypothesis=hypothesis,
            generator_identity=generator_identity,
            generator_config_identity=generator_config_identity,
            parent_candidate=existing_id,
        )
        return {
            "refreshed": True,
            "reason": "working-tree content no longer matched the bound candidate",
            "previous_candidate_identity": existing_id,
            "candidate_identity": record["candidate_id"],
            "candidate": record,
            "note": "prior evidence remains bound to the previous candidate identity",
        }

    def compare(self, candidate_ids: list[str]) -> dict[str, object]:
        if len(candidate_ids) < 2:
            raise ForgeError("COMPARE_INPUT", "at least two candidate identities are required")
        self.lifecycle.machine().authorize_candidate_comparison(candidate_ids)
        policy_path = resolve_contained(
            self.config.root, str(self.config.raw["policies"]["selection"]), must_exist=False
        )
        candidates: list[dict[str, object]] = []
        for candidate_id in candidate_ids:
            results = [
                record.to_object_dict() for record in self.development.result_records(candidate_id)
            ]
            statuses = [str(item["status"]) for item in results]
            candidates.append(
                {
                    "candidate_identity": candidate_id,
                    "correctness_and_safety": aggregate_status(
                        statuses, native_status_aggregate=self.lifecycle.native_status_aggregate
                    ),
                    "resource_constraints": [
                        item["status"] for item in results if item["category"] == "resource_checks"
                    ]
                    or ["UNKNOWN"],
                    "useful_benefit_measurements": [
                        item["witnesses_or_counterexamples"]
                        for item in results
                        if item["category"] == "benchmark"
                    ],
                    "regressions": [
                        item["workflow"] for item in results if item["status"] == "FAIL"
                    ],
                    "evidence_completeness": (
                        "complete"
                        if results and all(item["status"] == "PASS" for item in results)
                        else "incomplete"
                    ),
                    "unknown_results": [
                        item["workflow"] for item in results if item["status"] == "UNKNOWN"
                    ],
                    "environmental_comparability": "UNKNOWN",
                    "rights_provenance": rights_provenance_status(
                        config=self.config,
                        lifecycle=self.lifecycle,
                        development=self.development,
                        candidate_identity=candidate_id,
                    ),
                }
            )
        return {
            "selection_policy": str(self.config.raw["policies"]["selection"]),
            "selection_policy_identity": self.observer.content_identity([policy_path]),
            "candidates": candidates,
            "pareto_or_tie_status": "REVIEW_REQUIRED",
            "selected_candidate": None,
            "note": "Forge does not select on a single observed benchmark run",
        }

    def dispose(self, candidate_id: str, *, disposition: str, reason: str) -> dict[str, object]:
        rights: dict[str, object] | None = None
        if disposition == "selected":
            rights = enforce_rights_provenance_selection(
                config=self.config,
                lifecycle=self.lifecycle,
                development=self.development,
                candidate_identity=candidate_id,
            )
        state_machine = self.lifecycle.machine()
        _, readiness = state_machine.authorize_candidate_disposition(candidate_id, disposition)
        fields: dict[str, object] = {
            "candidate_identity": candidate_id,
            "disposition": disposition,
            "reason": reason,
            "selection_rule": str(self.config.raw["policies"]["selection"]),
            "selection_policy_identity": state_machine.selection_policy_identity,
            "evidence_status": readiness.status,
            "recorded_at": now(),
        }
        if rights is not None:
            fields["extensions"] = {"rights_provenance": rights}
        record = new_record(RecordType.CANDIDATE_DISPOSITION, fields)
        self.record_store.commit("dispositions", "disposition", record)
        return record.to_object_dict()
