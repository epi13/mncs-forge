"""Declared workflow execution and development-check application service."""

from __future__ import annotations

import json
from collections.abc import Mapping
from contextlib import nullcontext
from pathlib import Path
from typing import Any

from ..config import ForgeConfig, Workflow
from ..errors import ForgeError
from ..execution import STATUSES, parse_provider_response
from ..ports import ProjectObserver, RecordCommitter, RecordReader, Runner
from ..records import (
    BundleRecord,
    FinalEvaluationRecord,
    ForgeRecord,
    RecordType,
    WorkflowActionRecord,
    WorkflowResultRecord,
    new_record,
)
from ..serialization import canonical_bytes, local_json_identity
from .execution_receipts import WorkflowExecution, persist_workflow_execution, summarize_binding
from .lifecycle import LifecycleContext
from .support import aggregate_status, now, redact


class WorkflowExecutor:
    """Execute declared workflows through an injected command port and construct typed records."""

    def __init__(
        self,
        *,
        config: ForgeConfig,
        mode: str,
        executor: Runner,
        observer: ProjectObserver,
    ) -> None:
        self.config = config
        self.mode = mode
        self.executor = executor
        self.observer = observer

    def workflow(self, name: str, expected_mode: str) -> Workflow:
        try:
            workflow = self.config.workflows[name]
        except KeyError as exc:
            raise ForgeError("UNDECLARED_COMMAND", f"workflow is not declared: {name}") from exc
        if workflow.mode not in {expected_mode, "both"}:
            raise ForgeError(
                "WORKFLOW_MODE", f"workflow {name} is not declared for {expected_mode} mode"
            )
        return workflow

    def execute(
        self,
        workflow: Workflow,
        candidate: Mapping[str, object],
        *,
        evaluator: bool,
        record_type: RecordType = RecordType.WORKFLOW_RESULT,
    ) -> WorkflowExecution:
        request: dict[str, object] | None = None
        stdin = b""
        if workflow.provider_protocol:
            request = {
                "protocol_version": "0.1",
                "type": "analysis_request",
                "request_id": "forge-"
                + local_json_identity(
                    {
                        "candidate": candidate["candidate_id"],
                        "workflow": workflow.name,
                        "at": now(),
                    }
                ).split(":", 1)[1][:24],
                "analysis": workflow.category,
                "component": {
                    "candidate_identity": candidate["candidate_id"],
                    "source_epoch": candidate["source_epoch"],
                },
                "limits": {
                    "timeout_seconds": self.config.timeout,
                    "output_bytes": self.config.output_cap,
                },
                "extensions": {"mncs_forge": {"mode": self.mode}},
            }
            stdin = canonical_bytes(request) + b"\n"
        workspace = (
            self.observer.provider_workspace(evaluator=evaluator)
            if workflow.provider_protocol or evaluator
            else nullcontext(str(self.config.root))
        )
        action = new_record(
            RecordType.WORKFLOW_ACTION,
            {
                "workflow": workflow.name,
                "candidate_identity": candidate["candidate_id"],
                "mode": self.mode,
                "protocol_request_identity": local_json_identity(request) if request else None,
                "requested_at": now(),
            },
        )
        if not isinstance(action, WorkflowActionRecord):
            raise ForgeError("INTERNAL_RECORD", "workflow action produced an invalid model")
        epoch_identity = candidate.get("source_epoch")
        with workspace as workspace_path:
            session = self.executor.run(
                workflow.command,
                cwd=Path(workspace_path),
                timeout=self.config.timeout,
                output_cap=self.config.output_cap,
                environment=self.config.environment(workflow),
                stdin=stdin,
            )
        error: ForgeError | None = None
        if session.error_code is not None:
            error = ForgeError(session.error_code, session.error_message or session.error_code)
            return WorkflowExecution(
                action=action,
                result=None,
                session=session,
                workflow=workflow,
                epoch_identity=epoch_identity if isinstance(epoch_identity, str) else None,
                error=error,
            )
        execution = session.result
        if execution is None:
            error = ForgeError("COMMAND_START", "runner produced no execution result")
            return WorkflowExecution(
                action=action,
                result=None,
                session=session,
                workflow=workflow,
                epoch_identity=epoch_identity if isinstance(epoch_identity, str) else None,
                error=error,
            )
        protocol: dict[str, Any] | None = None
        witnesses: list[object]
        limitations: list[object]
        unsupported: list[object]
        if workflow.provider_protocol:
            if execution.returncode != 0:
                error = ForgeError(
                    "PROVIDER_EXIT",
                    f"provider exited {execution.returncode}: "
                    + redact(session.stderr.decode("utf-8", errors="replace")),
                )
                return WorkflowExecution(
                    action=action,
                    result=None,
                    session=session,
                    workflow=workflow,
                    epoch_identity=epoch_identity if isinstance(epoch_identity, str) else None,
                    error=error,
                )
            try:
                protocol = parse_provider_response(session.stdout)
            except ForgeError as exc:
                return WorkflowExecution(
                    action=action,
                    result=None,
                    session=session,
                    workflow=workflow,
                    epoch_identity=epoch_identity if isinstance(epoch_identity, str) else None,
                    error=exc,
                )
            status = str(protocol.get("status", "UNKNOWN"))
            method = str(protocol.get("type"))
            provider = dict(protocol["provider"])
            witnesses = list(protocol.get("witnesses", []))
            limitations = list(protocol.get("limitations", []))
            unsupported = list(protocol.get("extensions", {}).get("unsupported", []))
        else:
            status = "UNKNOWN"
            method = "declared-command"
            provider = {"id": workflow.provider_id or workflow.name, "kind": "declared-workflow"}
            witnesses = []
            limitations = [
                "command completion is not evidence PASS; "
                "no validated structured result was emitted"
            ]
            unsupported = []
            if execution.returncode != 0:
                status = "FAIL"
                witnesses = [{"exit_code": execution.returncode}]
                limitations = []
            elif session.stdout:
                try:
                    value = json.loads(session.stdout)
                    if isinstance(value, dict) and value.get("status") in STATUSES:
                        status = str(value["status"])
                        witnesses = list(value.get("witnesses", []))
                        limitations = list(value.get("limitations", []))
                        unsupported = list(value.get("unsupported_constructs", []))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    pass
        if evaluator and workflow.disclosure == "status-only":
            witnesses = []
        record = new_record(
            record_type,
            {
                "candidate_identity": candidate["candidate_id"],
                "subject_type": workflow.subject,
                "provider_or_evaluator_identity": provider,
                "method": method,
                "workflow": workflow.name,
                "category": workflow.category,
                "scope": "declared configuration paths",
                "environment": {
                    "allowlisted_keys": sorted(self.config.environment(workflow)),
                    "values_disclosed": False,
                },
                "duration_seconds": execution.duration_seconds,
                "status": status,
                "witnesses_or_counterexamples": witnesses[:20],
                "limitations": limitations[:20],
                "unsupported_constructs": unsupported[:20],
                "stderr_diagnostic": redact(session.stderr.decode("utf-8", errors="replace")),
                "returncode": execution.returncode,
                "recorded_at": now(),
                "protocol_request_identity": action["protocol_request_identity"],
            },
        )
        if not isinstance(record, (WorkflowResultRecord, FinalEvaluationRecord, BundleRecord)):
            raise ForgeError("INTERNAL_RECORD", "workflow produced an invalid record model")
        return WorkflowExecution(
            action=action,
            result=record,
            session=session,
            workflow=workflow,
            epoch_identity=epoch_identity if isinstance(epoch_identity, str) else None,
            error=None,
        )

    def run(
        self,
        workflow: Workflow,
        candidate: Mapping[str, object],
        *,
        evaluator: bool,
        record_type: RecordType = RecordType.WORKFLOW_RESULT,
    ) -> WorkflowResultRecord | FinalEvaluationRecord | BundleRecord:
        """Compatibility wrapper that returns only the result or raises."""

        execution = self.execute(workflow, candidate, evaluator=evaluator, record_type=record_type)
        if execution.error is not None:
            raise execution.error
        if execution.result is None:
            raise ForgeError("INTERNAL_RECORD", "workflow produced no result")
        return execution.result


class DevelopmentWorkflowService:
    def __init__(
        self,
        *,
        config: ForgeConfig,
        mode: str,
        records: RecordReader,
        record_store: RecordCommitter,
        lifecycle: LifecycleContext,
        workflows: WorkflowExecutor,
    ) -> None:
        self.config = config
        self.mode = mode
        self.records = records
        self.record_store = record_store
        self.lifecycle = lifecycle
        self.workflows = workflows

    def run(self, workflow_names: list[str], candidate_id: str | None = None) -> dict[str, object]:
        state_machine = self.lifecycle.machine()
        results: list[WorkflowResultRecord] = []
        receipts: list[dict[str, object]] = []
        candidate: ForgeRecord | None = None
        for name in workflow_names:
            workflow = self.workflows.workflow(name, "development")
            subject: Mapping[str, object]
            if workflow.subject == "project":
                state_machine.authorize_development_work(candidate_id, project_scoped=True)
                subject = {
                    "candidate_id": f"project:{self.config.project_identity}",
                    "source_epoch": None,
                }
            else:
                candidate = candidate or state_machine.authorize_development_work(
                    candidate_id, project_scoped=False
                )
                if candidate is None:
                    raise ForgeError("NO_CANDIDATE", "candidate workflow requires a candidate")
                subject = candidate
            execution = self.workflows.execute(workflow, subject, evaluator=False)
            binding = persist_workflow_execution(
                config=self.config,
                records=self.records,
                record_store=self.record_store,
                execution=execution,
            )
            if execution.error is not None:
                raise execution.error
            result = execution.result
            if not isinstance(result, WorkflowResultRecord):
                raise ForgeError("INTERNAL_RECORD", "development check produced invalid model")
            results.append(result)
            receipts.append(summarize_binding(binding))
        return {
            "candidate_identity": candidate["candidate_id"] if candidate else None,
            "subject_identities": sorted({str(item["candidate_identity"]) for item in results}),
            "results": [result.to_object_dict() for result in results],
            "execution_receipts": receipts,
            "aggregate_status": aggregate_status(
                (str(item["status"]) for item in results),
                native_status_aggregate=self.lifecycle.native_status_aggregate,
            ),
            "dominance": "FAIL > UNKNOWN > PASS",
        }

    def result_records(self, candidate_id: str | None = None) -> list[ForgeRecord]:
        results = [entry.payload for entry in self.records.records("result")]
        if candidate_id is not None:
            return [item for item in results if item.get("candidate_identity") == candidate_id]
        return results

    def explain(self, output_identity: str | None = None) -> dict[str, object]:
        results = self.result_records()
        if output_identity:
            results = [item for item in results if item.get("output_identity") == output_identity]
        if not results:
            raise ForgeError("RESULT_NOT_FOUND", "no matching check result exists")
        result = results[-1].to_object_dict()
        status = str(result["status"])
        base: dict[str, object] = {
            "status": status,
            "candidate_identity": result["candidate_identity"],
            "workflow": result["workflow"],
            "affected_claim": result["category"],
            "repair_allowed": self.mode == "development",
        }
        if status == "FAIL":
            raw_witnesses = result["witnesses_or_counterexamples"]
            witnesses = raw_witnesses if isinstance(raw_witnesses, list) else []
            base.update(
                {
                    "violated_invariant_or_gate": result["category"],
                    "witness_or_counterexample": witnesses,
                    "relevant_locations": [
                        item.get("location")
                        for item in witnesses
                        if isinstance(item, dict) and item.get("location")
                    ],
                    "permitted_next_actions": (
                        [
                            "inspect compact witness",
                            "repair within declared write paths",
                            "rerun check",
                        ]
                        if self.mode == "development"
                        else ["record rejection", "start a new development epoch"]
                    ),
                }
            )
        elif status == "UNKNOWN":
            base.update(
                {
                    "exact_unresolved_fact": result["limitations"]
                    or result["unsupported_constructs"]
                    or ["provider did not establish PASS or FAIL"],
                    "provider_limitation": result["limitations"],
                    "required_evidence_or_provider": (
                        "a declared provider that supports the unresolved semantics"
                    ),
                    "uncertainty_under_current_policy": (
                        "mandatory until a declared policy says otherwise"
                    ),
                }
            )
        else:
            base["message"] = "result is PASS; there is no failure or unknown to explain"
        return base
