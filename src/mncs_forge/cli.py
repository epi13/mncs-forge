"""Human-facing MNCS Forge CLI."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .config import load_config
from .engine import Forge
from .errors import ForgeError
from .operations import (
    DEFAULT_OPERATION_REGISTRY,
    CliDecoder,
    OperationInterface,
)


def _cli_command(operation_id: str, part: int = -1) -> str:
    operation = DEFAULT_OPERATION_REGISTRY.resolve(operation_id)
    if operation.cli is None:  # pragma: no cover - registry metadata is validated at import
        raise RuntimeError(f"operation is not exposed through CLI: {operation_id}")
    return operation.cli.command[part]


def _register(parser: argparse.ArgumentParser, operation_id: str) -> argparse.ArgumentParser:
    parser.set_defaults(operation_id=operation_id)
    return parser


def _verifier_run_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--candidate")
    parser.add_argument("--changed", action="append", default=[])
    parser.add_argument("--scope")
    parser.add_argument("--source-region", help="bounded JSON source-region object")
    parser.add_argument("--contract")
    parser.add_argument("--dependency", action="append", default=[], metavar="NAME=IDENTITY")
    parser.add_argument("--prior-artifact")
    parser.add_argument("--parameters", default="{}", help="JSON object of declared parameters")


def _common_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mncs-forge")
    parser.add_argument("--config", type=Path, default=Path("mncs-forge.toml"))
    parser.add_argument("--mode", choices=("development", "evaluator"), default="development")
    parser.add_argument("--json", action="store_true", help="emit structured JSON (default)")
    commands = parser.add_subparsers(dest="command", required=True)
    _register(commands.add_parser(_cli_command("project.doctor")), "project.doctor")
    _register(commands.add_parser(_cli_command("project.inspect")), "project.inspect")
    _register(commands.add_parser(_cli_command("lifecycle.inspect")), "lifecycle.inspect")
    _register(commands.add_parser(_cli_command("claims.status")), "claims.status")
    blockers = _register(commands.add_parser(_cli_command("claims.blockers")), "claims.blockers")
    blockers.add_argument("claim", nargs="?", default="promotion")
    providers = commands.add_parser(_cli_command("providers.list", 0))
    provider_commands = providers.add_subparsers(dest="provider_command", required=True)
    _register(provider_commands.add_parser(_cli_command("providers.list")), "providers.list")
    probe = _register(
        provider_commands.add_parser(_cli_command("providers.probe")), "providers.probe"
    )
    probe.add_argument("provider_id")
    capability_blockers = _register(
        provider_commands.add_parser(_cli_command("providers.capability-blockers")),
        "providers.capability-blockers",
    )
    capability_blockers.add_argument("capabilities", nargs="*")
    learned_shadow = _register(
        provider_commands.add_parser(_cli_command("providers.learned-shadow")),
        "providers.learned-shadow",
    )
    learned_shadow.add_argument("artifact")
    learned_shadow.add_argument("--provider-command", action="append", required=True)
    learned_shadow.add_argument("--source-records", required=True)
    learned_shadow.add_argument("--context-observations", default="[]")
    learned_shadow.add_argument("--lineage")
    learned_shadow.add_argument("--timeout", type=float, default=5.0)

    verifier = commands.add_parser(_cli_command("verifiers.list", 0))
    verifier_commands = verifier.add_subparsers(dest="verifier_command", required=True)
    _register(verifier_commands.add_parser(_cli_command("verifiers.list")), "verifiers.list")
    verifier_describe = _register(
        verifier_commands.add_parser(_cli_command("verifiers.describe")),
        "verifiers.describe",
    )
    verifier_describe.add_argument("verifier_id")
    verifier_match = _register(
        verifier_commands.add_parser(_cli_command("verifiers.match")), "verifiers.match"
    )
    verifier_match.add_argument("--uncertainty", action="append", default=[])
    verifier_match.add_argument("--language")
    verifier_match.add_argument("--artifact-type")
    verifier_match.add_argument("--changed", action="append", default=[])
    verifier_match.add_argument("--scope")
    verifier_match.add_argument("--maximum-cost", choices=("low", "medium", "high"), default="high")
    verifier_match.add_argument("--category")
    verifier_match.add_argument("--active-mode", choices=("development", "evaluator"))
    verifier_run = _register(
        verifier_commands.add_parser(_cli_command("verifiers.run")), "verifiers.run"
    )
    verifier_run.add_argument("verifier_id")
    _verifier_run_arguments(verifier_run)
    verifier_batch = _register(
        verifier_commands.add_parser(_cli_command("verifiers.batch")), "verifiers.batch"
    )
    verifier_batch.add_argument("verifier_ids", nargs="+")
    _verifier_run_arguments(verifier_batch)
    verifier_explain = _register(
        verifier_commands.add_parser(_cli_command("verifiers.explain")), "verifiers.explain"
    )
    verifier_explain.add_argument("output_identity")

    epoch = commands.add_parser(_cli_command("epochs.begin", 0))
    epoch_commands = epoch.add_subparsers(dest="epoch_command", required=True)
    begin = _register(epoch_commands.add_parser(_cli_command("epochs.begin")), "epochs.begin")
    begin.add_argument("--generator", required=True)
    begin.add_argument("--evaluator", required=True)
    begin.add_argument("--parent")
    begin.add_argument("--authority-overlap", action="append", default=[])

    candidate = commands.add_parser(_cli_command("candidates.register", 0))
    candidate_commands = candidate.add_subparsers(dest="candidate_command", required=True)
    register = _register(
        candidate_commands.add_parser(_cli_command("candidates.register")),
        "candidates.register",
    )
    register.add_argument("--changed", action="append", default=[])
    register.add_argument("--hypothesis", required=True)
    register.add_argument("--generator", required=True)
    register.add_argument("--generator-config", required=True)
    register.add_argument("--parent")
    register.add_argument("--expected-identity")
    refresh = _register(
        candidate_commands.add_parser(_cli_command("candidates.refresh")),
        "candidates.refresh",
    )
    refresh.add_argument("--hypothesis", required=True)
    refresh.add_argument("--generator", required=True)
    refresh.add_argument("--generator-config", required=True)
    refresh.add_argument("--changed", action="append", default=[])
    compare = _register(
        candidate_commands.add_parser(_cli_command("candidates.compare")),
        "candidates.compare",
    )
    compare.add_argument("candidate_ids", nargs="+")
    for disposition in ("select", "reject"):
        operation_id = f"candidates.{disposition}"
        action = _register(candidate_commands.add_parser(_cli_command(operation_id)), operation_id)
        action.add_argument("candidate_id")
        action.add_argument("--reason", required=True)

    check = commands.add_parser(_cli_command("development.checks.run", 0))
    check_commands = check.add_subparsers(dest="check_command", required=True)
    development = _register(
        check_commands.add_parser(_cli_command("development.checks.run")),
        "development.checks.run",
    )
    development.add_argument("workflows", nargs="+")
    development.add_argument("--candidate")

    mncs = commands.add_parser(_cli_command("development.mncs.failure-loop", 0))
    mncs_commands = mncs.add_subparsers(dest="mncs_command", required=True)
    failure_loop = _register(
        mncs_commands.add_parser(_cli_command("development.mncs.failure-loop")),
        "development.mncs.failure-loop",
    )
    failure_loop.add_argument("manifest")
    failure_loop.add_argument("--test-command", default=None)
    failure_loop.add_argument("--debug-command", default=None)
    failure_loop.add_argument("--mncs-binary", default=None)
    failure_loop.add_argument("--library-paths", default=None)
    failure_loop.add_argument("--embed-library", default=None)
    failure_loop.add_argument("--working-directory", default=".")
    failure_loop.add_argument("--test-result-file", default=".mncs-forge/mncs-test-result.json")
    failure_loop.add_argument("--test-check-file", default=".mncs-forge/mncs-test-check.json")
    failure_loop.add_argument(
        "--test-artifacts-directory", default=".mncs-forge/mncs-test-artifacts"
    )
    failure_loop.add_argument("--debug-witness-file", default=".mncs-forge/mncs-debug-witness.json")
    failure_loop.add_argument(
        "--debug-artifacts-directory", default=".mncs-forge/mncs-debug-artifacts"
    )
    failure_loop.add_argument(
        "--capture-policy",
        choices=("failure-only", "selected", "bounded", "diagnostic", "events"),
        default="failure-only",
    )
    failure_loop.add_argument("--max-events", type=int, default=256)
    failure_loop.add_argument("--max-values", type=int, default=1024)
    failure_loop.add_argument("--max-value-bytes", type=int, default=4096)
    failure_loop.add_argument(
        "--selected-operation", dest="selected_operations", action="append", default=None
    )
    failure_loop.add_argument("--timeout-seconds", type=float, default=None)
    failure_loop.add_argument("--minimize", action="store_true")
    failure_loop.add_argument("--test-id", default=None)
    failure_loop.add_argument("--provider-mode", choices=("invoke", "consume"), default="invoke")
    failure_loop.add_argument("--debug-check-file", default=None)
    failure_loop.add_argument("--actions-evidence-files", default=None)
    failure_loop.add_argument("--repair-path", default=None)
    failure_loop.add_argument("--repair-from", default=None)
    failure_loop.add_argument("--repair-to", default=None)
    failure_loop.add_argument("--verification-plan-file", default=None)
    failure_loop.add_argument("--post-repair-verification-plan-file", default=None)
    failure_loop.add_argument("--ravel-command", default=None)
    failure_loop.add_argument(
        "--diagnostic-depth", choices=("minimal", "standard", "deep"), default="minimal"
    )
    failure_loop.add_argument("--output-file", default=None)

    explain = _register(
        commands.add_parser(_cli_command("development.failure.explain")),
        "development.failure.explain",
    )
    explain.add_argument("--result")
    freeze = _register(commands.add_parser(_cli_command("candidates.freeze")), "candidates.freeze")
    freeze.add_argument("candidate_id")
    freeze.add_argument("--environment", required=True)
    freeze.add_argument("--evidence-plan", required=True)
    evaluate = _register(
        commands.add_parser(_cli_command("evaluation.final.run")), "evaluation.final.run"
    )
    evaluate.add_argument("workflows", nargs="+")
    reconcile = _register(
        commands.add_parser(_cli_command("evidence.reconcile")), "evidence.reconcile"
    )
    reconcile.add_argument("--candidate")
    bundle = _register(commands.add_parser(_cli_command("bundles.build")), "bundles.build")
    bundle.add_argument("workflow")
    bundle.add_argument("--candidate")

    receipts = commands.add_parser(_cli_command("execution.receipts.list", 0))
    receipt_commands = receipts.add_subparsers(dest="receipt_command", required=True)
    receipt_list = _register(
        receipt_commands.add_parser(_cli_command("execution.receipts.list")),
        "execution.receipts.list",
    )
    receipt_list.add_argument("--candidate")
    receipt_list.add_argument("--action")
    receipt_get = _register(
        receipt_commands.add_parser(_cli_command("execution.receipts.get")),
        "execution.receipts.get",
    )
    receipt_get.add_argument("binding_id")

    license_evidence = commands.add_parser(_cli_command("rights.license-evidence.scan", 0))
    license_evidence_commands = license_evidence.add_subparsers(
        dest="license_evidence_command", required=True
    )
    _register(
        license_evidence_commands.add_parser(_cli_command("rights.license-evidence.scan")),
        "rights.license-evidence.scan",
    )

    assessments = commands.add_parser(_cli_command("execution.assurance.list", 0))
    assessment_commands = assessments.add_subparsers(dest="assessment_command", required=True)
    assessment_request = _register(
        assessment_commands.add_parser(_cli_command("execution.assurance.assess")),
        "execution.assurance.assess",
    )
    assessment_request.add_argument("binding")
    assessment_request.add_argument("--requested", action="append", default=[], required=True)
    assessment_request.add_argument("--policy", default=None)
    assessment_list = _register(
        assessment_commands.add_parser(_cli_command("execution.assurance.list")),
        "execution.assurance.list",
    )
    assessment_list.add_argument("--binding", dest="binding_identity")
    assessment_list.add_argument("--candidate", dest="candidate_identity")

    cell = commands.add_parser(_cli_command("cell.documents.validate", 0))
    cell_commands = cell.add_subparsers(dest="cell_command", required=True)
    cell_validate = _register(
        cell_commands.add_parser(_cli_command("cell.documents.validate")),
        "cell.documents.validate",
    )
    cell_validate.add_argument("kind", choices=["policy", "test-bundle", "execution-record"])
    cell_validate.add_argument("document", help="Forge Cell document JSON object")
    cell_assess = _register(
        cell_commands.add_parser(_cli_command("cell.execution.assess")),
        "cell.execution.assess",
    )
    cell_assess.add_argument("policy", help="Forge Cell policy JSON object")
    cell_assess.add_argument("record", help="Forge Cell execution-record JSON object")
    cell_assess.add_argument("--nonce", dest="expected_nonce", default=None)

    compiler = commands.add_parser(_cli_command("compiler.experiments.list", 0))
    compiler_commands = compiler.add_subparsers(dest="compiler_command", required=True)
    compiler_record = _register(
        compiler_commands.add_parser(_cli_command("compiler.experiments.record")),
        "compiler.experiments.record",
    )
    compiler_record.add_argument("record", help="language compiler-study JSON object")
    _register(
        compiler_commands.add_parser(_cli_command("compiler.experiments.list")),
        "compiler.experiments.list",
    )
    compiler_compare = _register(
        compiler_commands.add_parser(_cli_command("compiler.experiments.compare")),
        "compiler.experiments.compare",
    )
    compiler_compare.add_argument("left")
    compiler_compare.add_argument("right")
    compiler_candidate_register = _register(
        compiler_commands.add_parser(_cli_command("compiler.candidates.register")),
        "compiler.candidates.register",
    )
    compiler_candidate_register.add_argument("baseline")
    compiler_candidate_register.add_argument("candidate_artifact")
    compiler_candidate_register.add_argument("generator")
    compiler_candidate_register.add_argument("transformation")
    compiler_candidate_register.add_argument("relation")
    compiler_candidate_register.add_argument("benefit")
    compiler_candidate_register.add_argument("--protected", action="append", default=[])
    compiler_candidate_register.add_argument("--target", default="unspecified")
    compiler_candidate_register.add_argument(
        "--required-validation",
        dest="required_validation",
        default="translation-validation",
    )
    _register(
        compiler_commands.add_parser(_cli_command("compiler.candidates.list")),
        "compiler.candidates.list",
    )
    compiler_candidate_compare = _register(
        compiler_commands.add_parser(_cli_command("compiler.candidates.compare")),
        "compiler.candidates.compare",
    )
    compiler_candidate_compare.add_argument("left")
    compiler_candidate_compare.add_argument("right")
    compiler_candidate_attach = _register(
        compiler_commands.add_parser(_cli_command("compiler.candidates.attach-validation")),
        "compiler.candidates.attach-validation",
    )
    compiler_candidate_attach.add_argument("candidate_id")
    compiler_candidate_attach.add_argument("validator")
    compiler_candidate_attach.add_argument("judgement")
    compiler_candidate_attach.add_argument("relation")
    compiler_candidate_attach.add_argument("--counterexample")
    compiler_candidate_attach.add_argument("--limitations", action="append", default=[])
    compiler_candidate_attach.add_argument("--stale", action="store_true")
    compiler_candidate_attach.add_argument("--expected-artifact", dest="expected_artifact_identity")
    compiler_tournament = _register(
        compiler_commands.add_parser(_cli_command("compiler.tournament.run")),
        "compiler.tournament.run",
    )
    compiler_tournament.add_argument("candidates", nargs="+")
    compiler_candidate_select = _register(
        compiler_commands.add_parser(_cli_command("compiler.candidates.select")),
        "compiler.candidates.select",
    )
    compiler_candidate_select.add_argument("candidate_id")
    compiler_candidate_select.add_argument(
        "--policy",
        default="explicit-protected-property-policy",
    )
    compiler_candidate_inspect = _register(
        compiler_commands.add_parser(_cli_command("compiler.candidates.inspect")),
        "compiler.candidates.inspect",
    )
    compiler_candidate_inspect.add_argument("candidate_id")

    evaluations = commands.add_parser(_cli_command("concept.evaluations.list", 0))
    evaluations_commands = evaluations.add_subparsers(dest="evaluations_command", required=True)
    evaluations_record = _register(
        evaluations_commands.add_parser(_cli_command("concept.evaluations.record")),
        "concept.evaluations.record",
    )
    evaluations_record.add_argument("evaluation", help="built Forge concept-evaluation JSON object")
    _register(
        evaluations_commands.add_parser(_cli_command("concept.evaluations.list")),
        "concept.evaluations.list",
    )
    evaluations_get = _register(
        evaluations_commands.add_parser(_cli_command("concept.evaluations.get")),
        "concept.evaluations.get",
    )
    evaluations_get.add_argument("id")

    ledger = commands.add_parser(_cli_command("ledger.verify", 0))
    ledger_commands = ledger.add_subparsers(dest="ledger_command", required=True)
    _register(ledger_commands.add_parser(_cli_command("ledger.verify")), "ledger.verify")
    config = commands.add_parser(_cli_command("config.validate", 0))
    config_commands = config.add_subparsers(dest="config_command", required=True)
    _register(config_commands.add_parser(_cli_command("config.validate")), "config.validate")
    _register(commands.add_parser(_cli_command("operations.inventory")), "operations.inventory")
    return parser


def _json_object(value: str, label: str) -> dict[str, object]:
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise ForgeError("CLI_INPUT", f"{label} must be a JSON object")
    return {str(key): item for key, item in parsed.items()}


def _json_value(value: str, label: str) -> object:
    try:
        return json.loads(value)
    except json.JSONDecodeError as exc:
        raise ForgeError("CLI_INPUT", f"{label} must be valid JSON") from exc


def _dependencies(values: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for value in values:
        name, separator, identity = value.partition("=")
        if not separator or not name or not identity:
            raise ForgeError("CLI_INPUT", "dependency must use NAME=IDENTITY")
        if name in result:
            raise ForgeError("CLI_INPUT", f"duplicate dependency name: {name}")
        result[name] = identity
    return result


def _cli_payload(args: argparse.Namespace) -> dict[str, object]:
    operation = DEFAULT_OPERATION_REGISTRY.resolve(str(args.operation_id))
    if operation.cli is None:  # pragma: no cover - parser uses only CLI operations
        raise ForgeError("OPERATION_NOT_EXPOSED", "operation is not exposed through CLI")
    payload: dict[str, object] = {}
    for binding in operation.cli.bindings:
        value = getattr(args, binding.namespace_name)
        if binding.decoder is CliDecoder.JSON_VALUE and value is not None:
            value = _json_value(str(value), binding.namespace_name.replace("_", "-"))
        elif binding.decoder is CliDecoder.JSON_OBJECT and value is not None:
            value = _json_object(str(value), binding.namespace_name.replace("_", "-"))
        elif binding.decoder is CliDecoder.DEPENDENCIES:
            value = _dependencies(list(value))
        payload[binding.input_name] = value
    return payload


def _dispatch(forge: Forge, args: argparse.Namespace) -> object:
    return DEFAULT_OPERATION_REGISTRY.invoke(
        forge,
        str(args.operation_id),
        _cli_payload(args),
        interface=OperationInterface.CLI,
    )


def run(argv: list[str] | None = None) -> tuple[int, dict[str, Any]]:
    args = _common_parser().parse_args(argv)
    try:
        config = load_config(args.config)
        value = _dispatch(Forge(config, mode=args.mode), args)
        if isinstance(value, dict):
            return 0, value
        return 0, {"ok": True, "result": value}
    except ForgeError as exc:
        return 2, exc.as_dict()
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        error = ForgeError("UNEXPECTED_INPUT", str(exc))
        return 2, error.as_dict()


def _cli_json(value: object) -> object:
    """Make Forge CLI output JSON-serializable without changing record meaning."""

    if isinstance(value, Mapping):
        return {str(key): _cli_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_cli_json(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def main(argv: list[str] | None = None) -> int:
    code, value = run(argv)
    json.dump(
        _cli_json(value),
        sys.stdout,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        indent=2,
    )
    sys.stdout.write("\n")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
