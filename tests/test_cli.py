from __future__ import annotations

import argparse
from pathlib import Path

from mncs_forge.cli import _common_parser, run
from mncs_forge.operations import DEFAULT_OPERATION_REGISTRY


def _parser_operations(
    parser: argparse.ArgumentParser, prefix: tuple[str, ...] = ()
) -> dict[tuple[str, ...], str]:
    found: dict[tuple[str, ...], str] = {}
    operation_id = parser.get_default("operation_id")
    if isinstance(operation_id, str):
        found[prefix] = operation_id
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for name, child in action.choices.items():
                found.update(_parser_operations(child, (*prefix, name)))
    return found


def test_argparse_commands_are_bound_to_registry_operations() -> None:
    parser_operations = _parser_operations(_common_parser())
    registered = {
        operation.cli.command: operation.operation_id
        for operation in DEFAULT_OPERATION_REGISTRY.for_cli()
        if operation.cli is not None
    }
    assert parser_operations == registered


def test_cli_existing_commands_and_inventory_remain_compatible(project: Path) -> None:
    config = str(project / "mncs-forge.toml")
    code, validated = run(["--config", config, "config", "validate"])
    assert code == 0
    assert validated == {
        "ok": True,
        "config": config,
        "project_root": str(project),
    }

    code, inspected = run(["--config", config, "inspect"])
    assert code == 0
    assert inspected["mode"] == "development"

    code, inventory = run(["--config", config, "operations"])
    assert code == 0
    assert inventory["schema_version"] == "1"
    assert len(inventory["operations"]) == 53


def test_cli_arguments_are_normalized_into_registered_inputs(project: Path) -> None:
    config = str(project / "mncs-forge.toml")
    code, epoch = run(
        [
            "--config",
            config,
            "epoch",
            "begin",
            "--generator",
            "cli-generator",
            "--evaluator",
            "cli-evaluator",
        ]
    )
    assert code == 0
    assert epoch["generator_identity"] == "cli-generator"
    code, candidate = run(
        [
            "--config",
            config,
            "candidate",
            "register",
            "--changed",
            "candidate/main.py",
            "--hypothesis",
            "registry argument compatibility",
            "--generator",
            "cli-generator",
            "--generator-config",
            "cli-config",
        ]
    )
    assert code == 0
    assert candidate["declared_hypothesis"] == "registry argument compatibility"


def test_cli_mode_rejection_uses_registry_gate(project: Path) -> None:
    code, result = run(
        [
            "--config",
            str(project / "mncs-forge.toml"),
            "--mode",
            "evaluator",
            "providers",
            "probe",
            "provider-pass",
        ]
    )
    assert code == 2
    assert result["error"]["code"] == "MODE_FORBIDDEN"
