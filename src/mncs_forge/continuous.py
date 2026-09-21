"""Generation-bound continuous development supervision.

This module is deliberately a policy executor, not a semantic implementation:
Language Service events provide compiler facts, RAVEL selects tests, mncs-test
executes the selected identities, Doctor owns Safe repairs, and mncs-debug owns
failure evidence.  Forge only matches checked-in trigger declarations, binds
every job to a generation, and suppresses stale results.
"""

from __future__ import annotations

import fnmatch
import json
import os
import time
from collections import Counter, deque
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote, urlparse

from .application.support import now
from .errors import ForgeError
from .records import RecordType, new_record
from .serialization import local_json_identity
from .verifier_support import changed_path_identity

if TYPE_CHECKING:
    from .engine import Forge

COST_ORDER = {"low": 0, "medium": 1, "high": 2}
CONTINUOUS_STATUS_SCHEMA = "mncs.continuous-status/1"
REPAIR_RESULT_SCHEMA = "mncs.continuous-repair/1"


def _mapping(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: object) -> list[str]:
    return [str(item) for item in value] if isinstance(value, list) else []


def _source_path(uri: str, root: Path) -> Path:
    parsed = urlparse(uri)
    path = Path(unquote(parsed.path)) if parsed.scheme == "file" else Path(uri)
    try:
        return path.resolve().relative_to(root.resolve())
    except ValueError as exc:
        raise ForgeError("CONTINUOUS_SCOPE", f"event source is outside Forge root: {uri}") from exc


class LanguageServiceSocket:
    """Small bounded client for the Language Service JSON-line host."""

    def __init__(self, path: Path, *, timeout: float = 30.0) -> None:
        self.path = path
        self.timeout = timeout

    def request(self, method: str, params: dict[str, object] | None = None) -> object:
        import socket

        request = {
            "id": 1,
            "method": method,
            "params": params or {},
        }
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(self.timeout)
                client.connect(str(self.path))
                client.sendall((json.dumps(request, separators=(",", ":")) + "\n").encode())
                data = bytearray()
                while len(data) < 8 * 1024 * 1024:
                    chunk = client.recv(65536)
                    if not chunk:
                        break
                    data.extend(chunk)
                    if b"\n" in chunk:
                        break
        except OSError as exc:
            raise ForgeError("LANGUAGE_SERVICE_UNAVAILABLE", str(exc)) from exc
        try:
            response = json.loads(bytes(data).splitlines()[0])
        except (IndexError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ForgeError("LANGUAGE_SERVICE_PROTOCOL", "resident response was not JSON") from exc
        if not isinstance(response, dict) or response.get("ok") is not True:
            raise ForgeError(
                "LANGUAGE_SERVICE_REQUEST",
                str(_mapping(response).get("error", "resident request failed")),
            )
        return response.get("result")


class ContinuousSupervisor:
    def __init__(self, forge: Forge) -> None:
        self.forge = forge
        self.config = forge.config
        self.settings = self.config.continuous_settings
        self.statuses: list[str] = []
        self.pending: dict[str, dict[str, object]] = {}
        self.attention: list[dict[str, object]] = []
        self.repairs: list[dict[str, object]] = []
        self.selected_test_ids: list[str] = []
        self.stale_jobs = 0
        self.cancelled_jobs = 0
        self.reused_evidence = 0
        self.recomputed_evidence = 0
        self.current_generation = 0
        self.current_source_identity: str | None = None
        self.current_cursor = 0
        self.active_tier = "edit-time"
        self._queued: deque[dict[str, object]] = deque()

    def _socket(self) -> LanguageServiceSocket:
        value = self.settings.get("language_service_socket", ".mncs/mnls-language-service.sock")
        return LanguageServiceSocket(self.config.root / str(value))

    def _status_path(self) -> Path:
        return self.config.state_dir / "continuous" / "status.json"

    def _write_status(self, status: dict[str, object]) -> None:
        path = self._status_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(status, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    def _attention(
        self, event: dict[str, object], reason: str, *, tier: str = "incremental"
    ) -> None:
        entry = {
            "generation": event.get("current_generation"),
            "cursor": event.get("cursor"),
            "source_identity": _mapping(event.get("current")).get("identity"),
            "reason": reason,
            "tier": tier,
        }
        if entry not in self.attention:
            self.attention.append(entry)

    def _escalate(
        self,
        event: dict[str, object],
        trigger: dict[str, object],
        reason: str,
        *,
        status: str = "UNKNOWN",
        tier: str = "incremental",
    ) -> None:
        policy = str(trigger.get("escalation", "attention"))
        if policy == "silent" or (policy == "unknown" and status != "UNKNOWN"):
            return
        self._attention(event, reason, tier=tier)

    def _record_status(self, status: str) -> None:
        if status in {"PASS", "FAIL", "UNKNOWN"}:
            self.statuses.append(status)

    def _run_command(self, command: list[str], *, cwd: Path, timeout: float) -> dict[str, object]:
        environment = {
            key: os.environ[key]
            for key in self.config.raw.get("environment_allowlist", [])
            if key in os.environ
        }
        session = self.forge._executor.run(  # type: ignore[attr-defined]
            command,
            cwd=cwd,
            timeout=timeout,
            output_cap=self.config.output_cap,
            environment=environment,
        )
        result = session.result
        stdout = result.stdout.decode("utf-8", errors="replace") if result else ""
        stderr = (
            result.stderr.decode("utf-8", errors="replace")
            if result
            else session.error_message or ""
        )
        return {
            "command": list(command),
            "returncode": result.returncode if result else None,
            "stdout": stdout[-self.config.output_cap :],
            "stderr": stderr[-4096:],
            "duration_seconds": session.observation.duration_seconds,
            "error_code": session.error_code,
        }

    @staticmethod
    def _stdout_json(execution: dict[str, object]) -> dict[str, object] | None:
        try:
            value = json.loads(str(execution.get("stdout", "")))
        except json.JSONDecodeError:
            return None
        return value if isinstance(value, dict) else None

    def _event_kinds(self, event: dict[str, object]) -> set[str]:
        kinds = {"source_changed"}
        diagnostics = _mapping(event.get("diagnostics"))
        if diagnostics.get("added"):
            kinds.add("diagnostic_added")
        if diagnostics.get("resolved"):
            kinds.add("diagnostic_resolved")
        if event.get("semantic_subjects"):
            kinds.add("semantic_subject_changed")
        obligations = _mapping(event.get("obligations"))
        if (
            obligations.get("added")
            or obligations.get("resolved")
            or obligations.get("status_changed")
        ):
            kinds.add("obligation_changed")
        impact = _mapping(event.get("impact"))
        if "effect_capability" in _list(impact.get("change_kinds")) or "effect_semantics" in _list(
            impact.get("risk_flags")
        ):
            kinds.add("security_boundary_changed")
        if "public_contract" in _list(impact.get("change_kinds")):
            kinds.add("public_contract_changed")
        return kinds

    def _matches(self, trigger: dict[str, object], event: dict[str, object]) -> bool:
        event_kinds = self._event_kinds(event)
        impact = _mapping(event.get("impact"))

        def intersects(key: str, values: list[str]) -> bool:
            if not values:
                return True
            return bool(set(values).intersection(_list(impact.get(key))))

        trigger_event_kinds = _list(trigger.get("event_kinds"))
        if trigger_event_kinds and not event_kinds.intersection(trigger_event_kinds):
            return False
        if not intersects("change_kinds", _list(trigger.get("change_kinds"))):
            return False
        if not intersects("guarantee_domains", _list(trigger.get("guarantee_domains"))):
            return False
        if not intersects("risk_flags", _list(trigger.get("risk_flags"))):
            return False
        subjects = _list(trigger.get("subject_kinds"))
        if subjects:
            node_kinds = {
                str(_mapping(node).get("kind"))
                for node in impact.get("nodes", [])
                if isinstance(node, dict)
            }
            if not node_kinds.intersection(subjects):
                return False
        contract_identities = _list(trigger.get("contract_identities"))
        if contract_identities:
            changed_contracts = {
                str(_mapping(node).get("identity"))
                for node in impact.get("nodes", [])
                if isinstance(node, dict) and _mapping(node).get("kind") == "contract"
            }
            if not any(
                fnmatch.fnmatch(identity, pattern)
                for identity in changed_contracts
                for pattern in contract_identities
            ):
                return False
        obligation_identities = _list(trigger.get("obligation_identities"))
        if obligation_identities:
            obligations = _mapping(event.get("obligations"))
            changed_obligations = [
                *_list(obligations.get("added")),
                *_list(obligations.get("resolved")),
                *_list(obligations.get("status_changed")),
            ]
            if not any(
                fnmatch.fnmatch(identity, pattern)
                for identity in changed_obligations
                for pattern in obligation_identities
            ):
                return False
        diagnostics = _list(trigger.get("diagnostics"))
        if diagnostics:
            observed = [
                *_list(_mapping(event.get("diagnostics")).get("added")),
                *_list(_mapping(event.get("diagnostics")).get("resolved")),
            ]
            if not any(
                fnmatch.fnmatch(code, pattern) for code in observed for pattern in diagnostics
            ):
                return False
        return not (
            bool(trigger.get("security", False)) and "security_boundary_changed" not in event_kinds
        )

    def _trigger_cost_allowed(self, trigger: dict[str, object], verifier_id: str) -> bool:
        verifier = self.config.verifiers.get(verifier_id)
        maximum = str(trigger.get("maximum_cost", "high"))
        return verifier is not None and COST_ORDER.get(verifier.cost, 99) <= COST_ORDER.get(
            maximum, -1
        )

    def _debounce_ms(self) -> int:
        values = [int(self.settings.get("debounce_ms", 0) or 0)]
        values.extend(
            int(trigger.get("debounce_ms", 0) or 0)
            for trigger in self.settings.get("triggers", [])
            if isinstance(trigger, dict)
        )
        return min(max(values, default=0), 5000)

    def _current_identity(self, client: LanguageServiceSocket, uri: str) -> str | None:
        try:
            response = _mapping(client.request("document_diagnostics", {"uri": uri}))
            return _mapping(response.get("snapshot")).get("source_identity") or None
        except ForgeError:
            return None

    def _doctor_safe(
        self, client: LanguageServiceSocket, trigger: dict[str, object], event: dict[str, object]
    ) -> dict[str, object]:
        command = self.config.public_commands().get("mncs_doctor")
        current = _mapping(event.get("current"))
        uri = str(current.get("uri", ""))
        if not command:
            self._escalate(
                event,
                trigger,
                "automatic Safe Doctor repair is declared but mncs_doctor is not configured",
            )
            return {"status": "UNKNOWN", "reason": "mncs_doctor command is not declared"}
        try:
            relative = _source_path(uri, self.config.root).as_posix()
        except ForgeError as error:
            self._escalate(event, trigger, str(error))
            return {"status": "UNKNOWN", "reason": str(error)}
        expected_identity = str(current.get("identity", ""))
        if self._current_identity(client, uri) != expected_identity:
            self._escalate(event, trigger, "Safe repair source identity changed before promotion")
            return {"status": "UNKNOWN", "reason": "source identity conflict"}
        base = [
            *command,
            "fix",
            "--root",
            str(self.config.root),
            "--json",
            "--quiet",
            "--changed-path",
            relative,
            "--safe-only",
        ]
        dry = self._run_command(
            [*base, "--dry-run"],
            cwd=self.config.root,
            timeout=float(self.settings.get("max_run_seconds", 300)),
        )
        dry_report = self._stdout_json(dry)
        if dry_report is None:
            self._escalate(
                event,
                trigger,
                "Doctor Safe dry-run did not emit its machine-readable report",
            )
            return {
                "status": "UNKNOWN",
                "reason": "malformed Doctor dry-run report",
                "execution": dry,
            }
        planned = dry_report.get("planned_diffs")
        if not planned:
            self._record_status("PASS")
            return {
                "status": "PASS",
                "applied": [],
                "rounds": 0,
                "reason": "already canonical",
                "dry_run": dry_report,
            }
        applied_execution = self._run_command(
            base, cwd=self.config.root, timeout=float(self.settings.get("max_run_seconds", 300))
        )
        report = self._stdout_json(applied_execution)
        if report is None:
            self._escalate(
                event,
                trigger,
                "Doctor Safe apply did not emit its machine-readable report",
            )
            return {
                "status": "UNKNOWN",
                "reason": "malformed Doctor apply report",
                "execution": applied_execution,
            }
        convergence = (
            report.get("convergence") if isinstance(report.get("convergence"), list) else []
        )
        fix_ids = _list(report.get("fix_identities")) or _list(dry_report.get("fix_identities"))
        fix_ids.extend(
            str(item)
            for entry in convergence
            if isinstance(entry, dict)
            for item in _list(entry.get("applied"))
        )
        migration_ids = _list(report.get("migration_rule_identities")) or _list(
            dry_report.get("migration_rule_identities")
        )
        migration_ids.extend(
            str(step.get("id"))
            for migration in report.get("migrations", [])
            if isinstance(migration, dict)
            for step in migration.get("steps", [])
            if isinstance(step, dict) and step.get("id")
        )
        repair_rounds = max(
            [int(entry.get("iterations", 0)) for entry in convergence if isinstance(entry, dict)]
            or [0]
        )
        if planned and repair_rounds == 0:
            repair_rounds = 1
        result: dict[str, object] = {
            "schema_version": REPAIR_RESULT_SCHEMA,
            "original_workspace_generation": event.get("current_generation"),
            "original_source_identity": expected_identity,
            "resulting_source_identity": None,
            "doctor_fix_identities": sorted(set(fix_ids)),
            "migration_rule_identities": sorted(set(migration_ids)),
            "repair_rounds": repair_rounds,
            "validation_result": report.get("verification")
            or {"status": "PASS" if int(report.get("exit_code", 4)) in {0, 1} else "UNKNOWN"},
            "focused_verification_result": None,
            "failure_conflict_reason": None,
            "report": report,
        }
        try:
            client.request("refresh_workspace", {})
            result["resulting_source_identity"] = self._current_identity(client, uri)
            if not result["resulting_source_identity"]:
                result["failure_conflict_reason"] = (
                    "Language Service did not publish the repaired source identity"
                )
        except ForgeError as error:
            result["failure_conflict_reason"] = str(error)
        self.repairs.append(result)
        self._persist_repair(event, result, applied_execution)
        status = "PASS" if result.get("failure_conflict_reason") is None else "UNKNOWN"
        self._record_status(status)
        if status != "PASS":
            self._escalate(
                event,
                trigger,
                "automatic Safe Doctor repair was not validated",
                status=status,
            )
        return {"status": status, "repair": result}

    def _persist_repair(
        self, event: dict[str, object], repair: dict[str, object], execution: dict[str, object]
    ) -> None:
        fields = {
            "candidate_identity": str(repair.get("original_source_identity")),
            "subject_type": "continuous-safe-repair",
            "provider_or_evaluator_identity": "mncs-doctor",
            "method": "safe-fixpoint",
            "workflow": "continuous.safe-repair",
            "category": "mncs_bundle_validation",
            "scope": "workspace-generation",
            "environment": {"workspace_generation": event.get("current_generation")},
            "duration_seconds": execution.get("duration_seconds"),
            "status": "PASS" if repair.get("failure_conflict_reason") is None else "UNKNOWN",
            "witnesses_or_counterexamples": [repair],
            "limitations": [
                "Doctor owns migration semantics; Forge records only the bounded repair projection."
            ],
            "unsupported_constructs": [],
            "stderr_diagnostic": str(execution.get("stderr", ""))[-4096:],
            "returncode": execution.get("returncode"),
            "protocol_request_identity": local_json_identity(
                {
                    "operation": "continuous.safe-repair",
                    "event_cursor": event.get("cursor"),
                    "workspace_generation": event.get("current_generation"),
                    "source_identity": repair.get("original_source_identity"),
                }
            ),
            "recorded_at": now(),
        }
        try:
            record = new_record(RecordType.WORKFLOW_RESULT, fields)
            self.forge.record_store.commit("results", "result", record)
        except (
            Exception
        ) as error:  # durable evidence failure must be visible, not fatal to source state
            repair["store_persistence"] = {"status": "UNKNOWN", "reason": str(error)}

    def _ravel_plan(self, event: dict[str, object], path: Path, run_dir: Path) -> dict[str, object]:
        commands = self.config.public_commands()
        ravel = commands.get("ravel_impact")
        mncs = commands.get("mncs")
        impact = _mapping(event.get("impact"))
        roots = _list(impact.get("roots"))
        if not ravel or not mncs or not roots:
            raise ForgeError(
                "VERIFICATION_UNKNOWN", "RAVEL, mncs, and compiler roots must be declared"
            )
        change_class = next(iter(_list(impact.get("change_kinds"))), "implementation")
        allowed = {
            "implementation",
            "public_contract",
            "shared_type",
            "parser_semantics",
            "serialization_format",
            "effect_semantics",
            "abi_boundary",
            "cross_repository_contract",
        }
        if change_class not in allowed:
            change_class = "implementation"
        plan_path = run_dir / "verification-plan.json"
        command = [*ravel, str(path), "--mncs", mncs[0]]
        for root in roots:
            command.extend(("--root", root))
        for library in self.settings.get("library_paths", []):
            command.extend(("--library", str(self.config.root / str(library))))
        command.extend(
            (
                "--change-class",
                change_class,
                "--cwd",
                str(self.config.root),
                "--output",
                str(plan_path),
            )
        )
        execution = self._run_command(
            command, cwd=self.config.root, timeout=float(self.settings.get("max_run_seconds", 300))
        )
        if execution.get("returncode") not in {0, None} or not plan_path.is_file():
            raise ForgeError(
                "VERIFICATION_UNKNOWN",
                f"RAVEL did not produce a plan: {execution.get('stderr', '')}",
            )
        try:
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ForgeError(
                "VERIFICATION_UNKNOWN", f"RAVEL plan is not valid JSON: {error}"
            ) from error
        if not isinstance(plan, dict):
            raise ForgeError("VERIFICATION_UNKNOWN", "RAVEL plan is not an object")
        self.forge._mncs_development_service._validate_verification_plan(plan)  # type: ignore[attr-defined]
        return plan

    def _selected_verification(
        self, client: LanguageServiceSocket, event: dict[str, object], trigger: dict[str, object]
    ) -> dict[str, object]:
        impact = _mapping(event.get("impact"))
        if not bool(event.get("impact_complete", False)) or not bool(impact.get("complete", False)):
            self._escalate(
                event,
                trigger,
                "compiler semantic impact is incomplete; exact verification remains UNKNOWN",
            )
            self._record_status("UNKNOWN")
            return {
                "status": "UNKNOWN",
                "reason": "incomplete compiler impact",
                "tier": "incremental",
            }
        manifest = self.settings.get("test_manifest")
        if not manifest:
            self._escalate(
                event,
                trigger,
                "continuous verification trigger matched but test_manifest is not configured",
            )
            self._record_status("UNKNOWN")
            return {"status": "UNKNOWN", "reason": "test manifest is not declared"}
        source_uri = str(_mapping(event.get("current")).get("uri", ""))
        source_path = self.config.root / _source_path(source_uri, self.config.root)
        run_dir = (
            self.config.state_dir
            / "continuous"
            / "runs"
            / f"g{event.get('current_generation')}-c{event.get('cursor')}"
        )
        run_dir.mkdir(parents=True, exist_ok=True)
        try:
            plan = self._ravel_plan(event, source_path, run_dir)
            selected = _list(_mapping(plan.get("selection")).get("selected_test_identities"))
            self.selected_test_ids = sorted(set(selected))
            commands = self.config.public_commands()
            mncs = commands.get("mncs")
            loop = self.forge.mncs_failure_loop(
                manifest=str(manifest),
                test_command=commands.get("mncs_test"),
                debug_command=commands.get("mncs_debug"),
                mncs_binary=mncs[0] if mncs else None,
                library_paths=[
                    str(self.config.root / str(value))
                    for value in self.settings.get("library_paths", [])
                ],
                working_directory=str(self.settings.get("test_working_directory", ".")),
                test_result_file=str(
                    (run_dir / "mncs-test-result.json").relative_to(self.config.root)
                ),
                test_check_file=str(
                    (run_dir / "mncs-test-check.json").relative_to(self.config.root)
                ),
                test_artifacts_directory=str(
                    (run_dir / "mncs-test-artifacts").relative_to(self.config.root)
                ),
                debug_witness_file=str(
                    (run_dir / "mncs-debug-witness.json").relative_to(self.config.root)
                ),
                debug_artifacts_directory=str(
                    (run_dir / "mncs-debug-artifacts").relative_to(self.config.root)
                ),
                capture_policy="events",
                verification_plan_file=str(
                    (run_dir / "verification-plan.json").relative_to(self.config.root)
                ),
                ravel_command=commands.get("ravel_impact"),
                timeout_seconds=float(self.settings.get("max_run_seconds", self.config.timeout)),
                output_file=str((run_dir / "failure-loop.json").relative_to(self.config.root)),
            )
        except ForgeError as error:
            self._escalate(event, trigger, str(error))
            self._record_status("UNKNOWN")
            return {
                "status": "UNKNOWN",
                "reason": str(error),
                "selected_test_identities": self.selected_test_ids,
            }
        current = _mapping(client.request("workspace_status", {}))
        if int(current.get("generation", 0)) > int(event.get("current_generation", 0)):
            self.stale_jobs += 1
            self._record_status("UNKNOWN")
            return {
                "status": "STALE",
                "reason": "superseded while verification was running",
                "selected_test_identities": selected,
            }
        verdict = str(loop.get("verdict", _mapping(loop.get("test")).get("verdict", "UNKNOWN")))
        if verdict not in {"PASS", "FAIL", "UNKNOWN"}:
            verdict = "UNKNOWN"
        self._record_status(verdict)
        if verdict in {"FAIL", "UNKNOWN"}:
            self._escalate(
                event,
                trigger,
                f"selected verification reached {verdict}",
                status=verdict,
            )
        return {
            "status": verdict,
            "selected_test_identities": sorted(set(selected)),
            "selected_test_count": len(selected),
            "available_test_count": _mapping(plan.get("selection")).get("available_test_count"),
            "plan_id": plan.get("plan_id"),
            "failure_loop": loop,
            "tier": "incremental",
        }

    def _reusable_result(
        self, event: dict[str, object], trigger: dict[str, object], verifier_id: str
    ) -> dict[str, object] | None:
        candidate = self.settings.get("candidate_identity")
        if not isinstance(candidate, str) or not candidate:
            return None
        verifier = self.config.verifiers.get(verifier_id)
        if verifier is None:
            return None
        provider = self.config.providers[verifier.provider_id]
        workflow = self.config.workflows[verifier.workflow]
        environment = self.config.environment(workflow)
        service = self.forge._verifier_service  # type: ignore[attr-defined]
        identities = service._material_identities(verifier, provider, workflow, environment)
        try:
            path = _source_path(
                str(_mapping(event.get("current")).get("uri", "")), self.config.root
            ).as_posix()
        except ForgeError:
            return None
        paths = [path]
        path_identities = {path: changed_path_identity(self.config.root, path) for path in paths}
        inputs = {
            "candidate_identity": candidate,
            "changed_path_identities": path_identities,
            "contract_identity": None,
            "dependency_slice_identities": {},
            "prior_artifact_identity": None,
            "question_parameters_identity": local_json_identity({}),
        }
        for entry in reversed(self.forge.ledger.records("verifier_result")):
            payload = entry.payload.to_object_dict()
            if payload.get("verifier_id") != verifier_id or payload.get("mode") != self.forge.mode:
                continue
            if payload.get("candidate_identity") != candidate:
                continue
            if any(payload.get(key) != value for key, value in identities.items()):
                continue
            if payload.get("input_identities") != inputs:
                continue
            envelope = _mapping(payload.get("dependency_envelope"))
            if envelope.get("complete") is not True:
                continue
            freshness = service._freshness(payload, allow_protected=True)
            if freshness.get("state") != "CURRENT":
                continue
            return {
                "status": payload.get("status", "UNKNOWN"),
                "output_identity": payload.get("output_identity"),
                "reused": True,
                "freshness": freshness,
            }
        return None

    def _micro(self, event: dict[str, object], trigger: dict[str, object]) -> dict[str, object]:
        verifier_ids = _list(trigger.get("verifier_ids"))
        if not verifier_ids:
            self._escalate(event, trigger, f"trigger {trigger.get('id')} has no verifier_ids")
            return {"status": "UNKNOWN", "reason": "no verifier declared"}
        candidate = self.settings.get("candidate_identity")
        if not isinstance(candidate, str) or not candidate:
            self._escalate(
                event, trigger, "micro-verifier matched without a configured candidate identity"
            )
            self._record_status("UNKNOWN")
            return {"status": "UNKNOWN", "reason": "candidate identity unavailable"}
        results = []
        for verifier_id in verifier_ids:
            if not self._trigger_cost_allowed(trigger, verifier_id):
                self._escalate(
                    event,
                    trigger,
                    f"trigger {trigger.get('id')} exceeds declared verifier cost boundary",
                )
                self._record_status("UNKNOWN")
                results.append(
                    {
                        "verifier_id": verifier_id,
                        "status": "UNKNOWN",
                        "reason": "cost boundary",
                        "reused": False,
                    }
                )
                continue
            reused = self._reusable_result(event, trigger, verifier_id)
            if reused is not None:
                self.reused_evidence += 1
                results.append({"verifier_id": verifier_id, **reused})
                self._record_status(str(reused["status"]))
                continue
            self.recomputed_evidence += 1
            try:
                path = _source_path(
                    str(_mapping(event.get("current")).get("uri", "")), self.config.root
                ).as_posix()
                result = self.forge.verifier_run(
                    verifier_id, candidate_identity=candidate, changed_paths=[path]
                )
                envelope_complete = (
                    _mapping(result.get("dependency_envelope")).get("complete") is True
                )
                if (
                    bool(trigger.get("complete_dependency_envelope", False))
                    and not envelope_complete
                ):
                    result = {
                        **result,
                        "status": "UNKNOWN",
                        "reuse_blocked": "verifier did not declare a complete dependency envelope",
                    }
                results.append({"verifier_id": verifier_id, **result, "reused": False})
                self._record_status(str(result.get("status", "UNKNOWN")))
            except ForgeError as error:
                self._escalate(event, trigger, f"micro-verifier {verifier_id}: {error}")
                self._record_status("UNKNOWN")
                results.append(
                    {
                        "verifier_id": verifier_id,
                        "status": "UNKNOWN",
                        "reason": str(error),
                        "reused": False,
                    }
                )
        statuses = [str(result.get("status", "UNKNOWN")) for result in results]
        status = "FAIL" if "FAIL" in statuses else "UNKNOWN" if "UNKNOWN" in statuses else "PASS"
        if status in {"FAIL", "UNKNOWN"}:
            self._escalate(
                event,
                trigger,
                f"micro-verification reached {status}",
                status=status,
                tier="micro",
            )
        return {"status": status, "results": results, "tier": "micro"}

    def _process_event(
        self, client: LanguageServiceSocket, event: dict[str, object]
    ) -> dict[str, object]:
        generation = int(event.get("current_generation", 0))
        if generation < self.current_generation:
            self.stale_jobs += 1
            return {"status": "STALE", "reason": "event generation is older than current"}
        self.current_generation = generation
        self.current_source_identity = _mapping(event.get("current")).get("identity") or None
        self.current_cursor = max(self.current_cursor, int(event.get("cursor", 0)))
        triggers = self.settings.get("triggers", [])
        matched = [
            trigger
            for trigger in triggers
            if isinstance(trigger, dict) and self._matches(trigger, event)
        ]
        action_results = []
        repaired = False
        for trigger in matched:
            action = str(trigger.get("action"))
            if action == "doctor_safe":
                result = self._doctor_safe(client, trigger, event)
                action_results.append(
                    {"trigger": trigger.get("id"), "action": action, "result": result}
                )
                repaired = repaired or result.get("repair") is not None
        if repaired:
            self.active_tier = "micro"
            return {
                "generation": generation,
                "cursor": event.get("cursor"),
                "status": "PASS",
                "repair_pending_rebound": True,
                "actions": action_results,
            }
        for trigger in matched:
            action = str(trigger.get("action"))
            if action == "verification_plan":
                self.active_tier = "incremental"
                action_results.append(
                    {
                        "trigger": trigger.get("id"),
                        "action": action,
                        "result": self._selected_verification(client, event, trigger),
                    }
                )
            elif action in {"micro_verifier", "security_micro_verifier"}:
                self.active_tier = "micro"
                action_results.append(
                    {
                        "trigger": trigger.get("id"),
                        "action": action,
                        "result": self._micro(event, trigger),
                    }
                )
        rebound_verification = next(
            (
                action.get("result")
                for action in action_results
                if action.get("action") == "verification_plan"
            ),
            None,
        )
        if rebound_verification is not None:
            identity = self.current_source_identity
            for repair in reversed(self.repairs):
                if (
                    repair.get("resulting_source_identity") == identity
                    and repair.get("focused_verification_result") is None
                ):
                    repair["focused_verification_result"] = rebound_verification
                    break
        try:
            current = _mapping(client.request("workspace_status", {}))
        except ForgeError as error:
            self._attention(event, f"could not confirm continuous result freshness: {error}")
            self._record_status("UNKNOWN")
            return {
                "generation": generation,
                "cursor": event.get("cursor"),
                "status": "UNKNOWN",
                "actions": action_results,
            }
        if int(current.get("generation", 0)) > generation:
            self.stale_jobs += 1
            self._record_status("UNKNOWN")
            return {
                "generation": generation,
                "cursor": event.get("cursor"),
                "status": "STALE",
                "reason": "superseded while continuous work was running",
                "actions": action_results,
            }
        return {
            "generation": generation,
            "cursor": event.get("cursor"),
            "status": "PASS",
            "actions": action_results,
        }

    def status(self) -> dict[str, object]:
        counts = Counter(self.statuses)
        return {
            "schema_version": CONTINUOUS_STATUS_SCHEMA,
            "workspace_generation": self.current_generation,
            "current_source_identity": self.current_source_identity,
            "pending_checks": list(self.pending.values()),
            "counts": {key: counts.get(key, 0) for key in ("PASS", "FAIL", "UNKNOWN")},
            "stale_evidence_count": self.stale_jobs,
            "automatic_repairs_applied": self.repairs,
            "selected_test_identities": self.selected_test_ids,
            "active_verification_tier": self.active_tier,
            "blocking_attention_events": self.attention,
            "event_cursor": self.current_cursor,
            "evidence_reused": self.reused_evidence,
            "evidence_recomputed": self.recomputed_evidence,
            "queued_jobs_cancelled": self.cancelled_jobs,
            "limitations": [
                (
                    "ReferenceCompiler frontend work remains synchronous; late results are "
                    "discarded by generation identity."
                ),
                (
                    "The bounded Language Service cursor may require a reset after transient "
                    "event history ages out."
                ),
            ],
        }

    def read_status(self) -> dict[str, object]:
        path = self._status_path()
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return self.status()
        return value if isinstance(value, dict) else self.status()

    def run(
        self,
        *,
        once: bool = False,
        max_events: int | None = None,
        after_cursor: int | None = None,
        poll_interval_seconds: float | None = None,
    ) -> dict[str, object]:
        if not bool(self.settings.get("enabled", False)):
            self._attention(
                {"current_generation": 0},
                "continuous mode is not enabled in Forge configuration",
                tier="edit-time",
            )
            return self.status()
        client = self._socket()
        limit = int(max_events or self.settings.get("poll_max_events", 32))
        interval = float(poll_interval_seconds or self.settings.get("poll_interval_seconds", 0.2))
        try:
            client.request("refresh_workspace", {})
            if after_cursor is None:
                after_cursor = 0
            poll = _mapping(
                client.request("poll_events", {"after_cursor": after_cursor, "max_events": limit})
            )
        except ForgeError as error:
            self._attention({"current_generation": 0}, str(error), tier="edit-time")
            self._record_status("UNKNOWN")
            result = self.status()
            result["transport"] = "UNKNOWN"
            self._write_status(result)
            return result
        if bool(poll.get("reset_required")):
            self._attention(
                {"current_generation": 0},
                "Language Service event cursor reset required",
                tier="edit-time",
            )
            self._record_status("UNKNOWN")
        events = [event for event in poll.get("events", []) if isinstance(event, dict)]
        debounce_ms = self._debounce_ms()
        if events and debounce_ms:
            # A debounce window is a bounded event-collection policy, not a
            # semantic guess.  Poll from the original cursor so a rapid edit
            # burst is coalesced by the same identity logic below; duplicate
            # cursors are removed before dispatch.
            time.sleep(debounce_ms / 1000)
            try:
                client.request("refresh_workspace", {})
                debounced = _mapping(
                    client.request(
                        "poll_events", {"after_cursor": after_cursor, "max_events": limit}
                    )
                )
                seen_cursors = {
                    int(event.get("cursor", -1))
                    for event in events
                    if isinstance(event.get("cursor"), int)
                }
                events.extend(
                    event
                    for event in debounced.get("events", [])
                    if isinstance(event, dict) and int(event.get("cursor", -1)) not in seen_cursors
                )
                poll["current_cursor"] = max(
                    int(poll.get("current_cursor", 0)),
                    int(debounced.get("current_cursor", 0)),
                )
            except ForgeError as error:
                self._attention(
                    {"current_generation": self.current_generation},
                    f"debounce poll failed: {error}",
                )
                self._record_status("UNKNOWN")
        # Coalesce successive edits of one source to the newest identity.  All
        # omitted generations are stale by construction and cannot publish PASS.
        latest: dict[str, dict[str, object]] = {}
        for event in events:
            uri = str(_mapping(event.get("current")).get("uri", event.get("cursor")))
            previous = latest.get(uri)
            if previous is not None:
                self.cancelled_jobs += 1
            latest[uri] = event
        results = [self._process_event(client, event) for event in latest.values()]
        self.current_cursor = max(
            self.current_cursor, int(poll.get("current_cursor", self.current_cursor))
        )
        if once:
            # A Safe Doctor promotion creates a new filesystem generation.
            # Drain the rebound event in the same bounded invocation so a
            # one-shot supervisor still proves the candidate's repaired state.
            for _ in range(8):
                try:
                    client.request("refresh_workspace", {})
                    rebound = _mapping(
                        client.request(
                            "poll_events",
                            {"after_cursor": self.current_cursor, "max_events": limit},
                        )
                    )
                except ForgeError as error:
                    self._attention({"current_generation": self.current_generation}, str(error))
                    self._record_status("UNKNOWN")
                    break
                rebound_events = [
                    event for event in rebound.get("events", []) if isinstance(event, dict)
                ]
                if not rebound_events:
                    break
                for event in rebound_events:
                    results.append(self._process_event(client, event))
                self.current_cursor = max(
                    self.current_cursor, int(rebound.get("current_cursor", self.current_cursor))
                )
        else:
            while True:
                time.sleep(interval)
                try:
                    client.request("refresh_workspace", {})
                    poll = _mapping(
                        client.request(
                            "poll_events",
                            {"after_cursor": self.current_cursor, "max_events": limit},
                        )
                    )
                except ForgeError as error:
                    self._attention({"current_generation": self.current_generation}, str(error))
                    self._record_status("UNKNOWN")
                    break
                events = [event for event in poll.get("events", []) if isinstance(event, dict)]
                if not events:
                    continue
                for event in events:
                    results.append(self._process_event(client, event))
                self.current_cursor = max(
                    self.current_cursor, int(poll.get("current_cursor", self.current_cursor))
                )
        result = self.status()
        result["events_processed"] = len(results)
        result["event_results"] = results
        self._write_status(result)
        return result
