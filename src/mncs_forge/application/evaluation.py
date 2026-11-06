"""Candidate freeze and evaluator-mode execution application service."""

from __future__ import annotations

import json

from ..config import ForgeConfig
from ..errors import ForgeError
from ..paths import resolve_contained
from ..ports import ProjectObserver, RecordCommitter
from ..records import FinalEvaluationRecord, RecordType, new_record
from ..serialization import read_json
from .execution_receipts import persist_workflow_execution, summarize_binding
from .lifecycle import LifecycleContext
from .support import aggregate_status, now
from .workflows import WorkflowExecutor


class EvaluationService:
    def __init__(
        self,
        *,
        config: ForgeConfig,
        observer: ProjectObserver,
        record_store: RecordCommitter,
        lifecycle: LifecycleContext,
        workflows: WorkflowExecutor,
    ) -> None:
        self.config = config
        self.observer = observer
        self.record_store = record_store
        self.lifecycle = lifecycle
        self.workflows = workflows

    def freeze(
        self, candidate_id: str, *, environment_identity: str, required_evidence_plan: str
    ) -> dict[str, object]:
        plan_path = resolve_contained(self.config.root, required_evidence_plan, must_exist=False)
        try:
            plan = read_json(plan_path, byte_cap=self.config.output_cap)
        except (OSError, ValueError, json.JSONDecodeError):
            plan = None
        raw_plan = (
            plan.get("required_workflows", plan.get("required")) if isinstance(plan, dict) else None
        )
        plan_requirements = (
            tuple(dict.fromkeys(raw_plan))
            if isinstance(raw_plan, list)
            and raw_plan
            and all(isinstance(item, str) and item for item in raw_plan)
            else None
        )
        candidate, selection = self.lifecycle.machine().authorize_candidate_freeze(
            candidate_id, evidence_plan_requirements=plan_requirements
        )
        paths = self.config.raw["paths"]
        record = new_record(
            RecordType.FREEZE,
            {
                "candidate_identity": candidate["candidate_id"],
                "contract_identity": self.observer.content_identity(self.config.paths("contracts")),
                "reference_identity": self.observer.content_identity(
                    self.config.paths("references")
                ),
                "evaluator_identity": self.observer.content_identity(
                    self.config.paths("evaluators")
                ),
                "acceptance_policy_identity": self.observer.content_identity(
                    self.config.paths("acceptance_policies")
                ),
                "protected_identity": self.observer.content_identity(
                    self.config.paths("protected")
                ),
                "selection_record": selection["disposition_id"],
                "environment": environment_identity,
                "required_evidence_plan": required_evidence_plan,
                "required_evidence_plan_identity": self.observer.content_identity([plan_path]),
                "frozen_path_sets": {
                    key: list(paths[key])
                    for key in (
                        "candidates",
                        "contracts",
                        "references",
                        "evaluators",
                        "protected",
                    )
                },
                "frozen_at": now(),
            },
        )
        self.record_store.commit("freezes", "freeze", record)
        return record.to_object_dict()

    def run(self, workflow_names: list[str]) -> dict[str, object]:
        freeze, candidate = self.lifecycle.machine(
            observe_epoch_authority=False
        ).authorize_evaluator_entry()
        results: list[FinalEvaluationRecord] = []
        receipts: list[dict[str, object]] = []
        for name in workflow_names:
            workflow = self.workflows.workflow(name, "evaluator")
            before = self.observer.current_authority_identities()
            candidate_before = self.observer.current_candidate_identity()
            execution = self.workflows.execute(
                workflow,
                candidate,
                evaluator=True,
                record_type=RecordType.FINAL_EVALUATION,
            )
            binding = persist_workflow_execution(
                config=self.config,
                records=self.lifecycle.records,
                record_store=self.record_store,
                execution=execution,
            )
            if execution.error is not None:
                raise execution.error
            result = execution.result
            if not isinstance(result, FinalEvaluationRecord):
                raise ForgeError("INTERNAL_RECORD", "evaluation produced an invalid record model")
            if before != self.observer.current_authority_identities():
                raise ForgeError("EVALUATION_DRIFT", "authority files changed during evaluation")
            if candidate_before != self.observer.current_candidate_identity():
                raise ForgeError("EVALUATION_DRIFT", "candidate changed during evaluation")
            self.lifecycle.verify_freeze(freeze)
            results.append(result)
            receipts.append(summarize_binding(binding))
        return {
            "freeze_id": freeze["freeze_id"],
            "candidate_identity": freeze["candidate_identity"],
            "results": [
                {
                    "workflow": item["workflow"],
                    "status": item["status"],
                    "limitations": item["limitations"],
                    "output_identity": item["output_identity"],
                }
                for item in results
            ],
            "execution_receipts": receipts,
            "aggregate_status": aggregate_status(
                (str(item["status"]) for item in results),
                native_status_aggregate=self.lifecycle.native_status_aggregate,
            ),
            "repair_feedback_withheld": True,
            "dominance": "FAIL > UNKNOWN > PASS",
        }
