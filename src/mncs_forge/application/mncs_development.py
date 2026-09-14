"""Forge's structured MNCS test -> debug -> repair -> verify loop.

This service is deliberately a transport/orchestration boundary.  It invokes
the configured providers with argv arrays, reads their versioned JSON files,
and preserves their identities.  It does not calculate a test oracle or a
debug trace and it never interprets human-readable process output.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path, PurePosixPath
from typing import Any

from ..config import ForgeConfig
from ..errors import ForgeError
from ..paths import is_within, resolve_contained
from ..ports import Runner
from ..serialization import canonical_bytes, local_json_identity, pretty_json, read_json

TEST_RESULT_SCHEMA = "mncs.test-result/1"
CHECK_RESULT_SCHEMA = "mncs.check-result/1"
VERIFICATION_PLAN_SCHEMA = "mncs.verification-plan/1"
VERIFICATION_LEVELS = {
    "changed_item",
    "direct_dependents",
    "affected_subsystem",
    "repository_canonical",
    "family",
}
VERIFICATION_ESCALATION_REASONS = {
    "direct_dependents_affected",
    "public_contract_changed",
    "shared_type_changed",
    "parser_semantics_changed",
    "serialization_format_changed",
    "effect_semantics_changed",
    "abi_boundary_changed",
    "canonical_fixture_changed",
    "high_connectivity_definition_changed",
    "dependent_targeted_test_failed",
    "insufficient_diagnostic_evidence",
    "migration_broad_semantic_surface",
    "language_profile_changed",
    "cross_repository_contract_changed",
    "impact_evidence_truncated",
    "unknown_changed_identity",
}
CAPABILITIES_SCHEMA = "mncs.debug-capabilities/1"
SESSION_SCHEMA = "mncs.debug-session/1"
WITNESS_SCHEMA = "mncs.debug-witness/1"
OBSERVATION_SCHEMA = "mncs.execution-observation/1"
SOURCE_MAP_SCHEMA = "mncs.execution-source-map/1"
VALIDATION_SCHEMA = "mncs.debug-validation/1"
TRACE_SCHEMA = "mncs.debug-trace/1"
INSPECTION_SCHEMA = "mncs.debug-inspection/1"
PROVENANCE_SCHEMA = "mncs.debug-provenance/1"
REPLAY_SCHEMA = "mncs.debug-replay/1"
MINIMIZATION_SCHEMA = "mncs.debug-minimization/1"
VERDICTS = {"PASS", "FAIL", "UNKNOWN"}


def _mapping(value: object) -> dict[str, Any]:
    """Return a JSON object without weakening the typed projection boundary."""

    return value if isinstance(value, dict) else {}


def _object_list(value: object) -> list[dict[str, Any]]:
    """Select JSON objects from a bounded provider array."""

    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class MncsDevelopmentService:
    """Run an explicit, bounded, evidence-preserving MNCS development loop."""

    def __init__(self, *, config: ForgeConfig, executor: Runner) -> None:
        self.config = config
        self.executor = executor

    def _path(self, value: str, *, must_exist: bool = False) -> Path:
        return resolve_contained(self.config.root, value, must_exist=must_exist)

    def _environment(self) -> dict[str, str]:
        allowed = self.config.raw.get("environment_allowlist", [])
        return {key: os.environ[key] for key in allowed if key in os.environ}

    def _command_prefix(self, supplied: list[str] | None, configured_name: str) -> list[str]:
        value = supplied
        if value is None:
            value = self.config.public_commands().get(configured_name)
        if (
            not isinstance(value, list)
            or not value
            or not all(isinstance(item, str) and item for item in value)
        ):
            raise ForgeError(
                "MNCS_PROVIDER_UNAVAILABLE",
                f"{configured_name} command must be declared as a non-empty argv prefix",
            )
        return list(value)

    def _run(
        self, command: list[str], cwd: Path, *, label: str, timeout: float
    ) -> dict[str, object]:
        session = self.executor.run(
            command,
            cwd=cwd,
            timeout=timeout,
            output_cap=self.config.output_cap,
            environment=self._environment(),
        )
        if session.error_code is not None:
            raise ForgeError(
                "MNCS_PROVIDER_EXECUTION",
                f"{label} could not be executed: {session.error_message or session.error_code}",
            )
        result = session.result
        observation = session.observation
        return {
            "label": label,
            "command": list(result.argv) if result is not None else command,
            "command_identity": observation.command_identity,
            "returncode": result.returncode if result is not None else observation.returncode,
            "termination_category": observation.termination_category,
            "duration_seconds": observation.duration_seconds,
            "stdout": observation.stdout.to_dict(),
            "stderr": observation.stderr.to_dict(),
            "runner_identity": observation.runner_identity,
            "runner_version": observation.runner_version,
        }

    def _read(self, path: Path, *, label: str, byte_cap: int | None = None) -> dict[str, Any]:
        try:
            value = read_json(
                path,
                byte_cap=byte_cap if byte_cap is not None else max(self.config.output_cap, 131072),
            )
        except (OSError, ValueError) as exc:
            raise ForgeError(
                "PROVIDER_CONTRACT_INVALID", f"{label} is not valid JSON: {exc}"
            ) from exc
        if not isinstance(value, dict):
            raise ForgeError("PROVIDER_CONTRACT_INVALID", f"{label} must be a JSON object")
        return value

    def _relative(self, path: Path) -> str:
        return path.resolve().relative_to(self.config.root.resolve()).as_posix()

    def _ref(self, path: Path, kind: str, schema: str | None = None) -> dict[str, object]:
        value: dict[str, object] = {
            "kind": kind,
            "path": self._relative(path),
            "sha256": _sha256(path),
        }
        if schema is not None:
            value["schema_revision"] = schema
        return value

    @staticmethod
    def _validate_test_result(value: dict[str, Any]) -> None:
        required = ("schema_version", "provider", "verdict", "tests", "run_id")
        if (
            value.get("schema_version") != TEST_RESULT_SCHEMA
            or value.get("provider") != "mncs-test"
        ):
            raise ForgeError(
                "PROVIDER_CONTRACT_INVALID", "test provider did not emit mncs.test-result/1"
            )
        missing = [field for field in required if field not in value]
        if (
            missing
            or value.get("verdict") not in VERDICTS
            or not isinstance(value.get("tests"), list)
        ):
            raise ForgeError(
                "PROVIDER_CONTRACT_INVALID", f"test result is missing or invalid: {missing}"
            )
        if not isinstance(value.get("run_id"), str) or not value["run_id"]:
            raise ForgeError("PROVIDER_CONTRACT_INVALID", "test result run_id must be non-empty")
        for index, item in enumerate(value["tests"]):
            if (
                not isinstance(item, dict)
                or not isinstance(item.get("id"), str)
                or item.get("verdict") not in VERDICTS
            ):
                raise ForgeError(
                    "PROVIDER_CONTRACT_INVALID",
                    f"test result tests[{index}] is not a valid TestExecution projection",
                )

    @staticmethod
    def _validate_check(value: dict[str, Any], provider: str) -> None:
        if value.get("schema_version") != CHECK_RESULT_SCHEMA or value.get("provider") != provider:
            raise ForgeError(
                "PROVIDER_CONTRACT_INVALID", f"{provider} did not emit mncs.check-result/1"
            )
        if value.get("verdict") not in VERDICTS:
            raise ForgeError("PROVIDER_CONTRACT_INVALID", f"{provider} check verdict is invalid")

    @staticmethod
    def _validate_verification_plan(value: dict[str, Any]) -> None:
        """Validate the compact RAVEL plan without reimplementing selection."""

        if value.get("schema_version") != VERIFICATION_PLAN_SCHEMA:
            raise ForgeError(
                "PROVIDER_CONTRACT_INVALID",
                f"verification plan did not emit {VERIFICATION_PLAN_SCHEMA}",
            )
        plan_id = value.get("plan_id")
        if (
            not isinstance(plan_id, str)
            or len(plan_id) != 64
            or any(character not in "0123456789abcdef" for character in plan_id)
        ):
            raise ForgeError("PROVIDER_CONTRACT_INVALID", "verification plan plan_id is not a sha256 identity")
        source = value.get("source")
        if not isinstance(source, dict) or not isinstance(source.get("path"), str):
            raise ForgeError("PROVIDER_CONTRACT_INVALID", "verification plan source binding is missing")
        source_sha256 = source.get("sha256")
        if (
            not isinstance(source_sha256, str)
            or len(source_sha256) != 64
            or any(character not in "0123456789abcdef" for character in source_sha256)
        ):
            raise ForgeError("PROVIDER_CONTRACT_INVALID", "verification plan source sha256 is invalid")
        impact = value.get("impact")
        if not isinstance(impact, dict) or not isinstance(impact.get("graph_identity"), str):
            raise ForgeError("PROVIDER_CONTRACT_INVALID", "verification plan impact identity is missing")
        if not isinstance(impact.get("roots"), list) or not all(
            isinstance(item, str) and item for item in impact["roots"]
        ):
            raise ForgeError("PROVIDER_CONTRACT_INVALID", "verification plan impact roots are invalid")
        if not isinstance(impact.get("affected_count"), int) or isinstance(
            impact["affected_count"], bool
        ) or impact["affected_count"] < 0:
            raise ForgeError("PROVIDER_CONTRACT_INVALID", "verification plan affected count is invalid")
        for field in ("direct_dependents", "test_identities", "risk_flags", "limitations"):
            values = impact.get(field)
            if not isinstance(values, list) or not all(
                isinstance(item, str) and (item or field == "limitations") for item in values
            ):
                raise ForgeError(
                    "PROVIDER_CONTRACT_INVALID", f"verification plan impact {field} is invalid"
                )
        if not isinstance(impact.get("complete"), bool):
            raise ForgeError("PROVIDER_CONTRACT_INVALID", "verification plan impact completeness is invalid")
        selection = value.get("selection")
        if not isinstance(selection, dict) or selection.get("level") not in VERIFICATION_LEVELS:
            raise ForgeError("PROVIDER_CONTRACT_INVALID", "verification plan selection level is invalid")
        selected = selection.get("selected_test_identities")
        if not isinstance(selected, list) or not all(isinstance(item, str) and item for item in selected):
            raise ForgeError("PROVIDER_CONTRACT_INVALID", "verification plan test identities are invalid")
        reasons = selection.get("escalation_reasons", [])
        if not isinstance(reasons, list) or not all(isinstance(item, str) and item for item in reasons):
            raise ForgeError("PROVIDER_CONTRACT_INVALID", "verification plan escalation reasons are invalid")
        unknown_reasons = sorted(set(reasons) - VERIFICATION_ESCALATION_REASONS)
        if unknown_reasons:
            raise ForgeError(
                "PROVIDER_CONTRACT_INVALID",
                "verification plan has unknown escalation reasons: " + ", ".join(unknown_reasons),
            )
        available = selection.get("available_test_count")
        if not isinstance(available, int) or isinstance(available, bool) or available < 0:
            raise ForgeError(
                "PROVIDER_CONTRACT_INVALID", "verification plan available test count is invalid"
            )
        proof = value.get("proof")
        if not isinstance(proof, dict) or not isinstance(proof.get("sufficient_to_stop"), bool):
            raise ForgeError("PROVIDER_CONTRACT_INVALID", "verification plan proof stop condition is missing")
        required_evidence = proof.get("required_evidence")
        if not isinstance(required_evidence, list) or not all(
            isinstance(item, str) and item for item in required_evidence
        ):
            raise ForgeError("PROVIDER_CONTRACT_INVALID", "verification plan required evidence is invalid")
        if not isinstance(value.get("provenance"), dict):
            raise ForgeError("PROVIDER_CONTRACT_INVALID", "verification plan provenance is missing")

    def _plan_source(self, plan: dict[str, Any]) -> Path:
        source = _mapping(plan.get("source"))
        value = source.get("path")
        if not isinstance(value, str) or not value:
            raise ForgeError("PROVIDER_CONTRACT_INVALID", "verification plan source path is missing")
        candidate = Path(value)
        if not candidate.is_absolute():
            candidate = self._path(value)
        try:
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(self.config.root.resolve())
        except (OSError, ValueError) as exc:
            raise ForgeError(
                "PROVIDER_CONTRACT_INVALID",
                "verification plan source is outside the Forge project or unavailable",
            ) from exc
        return resolved

    def _plan_projection(self, plan: dict[str, Any], plan_ref: dict[str, object]) -> dict[str, Any]:
        impact = _mapping(plan.get("impact"))
        selection = _mapping(plan.get("selection"))
        return {
            "plan_id": plan.get("plan_id"),
            "reference": plan_ref,
            "graph_identity": impact.get("graph_identity"),
            "roots": impact.get("roots", []),
            "affected_surface_size": impact.get("affected_count", 0),
            "direct_dependents": impact.get("direct_dependents", []),
            "risk_flags": impact.get("risk_flags", []),
            "complete": impact.get("complete"),
            "level": selection.get("level"),
            "selected_test_count": len(selection.get("selected_test_identities", []))
            if isinstance(selection.get("selected_test_identities"), list)
            else 0,
            "available_test_count": selection.get("available_test_count"),
            "escalation_reasons": selection.get("escalation_reasons", []),
            "sufficient_to_stop": _mapping(plan.get("proof")).get("sufficient_to_stop"),
        }

    def _test_command(
        self,
        prefix: list[str],
        *,
        manifest: Path,
        result: Path,
        check: Path,
        artifacts: Path,
        mncs_binary: str | None,
        library_paths: list[str] | None,
        embed_library: str | None,
        verification_plan: Path | None = None,
    ) -> list[str]:
        command = [
            *prefix,
            "run",
            "--manifest",
            str(manifest),
            "--result",
            str(result),
            "--check-result",
            str(check),
            "--artifacts",
            str(artifacts),
        ]
        if mncs_binary:
            command.extend(("--mncs", mncs_binary))
        for path in library_paths or []:
            command.extend(("--library", path))
        if embed_library:
            command.extend(("--embed-library", embed_library))
        if verification_plan is not None:
            command.extend(("--verification-plan", str(verification_plan)))
        return command

    def _debug_commands(
        self,
        prefix: list[str],
        *,
        test_result: Path,
        test_id: str,
        witness: Path,
        artifacts: Path,
        cwd: Path,
        mncs_binary: str | None,
        library_paths: list[str] | None,
        capture_policy: str,
        max_events: int,
        max_values: int,
        max_value_bytes: int,
        selected_operations: list[str] | None,
        timeout: float,
        minimize: bool,
        diagnostic_depth: str = "minimal",
        include_import: bool = True,
    ) -> list[tuple[str, list[str], Path]]:
        common: list[str] = []
        if mncs_binary:
            common.extend(("--mncs", mncs_binary))
        common.extend(
            (
                "--cwd",
                str(cwd),
                "--capture",
                capture_policy,
                "--max-events",
                str(max_events),
                "--max-values",
                str(max_values),
                "--max-value-bytes",
                str(max_value_bytes),
                "--timeout",
                str(timeout),
            )
        )
        if capture_policy == "selected":
            for operation in selected_operations or []:
                common.extend(("--operation", operation))
        for path in library_paths or []:
            common.extend(("--library", path))
        commands: list[tuple[str, list[str], Path]] = []

        def add(label: str, command: list[str], path: Path) -> None:
            commands.append((label, command, path))

        if diagnostic_depth == "deep":
            capabilities_command = [*prefix, "capabilities"]
            if mncs_binary:
                capabilities_command.extend(("--mncs", mncs_binary))
            capabilities_command.extend(("--output", str(artifacts / "capabilities.json")))
            add("debug-capabilities", capabilities_command, artifacts / "capabilities.json")
        if include_import:
            add(
                "debug-import",
                [
                    *prefix,
                    "import-test",
                    str(test_result),
                    "--test-id",
                    test_id,
                    *common,
                    "--output",
                    str(witness),
                ],
                witness,
            )
        add(
            "debug-validation",
            [
                *prefix,
                "validate",
                str(witness),
                "--output",
                str(artifacts / "validation.json"),
            ],
            artifacts / "validation.json",
        )
        if diagnostic_depth == "deep":
            add(
                "debug-open",
                [*prefix, "open", str(witness), "--output", str(artifacts / "session.json")],
                artifacts / "session.json",
            )
        add(
            "debug-inspection",
            [
                *prefix,
                "inspect",
                str(witness),
                "--output",
                str(artifacts / "inspection.json"),
            ],
            artifacts / "inspection.json",
        )
        if diagnostic_depth in {"standard", "deep"}:
            add(
                "debug-trace",
                [
                    *prefix,
                    "trace",
                    str(witness),
                    "--limit",
                    str(max_events),
                    "--output",
                    str(artifacts / "trace.json"),
                ],
                artifacts / "trace.json",
            )
            add(
                "debug-provenance",
                [*prefix, "why", str(witness), "--output", str(artifacts / "provenance.json")],
                artifacts / "provenance.json",
            )
        if diagnostic_depth == "deep":
            add(
                "debug-replay",
                [
                    *prefix,
                    "replay",
                    str(witness),
                    "--mode",
                    "trace",
                    "--output",
                    str(artifacts / "replay.json"),
                ],
                artifacts / "replay.json",
            )
        if minimize and diagnostic_depth == "deep":
            minimize_command = [
                *prefix,
                "minimize",
                str(witness),
                "--max-attempts",
                "32",
                "--report",
                str(artifacts / "minimization.json"),
            ]
            if mncs_binary:
                minimize_command.extend(("--mncs", mncs_binary))
            minimize_command.extend(("--timeout", str(timeout)))
            commands.append(
                ("debug-minimization", minimize_command, artifacts / "minimization.json")
            )
        return commands

    def _reusable_debug_queries(
        self, check: dict[str, Any] | None, cwd: Path
    ) -> dict[str, Path]:
        """Return integrity-checked query artifacts from an Actions handoff.

        Actions references are evidence, not instructions.  A query is reused
        only when its declared path is inside the Forge project and its
        recorded digest matches.  Missing references simply leave that query
        to the normal bounded provider invocation.
        """

        if check is None or not isinstance(check.get("references"), list):
            return {}
        labels = {
            "mncs-debug-capabilities": "debug-capabilities",
            "mncs-debug-validation": "debug-validation",
            "mncs-debug-open": "debug-open",
            "mncs-debug-inspection": "debug-inspection",
            "mncs-debug-trace": "debug-trace",
            "mncs-debug-provenance": "debug-provenance",
            "mncs-debug-replay": "debug-replay",
            "mncs-debug-minimization": "debug-minimization",
        }
        reusable: dict[str, Path] = {}
        for reference in check["references"]:
            if not isinstance(reference, dict):
                continue
            label = labels.get(reference.get("kind"))
            raw_path = reference.get("path")
            expected_digest = reference.get("digest") or reference.get("sha256")
            if label is None or not isinstance(raw_path, str) or not raw_path:
                continue
            candidate = Path(raw_path)
            if not candidate.is_absolute():
                candidate = cwd / candidate
            try:
                resolved = candidate.resolve(strict=True)
                resolved.relative_to(self.config.root.resolve())
            except (OSError, ValueError):
                continue
            if not isinstance(expected_digest, str) or _sha256(resolved) != expected_digest:
                continue
            reusable[label] = resolved
        return reusable

    def _selected_test(self, result: dict[str, Any], test_id: str | None) -> dict[str, Any]:
        tests = [item for item in result.get("tests", []) if isinstance(item, dict)]
        if test_id:
            selected = next((item for item in tests if item.get("id") == test_id), None)
        else:
            selected = next((item for item in tests if item.get("verdict") == "FAIL"), None)
        if selected is None:
            raise ForgeError(
                "PROVIDER_CONTRACT_INVALID",
                "the failing test result has no selectable failing TestExecution",
            )
        if selected.get("verdict") != "FAIL":
            raise ForgeError(
                "PROVIDER_CONTRACT_INVALID",
                "debug loop selection must reference a failing TestExecution",
            )
        return selected

    @staticmethod
    def _validate_native_observation(witness: dict[str, Any]) -> dict[str, Any] | None:
        """Validate the native facts Forge is about to project.

        `mncs-debug` is the semantic authority for these documents.  Forge
        still validates the membrane shape and identity linkage before using
        the facts in a diagnosis, but it never recomputes their identities or
        infers missing execution relationships.
        """

        runtime = _mapping(witness.get("runtime"))
        observation_value = runtime.get("observation")
        if observation_value is None:
            return None
        if not isinstance(observation_value, dict):
            raise ForgeError(
                "PROVIDER_CONTRACT_INVALID",
                "mncs-debug emitted a malformed native execution observation",
            )
        observation: dict[str, Any] = observation_value
        if observation.get("schema_version") != OBSERVATION_SCHEMA:
            raise ForgeError(
                "PROVIDER_CONTRACT_INVALID",
                "mncs-debug emitted a malformed native execution observation",
            )
        required = (
            "identity",
            "execution_identity",
            "policy",
            "completeness",
            "events",
            "values",
            "frames",
            "effects",
        )
        missing = [field for field in required if field not in observation]
        if missing or not all(
            isinstance(observation[field], list)
            for field in ("events", "values", "frames", "effects")
        ):
            raise ForgeError(
                "PROVIDER_CONTRACT_INVALID",
                f"native execution observation is missing or invalid: {missing}",
            )
        execution_identity = witness.get("execution_identity")
        if observation.get("execution_identity") != execution_identity:
            raise ForgeError(
                "PROVIDER_CONTRACT_INVALID",
                "native observation execution identity does not match the debug witness",
            )
        if (
            not isinstance(runtime.get("observation_identity"), str)
            or not runtime["observation_identity"]
        ):
            raise ForgeError(
                "PROVIDER_CONTRACT_INVALID",
                "native observation transport identity is missing",
            )
        source = _mapping(_mapping(witness.get("static")).get("source"))
        source_map = source.get("source_map")
        if source_map is not None and (
            not isinstance(source_map, dict)
            or source_map.get("schema_version") != SOURCE_MAP_SCHEMA
        ):
            raise ForgeError(
                "PROVIDER_CONTRACT_INVALID",
                "mncs-debug emitted a malformed compiler execution source map",
            )
        return observation

    @staticmethod
    def _native_diagnosis(
        witness: dict[str, Any],
        inspection: dict[str, Any],
        trace: dict[str, Any],
        provenance: dict[str, Any],
        selected: dict[str, Any],
        *,
        status: str,
    ) -> dict[str, Any]:
        """Project provider facts into a bounded Forge diagnosis.

        This is deliberately a projection, not a second debugger.  Every
        relationship below is copied from `mncs-debug` inspection/provenance
        or its structured trace.  Forge does not rebuild dataflow from source
        text, operation order, or human process output.
        """

        runtime = _mapping(witness.get("runtime"))
        observation = _mapping(runtime.get("observation"))
        native = observation.get("schema_version") == OBSERVATION_SCHEMA
        events = _object_list(trace.get("events"))
        outcome = _mapping(witness.get("outcome"))
        failure = _mapping(outcome.get("failure"))
        failure_operation = (
            failure.get("identity") if isinstance(failure.get("identity"), str) else None
        )
        matching_events: list[dict[str, Any]] = []

        def operation_of(event: dict[str, Any]) -> str | None:
            location = _mapping(event.get("location"))
            runtime_location = _mapping(location.get("runtime"))
            operation = runtime_location.get("operation")
            if isinstance(operation, str):
                return operation
            payload = _mapping(event.get("payload"))
            operation = payload.get("operation_identity")
            return operation if isinstance(operation, str) else None

        for event in events:
            payload = _mapping(event.get("payload"))
            event_failure = payload.get("failure_identity")
            event_operation = operation_of(event)
            if failure_operation is None and event.get("kind") == "failure" and event_operation:
                failure_operation = event_operation
            if failure_operation and (
                event_operation == failure_operation or event_failure == failure_operation
            ):
                matching_events.append(event)

        event_ids = [
            item.get("event_id")
            for item in matching_events
            if isinstance(item.get("event_id"), str)
        ]
        frame_ids: list[str] = []
        value_ids: list[str] = []
        effect_ids: list[str] = []
        for event in matching_events:
            location = _mapping(event.get("location"))
            runtime_location = _mapping(location.get("runtime"))
            frame = runtime_location.get("frame")
            if isinstance(frame, str) and frame not in frame_ids:
                frame_ids.append(frame)
            relationships = _mapping(event.get("relationships"))
            for key in ("value_inputs", "value_outputs"):
                relationship_values = relationships.get(key)
                values_for_event = (
                    relationship_values if isinstance(relationship_values, list) else []
                )
                for value in values_for_event:
                    if isinstance(value, str) and value not in value_ids:
                        value_ids.append(value)
            effect = relationships.get("effect")
            if isinstance(effect, str) and effect not in effect_ids:
                effect_ids.append(effect)

        raw_frames = _object_list(inspection.get("frames"))
        frames = [
            frame for frame in raw_frames if not frame_ids or frame.get("frame_id") in frame_ids
        ]
        raw_values = _object_list(inspection.get("values"))
        values = [
            value
            for value in raw_values
            if not value_ids
            or value.get("value_id") in value_ids
            or value.get("identity") in value_ids
        ]
        raw_effects = _object_list(inspection.get("effects"))
        effects = [
            effect
            for effect in raw_effects
            if not effect_ids or effect.get("identity") in effect_ids
        ]
        source_correspondence = None
        for event in matching_events:
            location = _mapping(event.get("location"))
            source = location.get("source")
            if isinstance(source, dict):
                source_correspondence = source
                break

        claims = _object_list(provenance.get("claims"))
        completeness = _mapping(_mapping(inspection.get("trace")).get("completeness"))
        observation_completeness = _mapping(observation.get("completeness")) if native else {}
        complete = observation_completeness.get("status") == "complete"
        if native and complete:
            confidence = "native_runtime_observation"
        elif native:
            confidence = "partial_native_observation"
        else:
            confidence = "bounded_provider_evidence"
        if failure_operation and native:
            statement = (
                f"mncs-debug observed runtime failure at {failure_operation} with "
                "native operation, frame, and value references"
            )
        elif native:
            statement = (
                "mncs-debug preserved bounded structured failure evidence; no "
                "native runtime failure operation was emitted"
            )
        else:
            statement = (
                "mncs-debug correlated the failing TestExecution with bounded provider evidence"
            )

        limitations = witness.get("limitations")
        if not isinstance(limitations, list):
            limitations = []
        diagnosis: dict[str, Any] = {
            "status": status,
            "confidence": confidence,
            "statement": statement,
            "test_failure": selected.get("failure"),
            "source_location": selected.get("location"),
            "evidence_basis": {
                "authority": "mncs-debug",
                "witness_id": witness.get("witness_id"),
                "execution_identity": witness.get("execution_identity"),
                "observation_identity": runtime.get("observation_identity") if native else None,
                "trace_id": trace.get("trace_id"),
                "inspection_id": inspection.get("inspection_id"),
                "provenance_id": provenance.get("provenance_id"),
            },
            "failure_operation": {
                "identity": failure_operation,
                "event_ids": event_ids,
                "source_correspondence": source_correspondence,
                "frame_ids": frame_ids,
                "input_value_ids": value_ids,
                "effect_ids": effect_ids,
                "status": "observed" if failure_operation and matching_events else "not_observed",
            },
            "frames": frames[:32],
            "consumed_values": values[:64],
            "effects": effects[:32],
            "provenance_claims": claims[:64],
            "completeness": {
                "observation": observation_completeness,
                "debugger": completeness,
                "status": "complete" if complete else "partial" if native else "provider_bounded",
                "limitations": limitations,
            },
        }
        return diagnosis

    def _source_change(self, path: str, old: str, new: str) -> tuple[Path, str, str]:
        resolved = self._path(path, must_exist=True)
        relative = PurePosixPath(self._relative(resolved))
        candidate_scopes = self.config.relative_scopes("candidates")
        generated_scopes = self.config.relative_scopes("generated")
        if not is_within(relative, [*candidate_scopes, *generated_scopes]):
            raise ForgeError(
                "AUTHORITY_FORBIDDEN",
                "repair path is outside configured candidate/generated scopes",
            )
        authority = self.config.raw.get("authority", {}).get("development", {})
        if not authority.get("may_write_candidates", False) and is_within(
            relative, candidate_scopes
        ):
            raise ForgeError(
                "AUTHORITY_FORBIDDEN", "development authority cannot write candidate paths"
            )
        if not authority.get("may_write_generated", False) and is_within(
            relative, generated_scopes
        ):
            raise ForgeError(
                "AUTHORITY_FORBIDDEN", "development authority cannot write generated paths"
            )
        if not isinstance(old, str) or not isinstance(new, str) or not old:
            raise ForgeError(
                "REPAIR_INVALID", "repair requires a non-empty exact source replacement"
            )
        try:
            text = resolved.read_text(encoding="utf-8")
        except OSError as exc:
            raise ForgeError("REPAIR_INVALID", f"repair source cannot be read: {exc}") from exc
        if text.count(old) != 1:
            raise ForgeError("REPAIR_INVALID", "repair replacement must match exactly once")
        return resolved, text, text.replace(old, new, 1)

    def _regenerate_verification_plan(
        self,
        *,
        plan: dict[str, Any],
        ravel_command: list[str] | None,
        mncs_binary: str | None,
        library_paths: list[str] | None,
        output_path: Path,
        cwd: Path,
        timeout: float,
    ) -> tuple[dict[str, Any], dict[str, object]]:
        """Ask RAVEL to rebind impact after a source mutation.

        Forge only assembles the declared argv and validates the returned
        plan. It never copies the compiler's graph traversal or test policy.
        """

        configured = ravel_command
        if configured is None:
            configured = self.config.public_commands().get("ravel_impact")
        if not isinstance(configured, list) or not configured:
            raise ForgeError(
                "MNCS_PROVIDER_UNAVAILABLE",
                "a post-repair verification plan is required; declare ravel_impact to regenerate it",
            )
        if not mncs_binary:
            raise ForgeError(
                "MNCS_PROVIDER_INPUT",
                "mncs_binary is required to regenerate a RAVEL verification plan",
            )
        impact = _mapping(plan.get("impact"))
        roots = impact.get("roots")
        if not isinstance(roots, list) or not all(isinstance(root, str) and root for root in roots):
            raise ForgeError("PROVIDER_CONTRACT_INVALID", "verification plan has no compiler roots to rebind")
        source_path = self._plan_source(plan)
        command = [*configured, str(source_path), "--mncs", mncs_binary]
        for root in roots:
            command.extend(("--root", root))
        for library in library_paths or []:
            command.extend(("--library", library))
        provenance = _mapping(plan.get("provenance"))
        change_class = provenance.get("change_class")
        if isinstance(change_class, str) and change_class:
            command.extend(("--change-class", change_class))
        if provenance.get("cross_repository") is True:
            command.append("--cross-repository")
        command.extend(("--cwd", str(cwd), "--output", str(output_path)))
        execution = self._run(command, cwd, label="ravel-impact", timeout=timeout)
        if not output_path.is_file():
            raise ForgeError(
                "PROVIDER_CONTRACT_INVALID",
                "ravel-impact completed without producing a verification plan",
            )
        rebound = self._read(output_path, label="post-repair verification plan")
        self._validate_verification_plan(rebound)
        return rebound, execution

    def failure_loop(
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
        repair_path: str | None = None,
        repair_from: str | None = None,
        repair_to: str | None = None,
        verification_plan_file: str | None = None,
        post_repair_verification_plan_file: str | None = None,
        ravel_command: list[str] | None = None,
        diagnostic_depth: str = "minimal",
        output_file: str | None = None,
    ) -> dict[str, object]:
        if (
            self.config.raw.get("authority", {}).get("development", {}).get("may_run_providers")
            is not True
        ):
            raise ForgeError(
                "AUTHORITY_FORBIDDEN", "development authority does not permit provider execution"
            )
        if provider_mode not in {"invoke", "consume"}:
            raise ForgeError("MNCS_PROVIDER_INPUT", "provider_mode must be invoke or consume")
        if diagnostic_depth not in {"minimal", "standard", "deep"}:
            raise ForgeError(
                "MNCS_DEBUG_INPUT",
                "diagnostic_depth must be minimal, standard, or deep",
            )
        if capture_policy not in {"failure-only", "selected", "bounded", "diagnostic", "events"}:
            raise ForgeError(
                "MNCS_DEBUG_INPUT",
                "capture_policy must be failure-only, selected, bounded, diagnostic, or events",
            )
        if max_events < 1 or max_events > 512:
            raise ForgeError("MNCS_DEBUG_INPUT", "max_events must be between 1 and 512")
        if max_values < 0 or max_values > 2048:
            raise ForgeError("MNCS_DEBUG_INPUT", "max_values must be between 0 and 2048")
        if max_value_bytes < 0 or max_value_bytes > 65536:
            raise ForgeError("MNCS_DEBUG_INPUT", "max_value_bytes must be between 0 and 65536")
        if capture_policy == "selected" and not selected_operations:
            raise ForgeError(
                "MNCS_DEBUG_INPUT", "selected capture requires at least one operation identity"
            )
        timeout = float(timeout_seconds if timeout_seconds is not None else self.config.timeout)
        if timeout <= 0:
            raise ForgeError("MNCS_DEBUG_INPUT", "timeout_seconds must be positive")
        manifest_path = self._path(manifest, must_exist=True)
        cwd = self._path(working_directory, must_exist=True)
        result_path = self._path(test_result_file)
        check_path = self._path(test_check_file)
        test_artifacts = self._path(test_artifacts_directory)
        witness_path = self._path(debug_witness_file)
        debug_artifacts = self._path(debug_artifacts_directory)
        debug_check_path = self._path(debug_check_file) if debug_check_file is not None else None
        verification_plan_path = (
            self._path(verification_plan_file, must_exist=True)
            if verification_plan_file is not None
            else None
        )
        post_verification_plan_path = (
            self._path(post_repair_verification_plan_file, must_exist=True)
            if post_repair_verification_plan_file is not None
            else None
        )
        action_refs = [
            self._ref(self._path(path, must_exist=True), "mncs-actions-artifact")
            for path in (actions_evidence_files or [])
        ]
        result_path.parent.mkdir(parents=True, exist_ok=True)
        check_path.parent.mkdir(parents=True, exist_ok=True)
        test_artifacts.mkdir(parents=True, exist_ok=True)
        witness_path.parent.mkdir(parents=True, exist_ok=True)
        debug_artifacts.mkdir(parents=True, exist_ok=True)

        verification_plan: dict[str, Any] | None = None
        verification_plan_ref: dict[str, object] | None = None
        plan_source_path: Path | None = None
        plan_projection: dict[str, Any] | None = None
        if verification_plan_path is not None:
            verification_plan = self._read(
                verification_plan_path,
                label="verification plan",
                byte_cap=262144,
            )
            self._validate_verification_plan(verification_plan)
            plan_source_path = self._plan_source(verification_plan)
            declared_source_sha = _mapping(verification_plan.get("source")).get("sha256")
            if declared_source_sha != _sha256(plan_source_path):
                raise ForgeError(
                    "PROVIDER_CONTRACT_INVALID",
                    "verification plan is stale: source digest does not match the current source",
                )
            verification_plan_ref = self._ref(
                verification_plan_path,
                "mncs-verification-plan",
                VERIFICATION_PLAN_SCHEMA,
            )
            verification_plan_ref["plan_id"] = verification_plan["plan_id"]
            plan_projection = self._plan_projection(verification_plan, verification_plan_ref)

        post_verification_plan_template: dict[str, Any] | None = None
        if post_verification_plan_path is not None:
            post_verification_plan_template = self._read(
                post_verification_plan_path,
                label="post-repair verification plan",
                byte_cap=262144,
            )
            self._validate_verification_plan(post_verification_plan_template)

        effective_diagnostic_depth = "deep" if minimize else diagnostic_depth
        if repair_path is not None and verification_plan_path is not None:
            if post_verification_plan_path is None and not (
                isinstance(ravel_command, list) and ravel_command
            ) and not self.config.public_commands().get("ravel_impact"):
                raise ForgeError(
                    "REPAIR_INVALID",
                    "repair with a verification plan requires a post-repair plan or declared ravel_impact command",
                )

        test_prefix = self._command_prefix(test_command, "mncs_test")
        if provider_mode == "invoke":
            test_argv = self._test_command(
                test_prefix,
                manifest=manifest_path,
                result=result_path,
                check=check_path,
                artifacts=test_artifacts,
                mncs_binary=mncs_binary,
                library_paths=library_paths,
                embed_library=embed_library,
                verification_plan=verification_plan_path,
            )
            before_execution = self._run(test_argv, cwd, label="mncs-test", timeout=timeout)
        else:
            if not result_path.is_file() or not check_path.is_file():
                raise ForgeError(
                    "PROVIDER_CONTRACT_INVALID",
                    "consume mode requires existing mncs-test result and check artifacts",
                )
            before_execution = {
                "label": "mncs-actions-provider-handoff",
                "source": "mncs-actions",
                "status": "consumed",
                "result": self._ref(result_path, "mncs-test-result", TEST_RESULT_SCHEMA),
                "check": self._ref(check_path, "mncs-test-check", CHECK_RESULT_SCHEMA),
                "references": action_refs,
            }
        test_result = self._read(result_path, label="mncs-test result")
        test_check = self._read(check_path, label="mncs-test check")
        self._validate_test_result(test_result)
        self._validate_check(test_check, "mncs-test")
        if verification_plan is not None:
            observed_source = _mapping(_mapping(test_result.get("provenance")).get("source")).get(
                "path"
            )
            if isinstance(observed_source, str) and plan_source_path is not None:
                try:
                    if Path(observed_source).resolve() != plan_source_path:
                        raise ForgeError(
                            "PROVIDER_CONTRACT_INVALID",
                            "mncs-test source provenance does not match the RAVEL plan source",
                        )
                except OSError as exc:
                    raise ForgeError(
                        "PROVIDER_CONTRACT_INVALID",
                        "mncs-test source provenance could not be resolved",
                    ) from exc
            result_selection = _mapping(test_result.get("selection"))
            planned_values = _mapping(verification_plan.get("selection")).get(
                "selected_test_identities", []
            )
            observed_values = result_selection.get("selected_test_identities", [])
            if not isinstance(planned_values, list) or not isinstance(observed_values, list):
                raise ForgeError(
                    "PROVIDER_CONTRACT_INVALID",
                    "mncs-test selection identities are not lists",
                )
            planned_ids = sorted(set(planned_values))
            observed_ids = sorted(set(observed_values))
            if planned_ids != observed_ids:
                raise ForgeError(
                    "PROVIDER_CONTRACT_INVALID",
                    "mncs-test selection does not match the RAVEL verification plan",
                )
        selected = (
            self._selected_test(test_result, test_id) if test_result["verdict"] == "FAIL" else None
        )

        base: dict[str, Any] = {
            "schema_version": "mncs.forge-mncs-development/1",
            "operation": "development.mncs.failure-loop",
            "test": {
                "result": self._ref(result_path, "mncs-test-result", TEST_RESULT_SCHEMA),
                "check": self._ref(check_path, "mncs-test-check", CHECK_RESULT_SCHEMA),
                "run_id": test_result.get("run_id"),
                "verdict": test_result.get("verdict"),
                "classification": test_result.get("classification"),
                "execution": before_execution,
            },
            "impact": {
                "status": "CONSUMED" if verification_plan is not None else "NOT_REQUESTED",
                "plan": plan_projection,
                "execution": None,
            },
            "debug": {"status": "NOT_REQUESTED", "execution": None, "references": []},
            "diagnosis": {
                "status": "NOT_REQUESTED",
                "confidence": "none",
                "statement": (
                    "mncs-test did not establish a failing test; no debugger invocation "
                    "was required"
                ),
            },
            "repair": {"status": "NOT_REQUESTED"},
            "verification": {"status": "NOT_REQUESTED"},
            "provenance": {
                "project_identity": self.config.project_identity,
                "manifest": self._ref(manifest_path, "mncs-test-manifest"),
                "working_directory": self._relative(cwd),
                "capture_policy": capture_policy,
                "max_events": max_events,
                "max_values": max_values,
                "max_value_bytes": max_value_bytes,
                "selected_operations": selected_operations or [],
                "provider_mode": provider_mode,
                "provider_handoff": {
                    "provider": "mncs-actions" if provider_mode == "consume" else None,
                    "debug_check": (
                        self._ref(debug_check_path, "mncs-debug-check", CHECK_RESULT_SCHEMA)
                        if debug_check_path is not None and debug_check_path.is_file()
                        else None
                    ),
                    "references": action_refs,
                },
                "verification_plan": plan_projection,
            },
            "observability": {
                "verification_scope": plan_projection,
                "selected_test_count": (
                    plan_projection.get("selected_test_count", 0)
                    if plan_projection is not None
                    else len(test_result.get("tests", []))
                ),
                "available_test_count": (
                    plan_projection.get("available_test_count")
                    if plan_projection is not None
                    else len(test_result.get("tests", []))
                ),
                "affected_surface_size": (
                    plan_projection.get("affected_surface_size", 0)
                    if plan_projection is not None
                    else None
                ),
                "diagnostic_depth_requested": diagnostic_depth,
                "diagnostic_depth_reached": None,
                "escalations": [],
                "reused_evidence": {
                    "actions_references": len(action_refs),
                    "verification_plan": verification_plan is not None,
                    "debug_queries": 0,
                },
            },
        }
        if plan_projection is not None:
            plan_level = plan_projection.get("level")
            plan_reasons = plan_projection.get("escalation_reasons", [])
            if plan_level != "changed_item" and isinstance(plan_reasons, list):
                base["observability"]["escalations"].append(
                    {
                        "kind": "verification_scope",
                        "from": "changed_item",
                        "to": plan_level,
                        "reasons": plan_reasons,
                    }
                )
        if effective_diagnostic_depth != "minimal":
            base["observability"]["escalations"].append(
                {
                    "kind": "diagnostic_depth",
                    "from": "minimal",
                    "to": effective_diagnostic_depth,
                    "reason": "insufficient_diagnostic_evidence",
                }
            )
        if test_result["verdict"] != "FAIL":
            if verification_plan is not None and not _mapping(verification_plan.get("proof")).get(
                "sufficient_to_stop", False
            ):
                base["verdict"] = "UNKNOWN"
                base["verification"] = {
                    "status": "NOT_ESTABLISHED",
                    "reason": "selected local result is not the required family proof boundary",
                }
            else:
                base["verdict"] = test_result["verdict"]
            return self._persist(base, output_file)

        if selected is None:  # pragma: no cover - guarded by _selected_test
            raise ForgeError("PROVIDER_CONTRACT_INVALID", "no failing test selected")
        debug_prefix = debug_command
        if debug_prefix is None:
            debug_prefix = self.config.public_commands().get("mncs_debug")
        if not isinstance(debug_prefix, list) or not debug_prefix:
            base["verdict"] = "FAIL"
            base["debug"] = {
                "status": "UNKNOWN",
                "reason": "mncs-debug command is not declared",
                "test_verdict": "FAIL",
                "references": [],
            }
            base["diagnosis"] = {
                "status": "UNKNOWN",
                "confidence": "none",
                "statement": "test FAIL is established; debug evidence is unavailable",
            }
            return self._persist(base, output_file)

        handoff_debug_check: dict[str, Any] | None = None
        if provider_mode == "consume":
            if debug_check_path is None or not debug_check_path.is_file():
                base["verdict"] = "FAIL"
                base["debug"] = {
                    "status": "UNKNOWN",
                    "reason": "mncs-actions did not provide a debug check artifact",
                    "test_verdict": "FAIL",
                    "references": [],
                }
                base["diagnosis"] = {
                    "status": "UNKNOWN",
                    "confidence": "none",
                    "statement": "test FAIL is established; the Actions debug claim is unavailable",
                }
                return self._persist(base, output_file)
            try:
                handoff_debug_check = self._read(
                    debug_check_path, label="mncs-debug action check"
                )
                self._validate_check(handoff_debug_check, "mncs-debug")
            except ForgeError:
                base["verdict"] = "FAIL"
                base["debug"] = {
                    "status": "UNKNOWN",
                    "reason": "mncs-actions emitted a malformed debug check artifact",
                    "test_verdict": "FAIL",
                    "references": [
                        self._ref(debug_check_path, "mncs-debug-check", CHECK_RESULT_SCHEMA)
                    ],
                }
                base["diagnosis"] = {
                    "status": "UNKNOWN",
                    "confidence": "none",
                    "statement": (
                        "test FAIL is established; malformed Actions debug evidence remains INVALID"
                    ),
                }
                return self._persist(base, output_file)
            if handoff_debug_check.get("verdict") != "PASS":
                base["verdict"] = "FAIL"
                base["debug"] = {
                    "status": "UNKNOWN",
                    "reason": "mncs-actions did not establish debug evidence",
                    "test_verdict": "FAIL",
                    "transport_check": self._ref(
                        debug_check_path, "mncs-debug-check", CHECK_RESULT_SCHEMA
                    ),
                    "references": [],
                }
                base["diagnosis"] = {
                    "status": "UNKNOWN",
                    "confidence": "none",
                    "statement": "test FAIL is established; Actions debug evidence is UNKNOWN",
                }
                return self._persist(base, output_file)

            if not witness_path.is_file():
                base["verdict"] = "FAIL"
                base["debug"] = {
                    "status": "UNKNOWN",
                    "reason": "mncs-actions did not provide a debug witness",
                    "test_verdict": "FAIL",
                    "transport_check": self._ref(
                        debug_check_path, "mncs-debug-check", CHECK_RESULT_SCHEMA
                    ),
                    "references": [],
                }
                base["diagnosis"] = {
                    "status": "UNKNOWN",
                    "confidence": "none",
                    "statement": (
                        "test FAIL is established; the Actions debug witness is unavailable"
                    ),
                }
                return self._persist(base, output_file)

        selected_id = str(selected["id"])
        debug_references: list[dict[str, object]] = []
        debug_executions: list[dict[str, object]] = []
        debug_query_paths: list[tuple[str, Path, str]] = []
        reusable_debug_queries = self._reusable_debug_queries(handoff_debug_check, cwd)
        for label, command, path in self._debug_commands(
            list(debug_prefix),
            test_result=result_path,
            test_id=selected_id,
            witness=witness_path,
            artifacts=debug_artifacts,
            cwd=cwd,
            mncs_binary=mncs_binary,
            library_paths=library_paths,
            capture_policy=capture_policy,
            max_events=max_events,
            max_values=max_values,
            max_value_bytes=max_value_bytes,
            selected_operations=selected_operations,
            timeout=timeout,
            minimize=minimize,
            diagnostic_depth=effective_diagnostic_depth,
            include_import=provider_mode == "invoke",
        ):
            reused_path = reusable_debug_queries.get(label) if provider_mode == "consume" else None
            if reused_path is not None:
                path = reused_path
                debug_executions.append(
                    {
                        "label": label,
                        "status": "reused",
                        "reference": self._ref(path, "mncs-actions-debug-artifact"),
                    }
                )
                base["observability"]["reused_evidence"]["debug_queries"] = (
                    base["observability"]["reused_evidence"].get("debug_queries", 0) + 1
                )
            else:
                execution = self._run(command, cwd, label=label, timeout=timeout)
                debug_executions.append(execution)
            if path.is_file():
                debug_query_paths.append((label, path, ""))

        if not witness_path.is_file():
            base["verdict"] = "FAIL"
            base["debug"] = {
                "status": "UNKNOWN",
                "reason": "mncs-debug did not produce a witness",
                "execution": debug_executions,
                "references": [],
            }
            base["diagnosis"] = {
                "status": "UNKNOWN",
                "confidence": "none",
                "statement": "test FAIL is established; debugger output was unavailable",
            }
            return self._persist(base, output_file)
        # Static operation/source indexes are intentionally bounded by the
        # debugger's capture policy, but can be larger than Forge's normal
        # provider-result cap.  Keep a separate hard membrane for this
        # structured artifact instead of truncating it or parsing stdout.
        witness = self._read(witness_path, label="mncs-debug witness", byte_cap=4_000_000)
        query_path_by_label = {label: path for label, path, _schema in debug_query_paths}
        validation_path = query_path_by_label.get(
            "debug-validation", debug_artifacts / "validation.json"
        )
        validation = (
            self._read(validation_path, label="mncs-debug validation")
            if validation_path.is_file()
            else {}
        )
        if (
            validation.get("schema_version") != VALIDATION_SCHEMA
            or validation.get("valid") is not True
            or witness.get("schema_version") != WITNESS_SCHEMA
        ):
            base["verdict"] = "FAIL"
            base["debug"] = {
                "status": "UNKNOWN",
                "reason": "mncs-debug witness validation did not establish a valid witness",
                "execution": debug_executions,
                "references": [self._ref(witness_path, "mncs-debug-witness", WITNESS_SCHEMA)],
            }
            base["diagnosis"] = {
                "status": "UNKNOWN",
                "confidence": "none",
                "statement": "test FAIL is established; malformed debug evidence remains INVALID",
            }
            return self._persist(base, output_file)

        observation = self._validate_native_observation(witness)

        integration = _mapping(witness.get("integration"))
        if (
            integration.get("run_id") != test_result.get("run_id")
            or integration.get("test_id") != selected_id
        ):
            base["verdict"] = "FAIL"
            base["debug"] = {
                "status": "UNKNOWN",
                "reason": "debug witness identity does not match the selected test execution",
                "execution": debug_executions,
                "references": [self._ref(witness_path, "mncs-debug-witness", WITNESS_SCHEMA)],
            }
            base["diagnosis"] = {
                "status": "UNKNOWN",
                "confidence": "none",
                "statement": "test FAIL is established; debug reproduction identity mismatched",
            }
            return self._persist(base, output_file)

        schema_by_label = {
            "debug-capabilities": CAPABILITIES_SCHEMA,
            "debug-validation": VALIDATION_SCHEMA,
            "debug-open": SESSION_SCHEMA,
            "debug-inspection": INSPECTION_SCHEMA,
            "debug-trace": TRACE_SCHEMA,
            "debug-provenance": PROVENANCE_SCHEMA,
            "debug-replay": REPLAY_SCHEMA,
            "debug-minimization": MINIMIZATION_SCHEMA,
        }
        query_documents: dict[str, dict[str, Any]] = {}
        for label, path, _ in debug_query_paths:
            if label == "debug-import":
                continue
            document = self._read(path, label=label, byte_cap=4_000_000)
            query_documents[label] = document
            expected = schema_by_label[label]
            if document.get("schema_version") != expected:
                raise ForgeError("PROVIDER_CONTRACT_INVALID", f"{label} did not emit {expected}")
            debug_references.append(
                self._ref(path, f"mncs-debug-{label.removeprefix('debug-')}", expected)
            )
        debug_references.insert(0, self._ref(witness_path, "mncs-debug-witness", WITNESS_SCHEMA))
        expected_queries = 1 + sum(
            1 for label, _command, _path in self._debug_commands(
                list(debug_prefix),
                test_result=result_path,
                test_id=selected_id,
                witness=witness_path,
                artifacts=debug_artifacts,
                cwd=cwd,
                mncs_binary=mncs_binary,
                library_paths=library_paths,
                capture_policy=capture_policy,
                max_events=max_events,
                max_values=max_values,
                max_value_bytes=max_value_bytes,
                selected_operations=selected_operations,
                timeout=timeout,
                minimize=minimize,
                diagnostic_depth=effective_diagnostic_depth,
                include_import=False,
            )
            if label != "debug-import"
        )
        debug_status = "ESTABLISHED" if len(debug_references) == expected_queries else "UNKNOWN"
        base["debug"] = {
            "status": debug_status,
            "witness_id": witness.get("witness_id"),
            "execution_identity": witness.get("execution_identity"),
            "test_execution": integration.get("test_execution"),
            "outcome": witness.get("outcome"),
            "observation": {
                "schema_revision": observation.get("schema_version") if observation else None,
                "identity": _mapping(witness.get("runtime")).get("observation_identity")
                if observation
                else None,
                "completeness": observation.get("completeness") if observation else None,
            },
            "source_map": {
                "schema_revision": _mapping(
                    _mapping(_mapping(witness.get("static")).get("source"))
                ).get("source_map_schema"),
                "identity": _mapping(_mapping(_mapping(witness.get("static")).get("source"))).get(
                    "source_map_identity"
                ),
            },
            "execution": debug_executions,
            "references": debug_references,
            "diagnostic_depth": effective_diagnostic_depth,
            "diagnostic_operations": [
                label.removeprefix("debug-")
                for label, _path, _schema in debug_query_paths
                if label != "debug-import"
            ],
        }
        base["observability"]["diagnostic_depth_reached"] = (
            effective_diagnostic_depth if debug_status == "ESTABLISHED" else "insufficient"
        )
        base["diagnosis"] = self._native_diagnosis(
            witness,
            query_documents.get("debug-inspection", {}),
            query_documents.get("debug-trace", {}),
            query_documents.get("debug-provenance", {}),
            selected,
            status=debug_status,
        )
        if debug_status != "ESTABLISHED":
            base["verdict"] = "FAIL"
            return self._persist(base, output_file)

        repair_requested = any(value is not None for value in (repair_path, repair_from, repair_to))
        if not repair_requested:
            base["verdict"] = "FAIL"
            base["repair"] = {"status": "CANDIDATE_NOT_REQUESTED"}
            return self._persist(base, output_file)
        if repair_path is None or repair_from is None or repair_to is None:
            raise ForgeError(
                "REPAIR_INVALID",
                "repair_path, repair_from, and repair_to must be supplied together",
            )
        source_path, _before_text, after_text = self._source_change(
            repair_path, repair_from, repair_to
        )
        before_source_sha = _sha256(source_path)
        after_plan: dict[str, Any] | None = None
        after_plan_ref: dict[str, object] | None = None
        after_plan_execution: dict[str, object] | None = None
        after_plan_path: Path | None = None
        if verification_plan is not None and post_verification_plan_template is not None:
            expected_after_sha = hashlib.sha256(after_text.encode("utf-8")).hexdigest()
            declared_after_sha = _mapping(post_verification_plan_template.get("source")).get("sha256")
            if declared_after_sha != expected_after_sha:
                raise ForgeError(
                    "REPAIR_INVALID",
                    "post-repair verification plan is not bound to the proposed source result",
                )
        source_path.write_text(after_text, encoding="utf-8")
        after_source_sha = _sha256(source_path)
        if verification_plan is not None:
            if post_verification_plan_template is not None and post_verification_plan_path is not None:
                after_plan = post_verification_plan_template
                after_plan_path = post_verification_plan_path
                after_plan_ref = self._ref(
                    post_verification_plan_path,
                    "mncs-verification-plan",
                    VERIFICATION_PLAN_SCHEMA,
                )
                after_plan_ref["plan_id"] = after_plan["plan_id"]
            else:
                generated_after_plan_path = verification_plan_path.with_name(
                    verification_plan_path.stem + ".after" + verification_plan_path.suffix
                )
                after_plan_path = generated_after_plan_path
                after_plan, after_plan_execution = self._regenerate_verification_plan(
                    plan=verification_plan,
                    ravel_command=ravel_command,
                    mncs_binary=mncs_binary,
                    library_paths=library_paths,
                    output_path=generated_after_plan_path,
                    cwd=cwd,
                    timeout=timeout,
                )
                after_plan_ref = self._ref(
                    generated_after_plan_path,
                    "mncs-verification-plan",
                    VERIFICATION_PLAN_SCHEMA,
                )
                after_plan_ref["plan_id"] = after_plan["plan_id"]
            base["impact"]["after_plan"] = self._plan_projection(after_plan, after_plan_ref)
            base["impact"]["execution"] = after_plan_execution
            base["provenance"]["verification_plan_after"] = base["impact"]["after_plan"]
            base["observability"]["reused_evidence"]["post_repair_plan"] = (
                after_plan_execution is None
            )
        after_result = result_path.with_name(result_path.stem + ".after" + result_path.suffix)
        after_check = check_path.with_name(check_path.stem + ".after" + check_path.suffix)
        after_artifacts = test_artifacts.with_name(test_artifacts.name + ".after")
        after_argv = self._test_command(
            test_prefix,
            manifest=manifest_path,
            result=after_result,
            check=after_check,
            artifacts=after_artifacts,
            mncs_binary=mncs_binary,
            library_paths=library_paths,
            embed_library=embed_library,
            verification_plan=after_plan_path,
        )
        after_execution = self._run(
            after_argv, cwd, label="mncs-test-verification", timeout=timeout
        )
        after_test = self._read(after_result, label="mncs-test verification result")
        after_check_value = self._read(after_check, label="mncs-test verification check")
        self._validate_test_result(after_test)
        self._validate_check(after_check_value, "mncs-test")
        if after_plan is not None:
            after_selection = _mapping(after_test.get("selection"))
            after_planned_values = _mapping(after_plan.get("selection")).get(
                "selected_test_identities", []
            )
            after_observed_values = after_selection.get("selected_test_identities", [])
            if not isinstance(after_planned_values, list) or not isinstance(after_observed_values, list):
                raise ForgeError(
                    "PROVIDER_CONTRACT_INVALID",
                    "post-repair mncs-test selection identities are not lists",
                )
            after_planned_ids = sorted(set(after_planned_values))
            after_observed_ids = sorted(set(after_observed_values))
            if after_planned_ids != after_observed_ids:
                raise ForgeError(
                    "PROVIDER_CONTRACT_INVALID",
                    "post-repair mncs-test selection does not match the rebound RAVEL plan",
                )
        proof_sufficient = after_plan is None or _mapping(after_plan.get("proof")).get(
            "sufficient_to_stop", False
        )
        base["repair"] = {
            "status": "APPLIED",
            "path": self._relative(source_path),
            "replacement_digest": hashlib.sha256(
                canonical_bytes({"from": repair_from, "to": repair_to})
            ).hexdigest(),
            "source_before_sha256": before_source_sha,
            "source_after_sha256": after_source_sha,
        }
        base["verification"] = {
            "status": after_test.get("verdict") if proof_sufficient else "UNKNOWN",
            "result": self._ref(after_result, "mncs-test-result", TEST_RESULT_SCHEMA),
            "check": self._ref(after_check, "mncs-test-check", CHECK_RESULT_SCHEMA),
            "run_id": after_test.get("run_id"),
            "execution": after_execution,
            "selection": after_test.get("selection"),
            "proof_sufficient": proof_sufficient,
        }
        base["provenance"]["identity_continuity"] = {
            "before_test_run_id": test_result.get("run_id"),
            "before_test_id": selected_id,
            "before_test_execution_identity": selected.get("execution_identity"),
            "debug_witness_id": witness.get("witness_id"),
            "debug_execution_identity": witness.get("execution_identity"),
            "after_test_run_id": after_test.get("run_id"),
            "source_before_sha256": before_source_sha,
            "source_after_sha256": after_source_sha,
        }
        base["verdict"] = after_test.get("verdict") if proof_sufficient else "UNKNOWN"
        return self._persist(base, output_file)

    def _persist(self, result: dict[str, Any], output_file: str | None) -> dict[str, Any]:
        material = dict(result)
        material.pop("artifact", None)
        result["output_identity"] = local_json_identity(material)
        output_identity = result["output_identity"]
        if not isinstance(output_identity, str):  # pragma: no cover - local identity is a string
            raise ForgeError("FORGE_OUTPUT_INVALID", "Forge output identity is not a string")
        destination = (
            self._path(output_file)
            if output_file is not None
            else self.config.state_dir
            / "mncs-development"
            / f"{output_identity.split(':')[-1]}.json"
        )
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(pretty_json(result), encoding="utf-8")
        result["artifact"] = {
            "kind": "forge-mncs-development",
            "path": str(destination),
            "sha256": _sha256(destination),
        }
        return result
