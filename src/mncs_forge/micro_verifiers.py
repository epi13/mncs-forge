"""Declared machine-native micro-verifier control-plane operations."""

from __future__ import annotations

import time
from collections.abc import Mapping
from math import isfinite
from pathlib import Path
from typing import Any

from .application.execution_receipts import (
    bind_verifier_execution,
    binding_for_action,
    summarize_binding,
)
from .application.lifecycle import LifecycleContext
from .application.support import now, redact
from .config import ForgeConfig, Provider, Verifier, Workflow
from .errors import ForgeError
from .execution import parse_provider_response
from .paths import is_within, resolve_contained, validate_relative_path
from .ports import (
    ExecutionResult,
    ExecutionSession,
    ProjectObserver,
    RecordCommitter,
    RecordReader,
    Runner,
    record_by_id,
)
from .records import (
    ForgeRecord,
    LedgerEntry,
    RecordType,
    VerifierActionRecord,
    VerifierResultRecord,
    derive_record_identity,
    new_record,
)
from .serialization import canonical_bytes, local_json_identity
from .state_machine import ForgeStateMachine
from .verifier_disclosure import redact_status_only_result
from .verifier_support import (
    changed_path_identity,
    parameters_for_verifier,
    resolve_batch_parameters,
    terminal_unknown_result,
    unrecorded_batch_unknown,
)

COST_ORDER = {"low": 0, "medium": 1, "high": 2}
STATUS_ORDER = {"PASS": 0, "UNKNOWN": 1, "FAIL": 2}
RESERVED_PARAMETER_KEYS = {
    "argv",
    "command",
    "env",
    "environment",
    "executable",
    "shell",
    "working_directory",
}


def _aggregate_status(statuses: list[str]) -> str:
    values = [value for value in statuses if value in STATUS_ORDER]
    return max(values, key=STATUS_ORDER.__getitem__) if values else "UNKNOWN"


def _string_list(value: object, *, limit: int = 20) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item)[:1024] for item in value if isinstance(item, str) and item][:limit]


def _redact(text: str, *, limit: int) -> str:
    return redact(text, limit)


class MicroVerifierService:
    """Operate declared verifier capabilities without introducing another runner."""

    def __init__(
        self,
        *,
        config: ForgeConfig,
        mode: str,
        records: RecordReader,
        record_store: RecordCommitter,
        lifecycle: LifecycleContext,
        observer: ProjectObserver,
        executor: Runner,
    ) -> None:
        self.config = config
        self.mode = mode
        self.records = records
        self.record_store = record_store
        self.lifecycle = lifecycle
        self.observer = observer
        self.executor = executor

    def _public(self, verifier: Verifier) -> dict[str, object]:
        provider = self.config.providers[verifier.provider_id]
        return {
            "verifier_id": verifier.verifier_id,
            "version": verifier.version,
            "claim": verifier.claim,
            "category": verifier.category,
            "modes": list(verifier.modes),
            "languages": list(verifier.languages),
            "artifact_types": list(verifier.artifact_types),
            "scopes": list(verifier.scopes),
            "input_kinds": list(verifier.input_kinds),
            "uncertainty_classes": list(verifier.uncertainty_classes),
            "cost": verifier.cost,
            "limitations": list(verifier.limitations),
            "provider_id": verifier.provider_id,
            "provider_identity": provider.identity,
            "provider_version": provider.version,
            "method": verifier.method,
        }

    def list_declared(self) -> dict[str, object]:
        verifiers = [
            self._public(verifier)
            for verifier in sorted(
                self.config.verifiers.values(), key=lambda item: item.verifier_id
            )
        ]
        return {
            "verifiers": verifiers,
            "configured_count": len(verifiers),
            "mode": self.mode,
            "inspection_executed_providers": False,
            "limitations": [
                "declared capability is not evidence that a verifier will PASS",
                "micro-verifier PASS covers only its bounded declared claim",
            ],
        }

    def describe(self, verifier_id: str) -> dict[str, object]:
        verifier = self._verifier(verifier_id)
        return {
            **self._public(verifier),
            "workflow": verifier.workflow,
            "assumptions": list(verifier.assumptions),
            "tags": list(verifier.tags),
            "parameter_keys": list(verifier.parameter_keys),
            "timeout_seconds": verifier.timeout_seconds,
            "disclosure": verifier.disclosure,
            "inspection_executed_provider": False,
            "authority_note": (
                "execution derives the command, method, mode, environment, and disclosure "
                "from this declaration and its referenced workflow/provider"
            ),
        }

    def match(
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
        if maximum_cost not in COST_ORDER:
            raise ForgeError("VERIFIER_MATCH", "maximum_cost must be low, medium, or high")
        mode = active_mode or self.mode
        if mode != self.mode:
            raise ForgeError(
                "MODE_FORBIDDEN",
                f"match mode {mode} differs from active Forge mode {self.mode}",
            )
        requested_uncertainties: list[str] = sorted(set(uncertainty_classes))
        validated_paths = self._validate_changed_paths(changed_paths or [], require_files=False)
        compatible: list[dict[str, object]] = []
        incompatible: list[dict[str, object]] = []
        for verifier in sorted(self.config.verifiers.values(), key=lambda item: item.verifier_id):
            reasons: list[str] = []
            exclusions: list[str] = []
            overlap = sorted(
                set(requested_uncertainties).intersection(verifier.uncertainty_classes)
            )
            if mode not in verifier.modes:
                exclusions.append(f"mode {mode} is not supported")
            else:
                reasons.append(f"supports mode {mode}")
            if requested_uncertainties and not overlap:
                exclusions.append("no requested uncertainty class is declared")
            elif overlap:
                reasons.append("uncertainty classes: " + ", ".join(overlap))
            if language is not None:
                if language in verifier.languages:
                    reasons.append(f"supports language {language}")
                else:
                    exclusions.append(f"language {language} is not supported")
            if artifact_type is not None:
                if artifact_type in verifier.artifact_types:
                    reasons.append(f"supports artifact type {artifact_type}")
                else:
                    exclusions.append(f"artifact type {artifact_type} is not supported")
            if scope is not None:
                if scope in verifier.scopes:
                    reasons.append(f"supports scope {scope}")
                else:
                    exclusions.append(f"scope {scope} is not supported")
            if required_category is not None:
                if required_category == verifier.category:
                    reasons.append(f"category is {required_category}")
                else:
                    exclusions.append(
                        f"category {verifier.category} differs from {required_category}"
                    )
            if COST_ORDER[verifier.cost] > COST_ORDER[maximum_cost]:
                exclusions.append(f"cost {verifier.cost} exceeds maximum {maximum_cost}")
            else:
                reasons.append(f"cost {verifier.cost} is within maximum")
            entry: dict[str, object] = {
                **self._public(verifier),
                "matched_uncertainty_classes": overlap,
                "reasons": reasons,
                "exclusions": exclusions,
            }
            if exclusions:
                incompatible.append(entry)
            else:
                entry["_score"] = len(overlap)
                compatible.append(entry)

        def match_key(item: dict[str, object]) -> tuple[int, int, str]:
            score = item["_score"]
            return (
                COST_ORDER[str(item["cost"])],
                -(score if isinstance(score, int) else 0),
                str(item["verifier_id"]),
            )

        compatible.sort(key=match_key)
        for rank, item in enumerate(compatible, start=1):
            item.pop("_score", None)
            item["rank"] = rank
        return {
            "match_outcome": "MATCHED" if compatible else "NO_MATCH",
            "unresolved_status": None if compatible else "UNKNOWN",
            "matches": compatible,
            "incompatible": incompatible,
            "request": {
                "uncertainty_classes": requested_uncertainties,
                "language": language,
                "artifact_type": artifact_type,
                "changed_paths": validated_paths,
                "scope": scope,
                "active_mode": mode,
                "maximum_cost": maximum_cost,
                "required_category": required_category,
            },
            "execution_performed": False,
            "limitations": (
                []
                if compatible
                else ["no declared verifier can answer the structured uncertainty request"]
            ),
        }

    def run(
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
        timeout_cap: float | None = None,
    ) -> dict[str, object]:
        verifier = self._verifier(verifier_id)
        if self.mode not in verifier.modes:
            raise ForgeError(
                "VERIFIER_MODE",
                f"verifier {verifier_id} is not declared for {self.mode} mode",
            )
        workflow = self.config.workflows[verifier.workflow]
        provider = self.config.providers[verifier.provider_id]
        state_machine = self.lifecycle.machine(
            observe_epoch_authority=False,
            observe_freeze_bindings=self.mode == "evaluator",
            observe_policy=False,
            history_kinds=frozenset({"epoch", "candidate", "disposition", "freeze"}),
        )
        if self.mode == "development":
            if not bool(self.config.raw["authority"]["development"]["may_run_providers"]):
                raise ForgeError(
                    "VERIFIER_AUTHORITY",
                    "development authority does not permit provider execution",
                )
            candidate = state_machine.authorize_development_work(
                candidate_identity, project_scoped=False
            )
            if candidate is None:
                raise ForgeError("NO_CANDIDATE", "verifier requires a candidate")
            freeze: ForgeRecord | None = None
        else:
            freeze, candidate = state_machine.authorize_evaluator_entry(candidate_identity)
        selected_scope = self._scope(verifier, scope)
        selected_paths = self._validate_changed_paths(changed_paths or [], require_files=False)
        region = self._validate_source_region(verifier, source_region)
        dependencies = self._validate_dependency_identities(dependency_slice_identities or {})
        parameters = self._validate_parameters(verifier, question_parameters or {})
        self._validate_input_kinds(
            verifier,
            changed_paths=selected_paths,
            source_region=region,
            contract_identity=contract_identity,
            dependency_slice_identities=dependencies,
            prior_artifact_identity=prior_artifact_identity,
            question_parameters=parameters,
        )
        current_contract_identity = self._contract_identity()
        if contract_identity is not None and contract_identity != current_contract_identity:
            raise ForgeError(
                "CONTRACT_IDENTITY",
                "declared contract identity does not match current configured contracts",
            )
        path_identities = {
            value: changed_path_identity(self.config.root, value) for value in selected_paths
        }
        input_identities: dict[str, object] = {
            "candidate_identity": candidate["candidate_id"],
            "changed_path_identities": path_identities,
            "contract_identity": contract_identity,
            "dependency_slice_identities": dependencies,
            "prior_artifact_identity": prior_artifact_identity,
            "question_parameters_identity": local_json_identity(parameters),
        }
        environment = self.config.environment(workflow)
        identities = self._material_identities(verifier, provider, workflow, environment)
        prior_verifier_results = self.records.records("verifier_result")
        supersedes_output_identity = self._superseded_result(
            verifier.verifier_id,
            str(candidate["candidate_id"]),
            candidate.get("parent_candidate"),
            prior_verifier_results,
        )
        action_fields: dict[str, object] = {
            "verifier_id": verifier.verifier_id,
            "verifier_version": verifier.version,
            "verifier_identity": identities["verifier_identity"],
            "provider_id": provider.provider_id,
            "provider_configuration_identity": identities["provider_configuration_identity"],
            "method": verifier.method,
            "mode": self.mode,
            "epoch_identity": candidate["source_epoch"],
            "candidate_identity": candidate["candidate_id"],
            "candidate_parent_identity": candidate.get("parent_candidate"),
            "freeze_identity": freeze["freeze_id"] if freeze else None,
            "supersedes_output_identity": supersedes_output_identity,
            "scope": selected_scope,
            "changed_paths": selected_paths,
            "source_region": region,
            "input_identities": input_identities,
            "configuration_identity": identities["configuration_identity"],
            "policy_identity": identities["policy_identity"],
            "environment_identity": identities["environment_identity"],
            "requested_at": self._now(),
        }
        action_id = derive_record_identity(RecordType.VERIFIER_ACTION, action_fields)
        action_seed = {**action_fields, "action_id": action_id}
        request = self._request(
            verifier,
            action_seed,
            selected_paths=selected_paths,
            region=region,
            dependencies=dependencies,
            contract_identity=contract_identity,
            prior_artifact_identity=prior_artifact_identity,
            parameters=parameters,
        )
        request_bytes = canonical_bytes(request) + b"\n"
        if len(request_bytes) > int(self.config.verifier_limits["request_bytes"]):
            raise ForgeError(
                "VERIFIER_REQUEST_LIMIT",
                "verifier request exceeds the configured byte limit",
            )
        action = new_record(
            RecordType.VERIFIER_ACTION,
            {**action_seed, "protocol_request_identity": local_json_identity(request)},
        )
        if not isinstance(action, VerifierActionRecord):
            raise ForgeError("INTERNAL_RECORD", "verifier action produced an invalid model")
        with self.record_store.action_execution(str(action["action_id"])):
            self.record_store.commit("verifier-actions", "verifier_action", action)
            started = time.monotonic()
            session: ExecutionSession | None = None
            try:
                session, result = self._execute(
                    verifier,
                    workflow,
                    provider,
                    candidate,
                    action,
                    request,
                    request_bytes,
                    identities,
                    environment,
                    freeze,
                    timeout_cap,
                    prior_verifier_results,
                )
            except Exception as exc:
                code = exc.code if isinstance(exc, ForgeError) else "VERIFIER_INTERNAL"
                message = (
                    exc.message
                    if isinstance(exc, ForgeError)
                    else "unexpected verifier execution failure"
                )
                terminal_fields = terminal_unknown_result(
                    action=action,
                    verifier=verifier,
                    provider=provider,
                    identities=identities,
                    code=code,
                    message=message,
                    recorded_at=self._now(),
                    duration_seconds=time.monotonic() - started,
                )
                terminal_record = new_record(RecordType.VERIFIER_RESULT, terminal_fields)
                if not isinstance(terminal_record, VerifierResultRecord):
                    raise ForgeError(
                        "INTERNAL_RECORD", "terminal verifier result model is invalid"
                    ) from exc
                result = terminal_record
            binding = None
            if session is not None:
                if binding_for_action(self.records, str(action["action_id"])) is not None:
                    raise ForgeError(
                        "RECEIPT_DUPLICATE",
                        f"execution receipt binding already exists for {action['action_id']}",
                    )
                binding = bind_verifier_execution(
                    config=self.config,
                    session=session,
                    action=action,
                    result_identity=str(result["output_identity"]),
                    verifier_id=verifier.verifier_id,
                    verifier_version=verifier.version,
                    provider_id=provider.provider_id,
                )
            ForgeStateMachine.authorize_terminal_result_for_recorded_action(
                action,
                prior_verifier_results,
                action_id=str(action["action_id"]),
                candidate_id=str(result["candidate_identity"]),
                freeze_id=(str(result["freeze_identity"]) if result["freeze_identity"] else None),
                mode=str(result["mode"]),
            )
            commit_batch = getattr(self.record_store, "commit_batch", None)
            if binding is not None and callable(commit_batch):
                commit_batch(
                    [
                        ("execution-receipt-bindings", "execution_receipt_binding", binding),
                        ("verifier-results", "verifier_result", result),
                    ]
                )
            else:
                if binding is not None:
                    self.record_store.commit(
                        "execution-receipt-bindings", "execution_receipt_binding", binding
                    )
                self.record_store.commit("verifier-results", "verifier_result", result)
            disclosed = self._disclose_result(result)
            if binding is not None:
                disclosed["execution_receipt"] = summarize_binding(binding)
            return disclosed

    def batch(
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
        limits = self.config.verifier_limits
        if not verifier_ids:
            raise ForgeError("VERIFIER_BATCH", "batch requires at least one verifier")
        if len(verifier_ids) > int(limits["max_batch"]):
            raise ForgeError(
                "VERIFIER_BATCH_LIMIT",
                f"batch exceeds the {int(limits['max_batch'])}-verifier limit",
            )
        if len(set(verifier_ids)) != len(verifier_ids):
            raise ForgeError("VERIFIER_BATCH", "batch verifier IDs must be unique")
        verifiers = [self._verifier(verifier_id) for verifier_id in verifier_ids]
        configured_duration = sum(verifier.timeout_seconds for verifier in verifiers)
        total_limit = float(limits["batch_timeout_seconds"])
        if configured_duration > total_limit:
            raise ForgeError(
                "VERIFIER_BATCH_LIMIT",
                "sum of verifier timeouts exceeds the configured batch duration",
            )
        try:
            shared, per_verifier = resolve_batch_parameters(verifier_ids, question_parameters)
        except ValueError as exc:
            raise ForgeError("VERIFIER_BATCH_PARAMETERS", str(exc)) from exc
        batch_request: dict[str, object] = {
            "verifier_ids": verifier_ids,
            "candidate_identity": candidate_identity,
            "changed_paths": changed_paths or [],
            "scope": scope,
            "source_region": source_region,
            "contract_identity": contract_identity,
            "dependency_slice_identities": dependency_slice_identities or {},
            "prior_artifact_identity": prior_artifact_identity,
            "question_parameters": question_parameters or {},
        }
        if len(canonical_bytes(batch_request)) > int(limits["request_bytes"]):
            raise ForgeError(
                "VERIFIER_REQUEST_LIMIT", "batch request exceeds the configured byte limit"
            )
        started = time.monotonic()
        results: list[dict[str, object]] = []
        for verifier in verifiers:
            remaining = total_limit - (time.monotonic() - started)
            if remaining <= 0:
                results.append(
                    unrecorded_batch_unknown(
                        verifier.verifier_id,
                        "VERIFIER_BATCH_LIMIT",
                        "batch duration exhausted before action recording",
                    )
                )
                continue
            parameters = parameters_for_verifier(
                verifier.verifier_id,
                shared,
                per_verifier,
            )
            try:
                result = self.run(
                    verifier.verifier_id,
                    candidate_identity=candidate_identity,
                    changed_paths=changed_paths,
                    scope=scope,
                    source_region=source_region,
                    contract_identity=contract_identity,
                    dependency_slice_identities=dependency_slice_identities,
                    prior_artifact_identity=prior_artifact_identity,
                    question_parameters=parameters,
                    timeout_cap=remaining,
                )
            except ForgeError as exc:
                result = unrecorded_batch_unknown(
                    verifier.verifier_id,
                    exc.code,
                    exc.message,
                )
            results.append(result)
        statuses = [str(result.get("status", "UNKNOWN")) for result in results]
        recorded_count = sum(bool(result.get("output_identity")) for result in results)
        return {
            "results": results,
            "aggregate_status": _aggregate_status(statuses),
            "individual_results_retained": True,
            "recorded_result_count": recorded_count,
            "unrecorded_result_count": len(results) - recorded_count,
            "partial_execution_explicit": True,
            "dominance": "FAIL > UNKNOWN > PASS",
            "duration_seconds": round(time.monotonic() - started, 6),
        }

    def _contract_identity(self) -> str:
        return self.observer.content_identity(self.config.paths("contracts"))

    def _superseded_result(
        self,
        verifier_id: str,
        candidate_identity: str,
        parent_candidate: object,
        results: list[LedgerEntry],
    ) -> str | None:
        lineage = {candidate_identity}
        if parent_candidate is not None:
            lineage.add(str(parent_candidate))
        for entry in reversed(results):
            payload = entry.payload
            if (
                payload.get("verifier_id") == verifier_id
                and payload.get("mode") == self.mode
                and payload.get("candidate_identity") in lineage
            ):
                return str(payload["output_identity"])
        return None

    def explain(self, output_identity: str) -> dict[str, object]:
        result = record_by_id(self.records, "verifier_result", output_identity, "output_identity")
        status_only = result.get("disclosure") == "status-only"
        exposed = result.to_object_dict()
        base: dict[str, object] = {
            "output_identity": output_identity,
            "verifier_id": exposed["verifier_id"],
            "claim": exposed["claim"],
            "status": exposed["status"],
            "mode": exposed["mode"],
            "repair_allowed": result["mode"] == "development" and self.mode == "development",
            "independent_evaluation": False,
            "freshness": self._freshness(result, allow_protected=not status_only),
        }
        if status_only:
            base.update(
                {
                    "witnesses": [],
                    "limitations": [
                        "status-only disclosure withholds repair-enabling verifier details"
                    ],
                    "unsupported_constructs": [],
                    "repair_feedback_withheld": True,
                }
            )
            return base
        base.update(
            {
                "summary": exposed["summary"],
                "witnesses": exposed["witnesses"],
                "assumptions": exposed["assumptions"],
                "limitations": exposed["limitations"],
                "unsupported_constructs": exposed["unsupported_constructs"],
                "dependency_envelope": exposed["dependency_envelope"],
                "operational_error": exposed["operational_error"],
                "repair_feedback_withheld": False,
            }
        )
        return base

    def _verifier(self, verifier_id: str) -> Verifier:
        try:
            return self.config.verifiers[verifier_id]
        except KeyError as exc:
            raise ForgeError(
                "VERIFIER_NOT_DECLARED", f"verifier is not declared: {verifier_id}"
            ) from exc

    @staticmethod
    def _now() -> str:
        return now()

    @staticmethod
    def _scope(verifier: Verifier, scope: str | None) -> str:
        if scope is None:
            if len(verifier.scopes) != 1:
                raise ForgeError(
                    "VERIFIER_SCOPE",
                    "scope is required when a verifier declares more than one scope",
                )
            return verifier.scopes[0]
        if scope not in verifier.scopes:
            raise ForgeError(
                "VERIFIER_SCOPE",
                f"scope {scope} is not supported by verifier {verifier.verifier_id}",
            )
        return scope

    def _validate_changed_paths(self, values: list[str], *, require_files: bool) -> list[str]:
        if len(values) > int(self.config.verifier_limits["max_changed_paths"]):
            raise ForgeError("VERIFIER_PATH_LIMIT", "too many changed paths in verifier request")
        writable = self.config.relative_scopes("candidates", "generated")
        protected = self.config.relative_scopes(
            "contracts",
            "references",
            "evaluators",
            "acceptance_policies",
            "protected",
        )
        result: list[str] = []
        for value in sorted(set(values)):
            relative = validate_relative_path(value)
            if is_within(relative, protected):
                raise ForgeError(
                    "PROTECTED_PATH",
                    f"verifier request cannot select protected path: {value}",
                )
            if not is_within(relative, writable):
                raise ForgeError(
                    "VERIFIER_PATH",
                    f"verifier changed path is outside candidate/generated scopes: {value}",
                )
            resolved = resolve_contained(self.config.root, value, must_exist=require_files)
            if resolved.exists() and not resolved.is_file():
                raise ForgeError("VERIFIER_PATH", f"verifier selected path is not a file: {value}")
            result.append(value)
        return result

    def _validate_source_region(
        self, verifier: Verifier, region: dict[str, object] | None
    ) -> dict[str, object] | None:
        if region is None:
            return None
        if "source_region" not in verifier.input_kinds:
            raise ForgeError("VERIFIER_INPUT", "source_region is not allowed by this verifier")
        if set(region) != {"path", "start_line", "end_line"}:
            raise ForgeError(
                "VERIFIER_REGION",
                "source_region requires only path, start_line, and end_line",
            )
        path = region["path"]
        start = region["start_line"]
        end = region["end_line"]
        if (
            not isinstance(path, str)
            or not isinstance(start, int)
            or isinstance(start, bool)
            or not isinstance(end, int)
            or isinstance(end, bool)
            or start < 1
            or end < start
            or end - start > 2000
        ):
            raise ForgeError("VERIFIER_REGION", "source_region is invalid or unbounded")
        validated = self._validate_changed_paths([path], require_files=True)
        return {"path": validated[0], "start_line": start, "end_line": end}

    def _validate_dependency_identities(self, values: dict[str, str]) -> dict[str, str]:
        if len(values) > int(self.config.verifier_limits["max_dependency_identities"]):
            raise ForgeError("VERIFIER_DEPENDENCY_LIMIT", "too many dependency-slice identities")
        result: dict[str, str] = {}
        for key, value in sorted(values.items()):
            if not key or len(key) > 256 or not value or len(value) > 512:
                raise ForgeError(
                    "VERIFIER_DEPENDENCY", "dependency identities must be bounded strings"
                )
            result[key] = value
        return result

    def _validate_parameters(
        self, verifier: Verifier, values: dict[str, object]
    ) -> dict[str, object]:
        if len(values) > int(self.config.verifier_limits["max_question_parameters"]):
            raise ForgeError("VERIFIER_PARAMETER_LIMIT", "too many verifier question parameters")
        unknown = sorted(set(values).difference(verifier.parameter_keys))
        if unknown:
            raise ForgeError(
                "VERIFIER_PARAMETER",
                "undeclared verifier question parameters: " + ", ".join(unknown),
            )
        forbidden = sorted(set(values).intersection(RESERVED_PARAMETER_KEYS))
        if forbidden:
            raise ForgeError(
                "VERIFIER_PARAMETER",
                "executable parameters are forbidden: " + ", ".join(forbidden),
            )
        self._validate_json_value(values)
        return {key: values[key] for key in sorted(values)}

    def _validate_json_value(self, value: object, *, depth: int = 0) -> None:
        if depth > 5:
            raise ForgeError("VERIFIER_PARAMETER_LIMIT", "question parameters are too deep")
        if value is None or isinstance(value, bool):
            return
        if isinstance(value, (str, int, float)):
            if isinstance(value, str) and len(value) > 4096:
                raise ForgeError(
                    "VERIFIER_PARAMETER_LIMIT", "question parameter string is too long"
                )
            if isinstance(value, float) and not isfinite(value):
                raise ForgeError("VERIFIER_PARAMETER", "question parameter numbers must be finite")
            return
        if isinstance(value, list):
            if len(value) > 128:
                raise ForgeError("VERIFIER_PARAMETER_LIMIT", "question parameter list is too long")
            for item in value:
                self._validate_json_value(item, depth=depth + 1)
            return
        if isinstance(value, dict):
            if len(value) > 128 or not all(
                isinstance(key, str) and 0 < len(key) <= 128 for key in value
            ):
                raise ForgeError("VERIFIER_PARAMETER_LIMIT", "question parameter object is invalid")
            for item in value.values():
                self._validate_json_value(item, depth=depth + 1)
            return
        raise ForgeError("VERIFIER_PARAMETER", "question parameters must be JSON values")

    @staticmethod
    def _validate_input_kinds(
        verifier: Verifier,
        *,
        changed_paths: list[str],
        source_region: dict[str, object] | None,
        contract_identity: str | None,
        dependency_slice_identities: dict[str, str],
        prior_artifact_identity: str | None,
        question_parameters: dict[str, object],
    ) -> None:
        provided = {
            "changed_paths": bool(changed_paths),
            "source_region": source_region is not None,
            "contract_identity": contract_identity is not None,
            "dependency_slice_identities": bool(dependency_slice_identities),
            "prior_artifact_identity": prior_artifact_identity is not None,
            "question_parameters": bool(question_parameters),
        }
        forbidden = sorted(
            key for key, present in provided.items() if present and key not in verifier.input_kinds
        )
        if forbidden:
            raise ForgeError(
                "VERIFIER_INPUT",
                "inputs are not declared by verifier: " + ", ".join(forbidden),
            )

    def _material_identities(
        self,
        verifier: Verifier,
        provider: Provider,
        workflow: Workflow,
        environment: dict[str, str],
    ) -> dict[str, str]:
        provider_configuration = {
            "provider_id": provider.provider_id,
            "name": provider.name,
            "identity": provider.identity,
            "version": provider.version,
            "command": provider.command,
            "capabilities": provider.capabilities,
            "executable_identity": provider.executable_identity,
            "descriptor": provider.descriptor,
        }
        verifier_configuration = {
            **self._public(verifier),
            "workflow": verifier.workflow,
            "assumptions": list(verifier.assumptions),
            "parameter_keys": list(verifier.parameter_keys),
            "timeout_seconds": verifier.timeout_seconds,
            "disclosure": verifier.disclosure,
            "provider_configuration_identity": local_json_identity(provider_configuration),
            "workflow_configuration_identity": local_json_identity(
                {
                    "name": workflow.name,
                    "category": workflow.category,
                    "subject": workflow.subject,
                    "mode": workflow.mode,
                    "command": workflow.command,
                    "provider_protocol": workflow.provider_protocol,
                    "provider_id": workflow.provider_id,
                    "environment": workflow.environment,
                    "disclosure": workflow.disclosure,
                }
            ),
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
        return {
            "configuration_identity": local_json_identity(self.config.raw),
            "provider_configuration_identity": local_json_identity(provider_configuration),
            "verifier_identity": local_json_identity(verifier_configuration),
            "policy_identity": self.observer.content_identity(policy_paths),
            "environment_identity": local_json_identity(environment),
        }

    @staticmethod
    def _request(
        verifier: Verifier,
        action: Mapping[str, object],
        *,
        selected_paths: list[str],
        region: dict[str, object] | None,
        dependencies: dict[str, str],
        contract_identity: str | None,
        prior_artifact_identity: str | None,
        parameters: dict[str, object],
    ) -> dict[str, object]:
        action_id = str(action["action_id"])
        return {
            "protocol_version": "0.1",
            "type": "analysis_request",
            "request_id": "forge-verifier-"
            + local_json_identity({"action_id": action_id}).split(":", 1)[1][:24],
            "analysis": verifier.method,
            "component": {
                "candidate_identity": action["candidate_identity"],
                "source_epoch": action["epoch_identity"],
                "scope": action["scope"],
                "changed_paths": selected_paths,
                "source_region": region,
                "contract_identity": contract_identity,
                "dependency_slice_identities": dependencies,
                "prior_artifact_identity": prior_artifact_identity,
            },
            "limits": {
                "timeout_seconds": verifier.timeout_seconds,
                "request_scope": action["scope"],
            },
            "extensions": {
                "mncs_forge": {
                    "verifier_id": verifier.verifier_id,
                    "verifier_version": verifier.version,
                    "mode": action["mode"],
                    "question_parameters": parameters,
                    "input_identities": action["input_identities"],
                }
            },
        }

    def _execute(
        self,
        verifier: Verifier,
        workflow: Workflow,
        provider: Provider,
        candidate: ForgeRecord,
        action: VerifierActionRecord,
        request: dict[str, object],
        request_bytes: bytes,
        identities: dict[str, str],
        environment: dict[str, str],
        freeze: ForgeRecord | None,
        timeout_cap: float | None,
        prior_verifier_results: list[LedgerEntry],
    ) -> tuple[ExecutionSession | None, VerifierResultRecord]:
        started = time.monotonic()
        execution: ExecutionResult | None = None
        session: ExecutionSession | None = None
        response: dict[str, Any] | None = None
        status = "UNKNOWN"
        summary = "verifier did not establish PASS or FAIL"
        witnesses: list[object] = []
        assumptions = list(verifier.assumptions)
        limitations = list(verifier.limitations)
        unsupported: list[str] = []
        dependency_envelope: dict[str, object] = {
            "paths": [],
            "path_identities": {},
            "additional_identities": {},
            "complete": False,
            "identity": None,
        }
        operational_error: dict[str, str] | None = None
        resource_evidence: dict[str, object] | None = None
        provider_identity: dict[str, Any] | None = None
        provider_response_identity: str | None = None
        provider_executable_identity: str | None = None
        authority_before = self.observer.current_authority_identities()
        candidate_before = self.observer.current_candidate_identity()
        try:
            executable, executable_identity = self.observer.provider_executable(provider)
            timeout = min(
                verifier.timeout_seconds,
                timeout_cap if timeout_cap is not None else verifier.timeout_seconds,
            )
            if timeout <= 0:
                raise ForgeError(
                    "VERIFIER_BATCH_LIMIT", "batch duration exhausted before verifier run"
                )
            with self.observer.provider_workspace(evaluator=self.mode == "evaluator") as workspace:
                session = self.executor.run(
                    [str(executable), *provider.command[1:]],
                    cwd=Path(workspace),
                    timeout=timeout,
                    output_cap=min(
                        self.config.output_cap,
                        int(self.config.verifier_limits["result_bytes"]),
                    ),
                    stderr_cap=min(
                        self.config.output_cap,
                        int(self.config.verifier_limits["stderr_bytes"]),
                    ),
                    environment=environment,
                    stdin=request_bytes,
                )
                if session.result is None:
                    observation = session.observation
                    resource_evidence = {
                        "resource_envelope_identity": observation.resource_envelope.get("identity"),
                        **dict(observation.resource_observations),
                    }
                    raise ForgeError(
                        session.error_code or "COMMAND_START",
                        session.error_message or "verifier provider produced no execution result",
                        details={"resource_evidence": resource_evidence},
                    )
                execution = session.result
            if execution.returncode != 0:
                raise ForgeError(
                    "PROVIDER_EXIT",
                    f"provider exited {execution.returncode}; exit status is not evidence",
                )
            response = parse_provider_response(execution.stdout)
            if response.get("type") != "analysis_response":
                if response.get("type") in {"error", "cancelled"}:
                    raise ForgeError(
                        "PROVIDER_OPERATIONAL",
                        str(response.get("summary") or response.get("message") or response["type"]),
                    )
                raise ForgeError(
                    "PROVIDER_MALFORMED",
                    "verifier run requires an analysis_response",
                )
            if response.get("request_id") != request["request_id"]:
                raise ForgeError(
                    "PROVIDER_MALFORMED", "provider response request identity mismatched"
                )
            provider_identity = dict(response["provider"])
            if (
                provider.identity is not None
                and provider_identity.get("identity") != provider.identity
            ) or (
                provider.version is not None
                and provider_identity.get("version") != provider.version
            ):
                raise ForgeError(
                    "PROVIDER_IDENTITY_DRIFT",
                    f"provider {provider.provider_id} response identity drifted",
                )
            status = str(response["status"])
            summary = str(response.get("summary", ""))[:2048]
            witnesses = self._bounded_witnesses(response.get("witnesses", []))
            limitations.extend(_string_list(response.get("limitations")))
            extensions = dict(response["extensions"])
            forge_extensions = extensions.get("mncs_forge", {})
            if not isinstance(forge_extensions, dict):
                raise ForgeError(
                    "PROVIDER_MALFORMED",
                    "mncs_forge provider extension must be an object",
                )
            assumptions.extend(_string_list(forge_extensions.get("assumptions")))
            unsupported = _string_list(
                extensions.get("unsupported_constructs", extensions.get("unsupported", []))
            )
            dependency_envelope = self._dependency_envelope(
                forge_extensions.get("dependency_envelope")
            )
            provider_response_identity = local_json_identity(response)
            provider_executable_identity = executable_identity
        except ForgeError as exc:
            status = "UNKNOWN"
            operational_error = {"code": exc.code, "message": exc.message}
            candidate_resource_evidence = exc.details.get("resource_evidence")
            if isinstance(candidate_resource_evidence, dict):
                resource_evidence = candidate_resource_evidence
            limitations.append(f"operational verifier failure {exc.code}: {exc.message}")
        if authority_before != self.observer.current_authority_identities():
            raise ForgeError(
                "EVALUATION_DRIFT" if self.mode == "evaluator" else "PROVIDER_MUTATION",
                "authority files changed during verifier execution",
            )
        if candidate_before != self.observer.current_candidate_identity():
            raise ForgeError(
                "EVALUATION_DRIFT" if self.mode == "evaluator" else "PROVIDER_MUTATION",
                "candidate changed during verifier execution",
            )
        if freeze is not None:
            self.lifecycle.verify_freeze(freeze)
        iterative_overlap = any(
            entry.payload.get("mode") == "development"
            and entry.payload.get("candidate_identity") == candidate["candidate_id"]
            and entry.payload.get("verifier_id") == verifier.verifier_id
            for entry in prior_verifier_results
        )
        disclosure = verifier.disclosure
        fields: dict[str, object] = {
            "action_id": action["action_id"],
            "verifier_id": verifier.verifier_id,
            "verifier_version": verifier.version,
            "verifier_identity": identities["verifier_identity"],
            "claim": verifier.claim,
            "category": verifier.category,
            "provider_id": provider.provider_id,
            "provider_configuration_identity": identities["provider_configuration_identity"],
            "provider_executable_identity": provider_executable_identity,
            "provider_identity": provider_identity,
            "provider_response_identity": provider_response_identity,
            "method": verifier.method,
            "mode": self.mode,
            "evidence_class": (
                "development_evidence" if self.mode == "development" else "local_evaluator_evidence"
            ),
            "independent_evaluation": False,
            "iterative_development_overlap": iterative_overlap,
            "epoch_identity": candidate["source_epoch"],
            "candidate_identity": candidate["candidate_id"],
            "candidate_parent_identity": action["candidate_parent_identity"],
            "freeze_identity": freeze["freeze_id"] if freeze else None,
            "supersedes_output_identity": action["supersedes_output_identity"],
            "input_identities": action["input_identities"],
            "configuration_identity": identities["configuration_identity"],
            "policy_identity": identities["policy_identity"],
            "environment_identity": identities["environment_identity"],
            "status": status,
            "summary": summary,
            "witnesses": witnesses,
            "assumptions": list(dict.fromkeys(assumptions))[:20],
            "limitations": list(dict.fromkeys(limitations))[:20],
            "unsupported_constructs": unsupported[:20],
            "dependency_envelope": dependency_envelope,
            "duration_seconds": (
                execution.duration_seconds
                if execution is not None
                else round(time.monotonic() - started, 6)
            ),
            "stderr_diagnostic": (
                _redact(
                    execution.stderr.decode("utf-8", errors="replace"),
                    limit=int(self.config.verifier_limits["stderr_bytes"]),
                )
                if execution is not None
                else ""
            ),
            "returncode": execution.returncode if execution is not None else None,
            "operational_error": operational_error,
            "disclosure": disclosure,
            "recorded_at": self._now(),
        }
        if session is not None and (
            session.observation.resource_envelope or session.observation.resource_observations
        ):
            resource_evidence = {
                "resource_envelope": dict(session.observation.resource_envelope),
                **dict(session.observation.resource_observations),
            }
        if resource_evidence:
            fields["extensions"] = {
                "mncs_forge": {"resource_evidence": resource_evidence}
            }
        if self.mode == "evaluator" and disclosure == "status-only":
            redact_status_only_result(fields)
        result = new_record(RecordType.VERIFIER_RESULT, fields)
        if not isinstance(result, VerifierResultRecord):
            raise ForgeError("INTERNAL_RECORD", "verifier result produced an invalid model")
        if len(canonical_bytes(result.to_json())) > int(
            self.config.verifier_limits["result_bytes"]
        ):
            fields["witnesses"] = []
            fields["stderr_diagnostic"] = ""
            current_limitations = fields["limitations"]
            bounded_limitations = (
                current_limitations[:19] if isinstance(current_limitations, list) else []
            )
            fields["limitations"] = [
                *bounded_limitations,
                "result details were reduced to fit the configured result limit",
            ]
            result = new_record(RecordType.VERIFIER_RESULT, fields)
            if not isinstance(result, VerifierResultRecord):
                raise ForgeError("INTERNAL_RECORD", "bounded verifier result model is invalid")
        if len(canonical_bytes(result.to_json())) > int(
            self.config.verifier_limits["result_bytes"]
        ):
            raise ForgeError(
                "VERIFIER_RESULT_LIMIT",
                "verifier result cannot fit the configured result byte limit",
            )
        return session, result

    def _bounded_witnesses(self, value: object) -> list[object]:
        if not isinstance(value, list):
            return []
        cap = int(self.config.verifier_limits["witness_bytes"])
        result: list[object] = []
        size = 2
        for item in value[:100]:
            self._validate_json_value(item)
            encoded = canonical_bytes(item)
            if size + len(encoded) + 1 > cap or len(result) >= 20:
                break
            result.append(item)
            size += len(encoded) + 1
        return result

    def _dependency_envelope(self, value: object) -> dict[str, object]:
        if value is None:
            return {
                "paths": [],
                "path_identities": {},
                "additional_identities": {},
                "complete": False,
                "identity": None,
            }
        if not isinstance(value, dict) or set(value).difference(
            {"paths", "identities", "complete"}
        ):
            raise ForgeError("PROVIDER_MALFORMED", "dependency envelope is malformed")
        paths = value.get("paths", [])
        identities = value.get("identities", {})
        complete = value.get("complete", False)
        if (
            not isinstance(paths, list)
            or not all(isinstance(path, str) for path in paths)
            or not isinstance(identities, dict)
            or not all(
                isinstance(key, str) and isinstance(identity, str)
                for key, identity in identities.items()
            )
            or not isinstance(complete, bool)
        ):
            raise ForgeError("PROVIDER_MALFORMED", "dependency envelope fields are invalid")
        if len(paths) > int(self.config.verifier_limits["max_changed_paths"]):
            raise ForgeError("PROVIDER_MALFORMED", "dependency envelope has too many paths")
        visible_keys = [
            "candidates",
            "generated",
            "contracts",
            "references",
            "development_evidence",
            "evaluators",
            "acceptance_policies",
        ]
        if self.mode == "evaluator":
            visible_keys.append("protected")
        visible = self.config.relative_scopes(*visible_keys)
        protected = self.config.relative_scopes("protected")
        normalized: list[str] = []
        path_identities: dict[str, str] = {}
        for value_path in sorted(set(paths)):
            relative = validate_relative_path(value_path)
            if not is_within(relative, visible):
                raise ForgeError(
                    "PROVIDER_MALFORMED",
                    f"dependency path is outside declared visible scopes: {value_path}",
                )
            if self.mode == "development" and is_within(relative, protected):
                raise ForgeError(
                    "PROTECTED_PATH",
                    "development verifier dependency envelope selected protected data",
                )
            resolved = resolve_contained(self.config.root, value_path, must_exist=False)
            normalized.append(value_path)
            path_identities[value_path] = self.observer.content_identity([resolved])
        envelope_without_identity: dict[str, object] = {
            "paths": normalized,
            "path_identities": path_identities,
            "additional_identities": {
                str(key): str(identity) for key, identity in sorted(identities.items())
            },
            "complete": complete,
        }
        return {
            **envelope_without_identity,
            "identity": local_json_identity(envelope_without_identity),
        }

    def _freshness(
        self, result: Mapping[str, object], *, allow_protected: bool
    ) -> dict[str, object]:
        verifier = self.config.verifiers.get(str(result["verifier_id"]))
        if verifier is None:
            return {
                "state": "STALE",
                "reason": "verifier declaration is no longer present",
            }
        provider = self.config.providers.get(verifier.provider_id)
        workflow = self.config.workflows.get(verifier.workflow)
        if provider is None or workflow is None:
            return {
                "state": "STALE",
                "reason": "referenced provider or workflow declaration is missing",
            }
        environment = self.config.environment(workflow)
        identities = self._material_identities(verifier, provider, workflow, environment)
        changed_material = [
            key
            for key in (
                "configuration_identity",
                "provider_configuration_identity",
                "verifier_identity",
                "policy_identity",
                "environment_identity",
            )
            if result.get(key) != identities[key]
        ]
        if result.get("mode") != self.mode:
            changed_material.append("mode")
        if changed_material:
            return {
                "state": "STALE",
                "reason": "material verifier identities changed",
                "changed_identities": sorted(changed_material),
            }
        envelope = result.get("dependency_envelope")
        if not isinstance(envelope, Mapping):
            return {
                "state": "UNKNOWN",
                "reason": "result has no valid dependency envelope",
            }
        paths = envelope.get("paths", [])
        recorded = envelope.get("path_identities", {})
        if (
            not allow_protected
            or not isinstance(paths, (list, tuple))
            or not isinstance(recorded, Mapping)
        ):
            return {
                "state": "UNKNOWN",
                "reason": "dependency freshness details are not disclosable",
            }
        stale_paths: list[str] = []
        for path in paths:
            if not isinstance(path, str) or path not in recorded:
                return {
                    "state": "UNKNOWN",
                    "reason": "dependency impact cannot be established",
                }
            resolved = resolve_contained(self.config.root, path, must_exist=False)
            current = self.observer.content_identity([resolved])
            if current != recorded[path]:
                stale_paths.append(path)
        if stale_paths:
            return {
                "state": "STALE",
                "reason": "a declared dependency-envelope identity changed",
                "changed_paths": stale_paths,
            }
        if result.get("candidate_identity") == self.observer.current_candidate_identity():
            return {
                "state": "CURRENT",
                "reason": "all material and candidate identities still match",
            }
        if envelope.get("complete") is True:
            return {
                "state": "CURRENT",
                "reason": (
                    "candidate changed outside an unchanged provider-declared complete "
                    "dependency envelope"
                ),
                "limitation": (
                    "path separation alone is not semantic independence; this conclusion "
                    "depends on the provider's declared complete envelope"
                ),
            }
        return {
            "state": "UNKNOWN",
            "reason": (
                "candidate changed and the provider did not declare a complete dependency envelope"
            ),
        }

    @staticmethod
    def _disclose_result(result: VerifierResultRecord) -> dict[str, object]:
        exposed = result.to_object_dict()
        if result["mode"] == "evaluator" and result["disclosure"] == "status-only":
            return {
                "record_type": exposed["record_type"],
                "schema_version": exposed["schema_version"],
                "output_identity": exposed["output_identity"],
                "verifier_id": exposed["verifier_id"],
                "status": exposed["status"],
                "mode": exposed["mode"],
                "limitations": exposed["limitations"],
                "repair_feedback_withheld": True,
                "independent_evaluation": False,
            }
        return exposed
