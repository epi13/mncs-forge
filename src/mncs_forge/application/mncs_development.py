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
CAPABILITIES_SCHEMA = "mncs.debug-capabilities/1"
SESSION_SCHEMA = "mncs.debug-session/1"
WITNESS_SCHEMA = "mncs.debug-witness/1"
VALIDATION_SCHEMA = "mncs.debug-validation/1"
TRACE_SCHEMA = "mncs.debug-trace/1"
INSPECTION_SCHEMA = "mncs.debug-inspection/1"
PROVENANCE_SCHEMA = "mncs.debug-provenance/1"
REPLAY_SCHEMA = "mncs.debug-replay/1"
MINIMIZATION_SCHEMA = "mncs.debug-minimization/1"
VERDICTS = {"PASS", "FAIL", "UNKNOWN"}


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
        timeout: float,
        minimize: bool,
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
                "--timeout",
                str(timeout),
            )
        )
        for path in library_paths or []:
            common.extend(("--library", path))
        capabilities_command = [*prefix, "capabilities"]
        if mncs_binary:
            capabilities_command.extend(("--mncs", mncs_binary))
        capabilities_command.extend(("--output", str(artifacts / "capabilities.json")))
        commands = [("debug-capabilities", capabilities_command, artifacts / "capabilities.json")]
        if include_import:
            commands.append(
                (
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
            )
        commands.extend(
            [
                (
                    "debug-validation",
                    [
                        *prefix,
                        "validate",
                        str(witness),
                        "--output",
                        str(artifacts / "validation.json"),
                    ],
                    artifacts / "validation.json",
                ),
                (
                    "debug-open",
                    [*prefix, "open", str(witness), "--output", str(artifacts / "session.json")],
                    artifacts / "session.json",
                ),
                (
                    "debug-inspection",
                    [
                        *prefix,
                        "inspect",
                        str(witness),
                        "--output",
                        str(artifacts / "inspection.json"),
                    ],
                    artifacts / "inspection.json",
                ),
                (
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
                ),
                (
                    "debug-provenance",
                    [*prefix, "why", str(witness), "--output", str(artifacts / "provenance.json")],
                    artifacts / "provenance.json",
                ),
                (
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
                ),
            ]
        )
        if minimize:
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
        timeout_seconds: float | None = None,
        minimize: bool = False,
        test_id: str | None = None,
        provider_mode: str = "invoke",
        debug_check_file: str | None = None,
        actions_evidence_files: list[str] | None = None,
        repair_path: str | None = None,
        repair_from: str | None = None,
        repair_to: str | None = None,
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
        if capture_policy not in {"failure-only", "bounded", "events"}:
            raise ForgeError(
                "MNCS_DEBUG_INPUT", "capture_policy must be failure-only, bounded, or events"
            )
        if max_events < 1 or max_events > 512:
            raise ForgeError("MNCS_DEBUG_INPUT", "max_events must be between 1 and 512")
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
        action_refs = [
            self._ref(self._path(path, must_exist=True), "mncs-actions-artifact")
            for path in (actions_evidence_files or [])
        ]
        result_path.parent.mkdir(parents=True, exist_ok=True)
        check_path.parent.mkdir(parents=True, exist_ok=True)
        test_artifacts.mkdir(parents=True, exist_ok=True)
        witness_path.parent.mkdir(parents=True, exist_ok=True)
        debug_artifacts.mkdir(parents=True, exist_ok=True)

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
        selected = (
            self._selected_test(test_result, test_id) if test_result["verdict"] == "FAIL" else None
        )

        base: dict[str, object] = {
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
            "debug": {"status": "NOT_REQUESTED", "execution": None, "references": []},
            "diagnosis": {
                "status": "NOT_REQUESTED",
                "confidence": "none",
                "statement": "mncs-test did not establish a failing test; no debugger invocation was required",
            },
            "repair": {"status": "NOT_REQUESTED"},
            "verification": {"status": "NOT_REQUESTED"},
            "provenance": {
                "project_identity": self.config.project_identity,
                "manifest": self._ref(manifest_path, "mncs-test-manifest"),
                "working_directory": self._relative(cwd),
                "capture_policy": capture_policy,
                "max_events": max_events,
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
            },
        }
        if test_result["verdict"] != "FAIL":
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
                debug_check = self._read(debug_check_path, label="mncs-debug action check")
                self._validate_check(debug_check, "mncs-debug")
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
                    "statement": "test FAIL is established; malformed Actions debug evidence remains INVALID",
                }
                return self._persist(base, output_file)
            if debug_check.get("verdict") != "PASS":
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
                    "statement": "test FAIL is established; the Actions debug witness is unavailable",
                }
                return self._persist(base, output_file)

        selected_id = str(selected["id"])
        debug_references: list[dict[str, object]] = []
        debug_executions: list[dict[str, object]] = []
        debug_query_paths: list[tuple[str, Path, str]] = []
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
            timeout=timeout,
            minimize=minimize,
            include_import=provider_mode == "invoke",
        ):
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
        validation_path = debug_artifacts / "validation.json"
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

        integration = (
            witness.get("integration") if isinstance(witness.get("integration"), dict) else {}
        )
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
        for label, path, _ in debug_query_paths:
            if label == "debug-import":
                continue
            document = self._read(path, label=label)
            expected = schema_by_label[label]
            if document.get("schema_version") != expected:
                raise ForgeError("PROVIDER_CONTRACT_INVALID", f"{label} did not emit {expected}")
            debug_references.append(
                self._ref(path, f"mncs-debug-{label.removeprefix('debug-')}", expected)
            )
        debug_references.insert(0, self._ref(witness_path, "mncs-debug-witness", WITNESS_SCHEMA))
        expected_queries = 9 if minimize else 8
        debug_status = "ESTABLISHED" if len(debug_references) == expected_queries else "UNKNOWN"
        base["debug"] = {
            "status": debug_status,
            "witness_id": witness.get("witness_id"),
            "execution_identity": witness.get("execution_identity"),
            "test_execution": integration.get("test_execution"),
            "outcome": witness.get("outcome"),
            "execution": debug_executions,
            "references": debug_references,
        }
        base["diagnosis"] = {
            "status": debug_status,
            "confidence": "bounded_evidence" if debug_status == "ESTABLISHED" else "none",
            "statement": (
                "mncs-debug correlated the failing TestExecution with bounded trace, inspection, provenance, and trace replay"
                if debug_status == "ESTABLISHED"
                else "debug evidence was incomplete; no diagnosis is established"
            ),
            "test_failure": selected.get("failure"),
            "source_location": selected.get("location"),
        }
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
        source_path.write_text(after_text, encoding="utf-8")
        after_source_sha = _sha256(source_path)
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
        )
        after_execution = self._run(
            after_argv, cwd, label="mncs-test-verification", timeout=timeout
        )
        after_test = self._read(after_result, label="mncs-test verification result")
        after_check_value = self._read(after_check, label="mncs-test verification check")
        self._validate_test_result(after_test)
        self._validate_check(after_check_value, "mncs-test")
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
            "status": after_test.get("verdict"),
            "result": self._ref(after_result, "mncs-test-result", TEST_RESULT_SCHEMA),
            "check": self._ref(after_check, "mncs-test-check", CHECK_RESULT_SCHEMA),
            "run_id": after_test.get("run_id"),
            "execution": after_execution,
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
        base["verdict"] = after_test.get("verdict")
        return self._persist(base, output_file)

    def _persist(self, result: dict[str, object], output_file: str | None) -> dict[str, object]:
        material = dict(result)
        material.pop("artifact", None)
        result["output_identity"] = local_json_identity(material)
        destination = (
            self._path(output_file)
            if output_file is not None
            else self.config.state_dir
            / "mncs-development"
            / f"{result['output_identity'].split(':')[-1]}.json"
        )
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(pretty_json(result), encoding="utf-8")
        result["artifact"] = {
            "kind": "forge-mncs-development",
            "path": str(destination),
            "sha256": _sha256(destination),
        }
        return result
