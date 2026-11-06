"""Claim reporting, reconciliation, and evidence-bundle application service."""

from __future__ import annotations

import json
from collections.abc import Mapping

from ..config import ForgeConfig
from ..errors import ForgeError
from ..ports import RecordCommitter
from ..records import BundleRecord, RecordType, new_record
from ..serialization import read_json
from .execution_receipts import persist_workflow_execution, summarize_binding
from .lifecycle import LifecycleContext
from .rights_provenance import rights_provenance_status
from .support import CLAIM_CLASSES, aggregate_status
from .workflows import DevelopmentWorkflowService, WorkflowExecutor


class EvidenceService:
    def __init__(
        self,
        *,
        config: ForgeConfig,
        mode: str,
        record_store: RecordCommitter,
        lifecycle: LifecycleContext,
        development: DevelopmentWorkflowService,
        workflows: WorkflowExecutor,
    ) -> None:
        self.config = config
        self.mode = mode
        self.record_store = record_store
        self.lifecycle = lifecycle
        self.development = development
        self.workflows = workflows

    @staticmethod
    def _extract_statuses(value: object) -> list[str]:
        statuses: list[str] = []
        if isinstance(value, dict):
            for key, child in value.items():
                if key in {"status", "result", "overall_status", "aggregate_status"}:
                    if isinstance(child, str) and child in {"PASS", "FAIL", "UNKNOWN"}:
                        statuses.append(child)
                elif isinstance(child, (dict, list)):
                    statuses.extend(EvidenceService._extract_statuses(child))
        elif isinstance(value, list):
            for child in value:
                statuses.extend(EvidenceService._extract_statuses(child))
        return statuses

    @staticmethod
    def _classify_record(path: str) -> str:
        lowered = path.lower()
        if "mncds" in lowered or "development-record" in lowered:
            return "mncds_development_process_result"
        if "independent" in lowered:
            return "independent_evaluation"
        if "holdout" in lowered or "protected" in lowered:
            return "protected_holdout"
        if "witness" in lowered:
            return "witnessed_evidence"
        if "operational" in lowered or "monitor" in lowered:
            return "operational_evidence"
        if "governance" in lowered or "approval" in lowered:
            return "governance_approval"
        if "reproduction" in lowered:
            return "local_reproduction"
        return "mncs_implementation_result"

    def _structured_statuses(self) -> list[dict[str, str]]:
        records: list[dict[str, str]] = []
        seen = 0
        for root in [*self.config.paths("development_evidence"), *self.config.paths("outputs")]:
            paths = (
                [root] if root.is_file() else sorted(root.rglob("*.json")) if root.exists() else []
            )
            for path in paths:
                if seen >= 1000:
                    break
                seen += 1
                try:
                    value = read_json(path, byte_cap=self.config.output_cap)
                except (OSError, ValueError, json.JSONDecodeError):
                    continue
                for status in self._extract_statuses(value):
                    relative = path.relative_to(self.config.root).as_posix()
                    records.append(
                        {
                            "source": relative,
                            "status": status,
                            "claim_class": self._classify_record(relative),
                        }
                    )
        return records

    def _rights_status(self, candidate_id: str | None = None) -> dict[str, object]:
        return rights_provenance_status(
            config=self.config,
            lifecycle=self.lifecycle,
            development=self.development,
            candidate_identity=candidate_id,
        )

    def claim_status(self) -> dict[str, object]:
        source_records = self._structured_statuses()
        statuses: dict[str, str] = {}
        sources: dict[str, list[str]] = {}
        for claim_class in CLAIM_CLASSES:
            matching = [item for item in source_records if item["claim_class"] == claim_class]
            statuses[claim_class] = aggregate_status(
                (item["status"] for item in matching),
                native_status_aggregate=self.lifecycle.native_status_aggregate,
            )
            sources[claim_class] = sorted({item["source"] for item in matching})[:20]
        statuses["operator_controlled_reproduction"] = "UNKNOWN"
        statuses["promotion_disposition"] = (
            "REVIEW_REQUIRED"
            if all(value == "PASS" for value in statuses.values())
            else "NOT_PROMOTABLE"
        )
        return {
            "statuses": statuses,
            "sources": sources,
            "rights_provenance": self._rights_status(),
            "dominance": "FAIL > UNKNOWN > PASS",
            "promotion_note": (
                "REVIEW_REQUIRED is a workflow disposition, not an MNCS result; rights/provenance "
                "is reported as a separate evidence domain and affects promotion only when its "
                "policy mode is explicitly enforced"
            ),
            "missing_is_pass": False,
        }

    def claim_blockers(self, requested_claim: str) -> dict[str, object]:
        rights = self._rights_status()
        rights_blockers = rights.get("blockers")
        rights_items = (
            [str(item) for item in rights_blockers] if isinstance(rights_blockers, list) else []
        )
        rights_policy = rights.get("policy")
        rights_blocking = (
            isinstance(rights_policy, Mapping) and rights_policy.get("blocking") is True
        )

        if requested_claim in {"rights", "rights_provenance"}:
            blockers = [
                {
                    "claim_class": "rights_provenance",
                    "status": str(rights.get("evidence_status", "UNKNOWN")),
                    "problem": item,
                    "work_class": "governance_work",
                }
                for item in rights_items
            ]
            return {
                "requested_claim": requested_claim,
                "blockers": blockers,
                "blocked": rights_blocking,
                "review_required": bool(rights_items),
                "rights_provenance": rights,
                "boundary": (
                    "Forge evaluates provenance evidence and configured policy; it does not make "
                    "legal conclusions or create rights authority"
                ),
            }

        status = self.claim_status()
        raw_statuses = status["statuses"]
        if not isinstance(raw_statuses, dict):
            raise ForgeError("INTERNAL_STATUS", "claim status map is invalid")
        statuses = {str(key): str(value) for key, value in raw_statuses.items()}
        mapping = {
            "mncs": ["mncs_implementation_result"],
            "mncds": ["mncds_development_process_result"],
            "independent": ["independent_evaluation"],
            "protected": ["protected_holdout"],
            "promotion": list(CLAIM_CLASSES),
        }
        required = mapping.get(requested_claim, [requested_claim])
        category_map = {
            "mncs_implementation_result": "locally_executable_work",
            "mncds_development_process_result": "locally_executable_work",
            "local_reproduction": "locally_executable_work",
            "operator_controlled_reproduction": "physical_machine_work",
            "independent_evaluation": "independent_evaluator_work",
            "protected_holdout": "protected_custody_work",
            "witnessed_evidence": "witnessed_work",
            "operational_evidence": "operational_work",
            "governance_approval": "governance_work",
        }
        blockers = [
            {
                "claim_class": name,
                "status": statuses.get(name, "UNKNOWN"),
                "problem": (
                    "failed evidence"
                    if statuses.get(name, "UNKNOWN") == "FAIL"
                    else "absent or unsupported evidence"
                ),
                "work_class": category_map.get(name, "unsupported_work"),
            }
            for name in required
            if statuses.get(name, "UNKNOWN") != "PASS"
        ]
        if requested_claim == "promotion" and rights_blocking:
            blockers.append(
                {
                    "claim_class": "rights_provenance",
                    "status": str(rights.get("evidence_status", "UNKNOWN")),
                    "problem": (
                        "; ".join(rights_items)
                        if rights_items
                        else "explicit enforced rights/provenance policy is unresolved"
                    ),
                    "work_class": "governance_work",
                }
            )
        return {
            "requested_claim": requested_claim,
            "blockers": blockers,
            "blocked": bool(blockers),
            "stale_or_conflicting": [
                item["source"]
                for item in self._structured_statuses()
                if item["status"] in {"FAIL", "UNKNOWN"}
            ][:20],
            "rights_provenance": rights,
            "boundary": "Forge reports blockers; it cannot create external authority or promotion",
        }

    @staticmethod
    def _unsupported_values(items: list[dict[str, object]]) -> list[str]:
        values: list[str] = []
        for item in items:
            unsupported = item.get("unsupported_constructs")
            if isinstance(unsupported, list):
                values.extend(str(value) for value in unsupported)
        return values

    def _native_reconciliation(
        self, by_category: dict[str, list[dict[str, object]]]
    ) -> tuple[dict[str, object], list[str], str] | None:
        native = self.lifecycle.native
        if native is None:
            return None
        projection = native.reconciliation_projection(by_category)
        ordered = sorted(by_category.items())
        if len(projection.categories) != len(ordered):
            raise ForgeError(
                "NATIVE_RECONCILIATION_MISMATCH",
                "native reconciliation returned an unexpected category count",
            )
        categories: dict[str, object] = {}
        conflicts: list[str] = []
        for (category, items), native_category in zip(ordered, projection.categories, strict=True):
            unsupported_values = self._unsupported_values(items)
            if native_category.observed_count != len(items):
                raise ForgeError(
                    "NATIVE_RECONCILIATION_MISMATCH",
                    f"native observed count disagrees for evidence category {category}",
                )
            if native_category.unsupported_count != len(unsupported_values):
                raise ForgeError(
                    "NATIVE_RECONCILIATION_MISMATCH",
                    f"native unsupported count disagrees for evidence category {category}",
                )
            if (
                native_category.pass_count
                + native_category.fail_count
                + native_category.unknown_count
                != len(items)
            ):
                raise ForgeError(
                    "NATIVE_RECONCILIATION_MISMATCH",
                    f"native status counts disagree for evidence category {category}",
                )
            if native_category.conflict:
                conflicts.append(category)
            categories[category] = {
                "status": native_category.status,
                "dependencies": [],
                "records": [str(item["output_identity"]) for item in items],
                "unsupported": sorted(set(unsupported_values)),
            }
        if (
            projection.observed_count != sum(len(items) for _, items in ordered)
            or projection.unsupported_count
            != sum(len(self._unsupported_values(items)) for _, items in ordered)
            or projection.conflicting_category_count != len(conflicts)
        ):
            raise ForgeError(
                "NATIVE_RECONCILIATION_MISMATCH",
                "native reconciliation aggregate counts disagree with the request",
            )
        return categories, conflicts, projection.status

    @classmethod
    def _compat_reconciliation(
        cls, by_category: dict[str, list[dict[str, object]]]
    ) -> tuple[dict[str, object], list[str], str]:
        """Compatibility classification used only when native mode is off/unavailable."""

        categories: dict[str, object] = {}
        conflicts: list[str] = []
        for category, items in sorted(by_category.items()):
            values = {str(item["status"]) for item in items}
            if len(values) > 1:
                conflicts.append(category)
            unsupported_values = cls._unsupported_values(items)
            categories[category] = {
                "status": aggregate_status(values),
                "dependencies": [],
                "records": [str(item["output_identity"]) for item in items],
                "unsupported": sorted(set(unsupported_values)),
            }
        aggregate = aggregate_status(
            str(value["status"]) for value in categories.values() if isinstance(value, dict)
        )
        return categories, conflicts, aggregate

    def reconcile(self, candidate_id: str | None = None) -> dict[str, object]:
        resolved_candidate = self.lifecycle.machine().authorize_reconciliation(candidate_id)
        selected_results = self.development.result_records(resolved_candidate)
        if resolved_candidate is None:
            selected_results = [
                record for record in selected_results if record.get("subject_type") == "project"
            ]
        results = [record.to_object_dict() for record in selected_results]
        by_category: dict[str, list[dict[str, object]]] = {}
        for result in results:
            by_category.setdefault(str(result["category"]), []).append(result)
        native_projection = self._native_reconciliation(by_category)
        if native_projection is None:
            categories, conflicts, aggregate = self._compat_reconciliation(by_category)
        else:
            categories, conflicts, aggregate = native_projection
        rights = self._rights_status(resolved_candidate)
        return new_record(
            RecordType.RECONCILIATION,
            {
                "candidate_identity": resolved_candidate,
                "required_gate_aggregation": aggregate,
                "categories": categories,
                "conflicting_evidence": conflicts,
                "stale_identities": [],
                "claim_limitations": (
                    ["one or more required gates are not PASS"] if aggregate != "PASS" else []
                ),
                "unresolved_blockers": [
                    name
                    for name, value in categories.items()
                    if isinstance(value, dict) and value["status"] != "PASS"
                ],
                "dominance": "FAIL > UNKNOWN > PASS",
                "normative_logic_delegated": (
                    "MNCS and MNCDS validators remain offline authorities"
                ),
                "extensions": {
                    "rights_provenance": rights,
                    "domain_boundary": (
                        "required_gate_aggregation remains technical/development evidence; "
                        "rights/provenance is separate unless explicitly enforced by policy"
                    ),
                },
            },
        ).to_object_dict()

    def build_bundle(
        self, workflow_name: str, candidate_id: str | None = None
    ) -> dict[str, object]:
        workflow = self.workflows.workflow(workflow_name, self.mode)
        valid_category = workflow.category in {"mncs_bundle_validation", "mncds_record_validation"}
        machine = self.lifecycle.machine()
        candidate = machine.authorize_bundle(
            candidate_id,
            request_valid=valid_category if self.lifecycle.native is not None else True,
        )
        if not valid_category:
            raise ForgeError("WORKFLOW_CATEGORY", "bundle requires an MNCS or MNCDS workflow")
        execution = self.workflows.execute(
            workflow,
            candidate,
            evaluator=self.mode == "evaluator",
            record_type=RecordType.BUNDLE,
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
        if not isinstance(result, BundleRecord):
            raise ForgeError("INTERNAL_RECORD", "bundle produced an invalid record model")
        integrity = str(result["status"])
        return {
            "package_creation": "COMPLETED" if result["returncode"] == 0 else "FAILED",
            "package_integrity": integrity,
            "schema_validity": integrity,
            "cryptographic_validity": "UNKNOWN",
            "trust": "UNKNOWN",
            "certification_eligibility": "UNKNOWN",
            "operational_disposition": "REVIEW_REQUIRED",
            "result_reference": result["output_identity"],
            "execution_receipt": summarize_binding(binding),
            "note": "a valid package or signature is not proof of correctness",
        }
