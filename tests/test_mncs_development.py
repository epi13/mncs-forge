from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from mncs_forge.engine import Forge

PROJECTS = Path(__file__).resolve().parents[2]
MNCS_TEST = Path(os.environ.get("MNCS_TEST_REPO", PROJECTS / "mncs-test"))
MNCS_DEBUG = Path(os.environ.get("MNCS_DEBUG_REPO", PROJECTS / "mncs-debug"))
MNCS_LANGUAGE = Path(os.environ.get("MNCS_LANGUAGE_REPO", PROJECTS / "mncs-language"))
RAVEL = Path(os.environ.get("RAVEL_REPO", PROJECTS / "RAVEL"))
MNCS_BINARY = Path(os.environ.get("MNCS", MNCS_LANGUAGE / "target/debug/mncs"))
MNCS_EMBED = Path(
    os.environ.get("MNCS_EMBED_LIBRARY", MNCS_LANGUAGE / "target/debug/libmncs_embed.so")
)
DEVELOPMENT_SCHEMA = (
    Path(__file__).resolve().parents[1]
    / "src/mncs_forge/resources/mncs-forge-mncs-development.schema.json"
)


def _require_native_toolchain() -> None:
    required = (
        MNCS_TEST / "tools/mncs_test.py",
        MNCS_DEBUG / "bin/mncs-debug",
        MNCS_BINARY,
        MNCS_EMBED,
    )
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        pytest.skip("native MNCS integration toolchain is unavailable: " + ", ".join(missing))


def _fixture_manifest(project: Path) -> Path:
    candidate = project / "candidate"
    shutil.copyfile(
        MNCS_TEST / "tests/fixtures/first_class_failing.mncs",
        candidate / "first_class_failing.mncs",
    )
    manifest = candidate / "first-class-failing.toml"
    text = (MNCS_TEST / "tests/fixtures/first-class-failing.toml").read_text(encoding="utf-8")
    # Provider library paths are supplied explicitly by Forge so the fixture can
    # remain inside the configured project root.
    manifest.write_text(
        text.replace('libraries = ["../../native"]', "libraries = []"), encoding="utf-8"
    )
    return manifest


def _forge_with_native_headroom(config):
    raw = {**config.raw, "limits": {**config.raw["limits"], "output_bytes": 65536}}
    return Forge(replace(config, raw=raw))


def _commands() -> tuple[list[str], list[str]]:
    return (
        [sys.executable, str(MNCS_TEST / "tools/mncs_test.py")],
        [sys.executable, str(MNCS_DEBUG / "bin/mncs-debug")],
    )


def _write_ravel_plan(project: Path, source: Path) -> Path:
    inventory = subprocess.run(
        [str(MNCS_BINARY), "test-inventory", str(source)],
        cwd=project,
        env={
            **os.environ,
            "MNCS_LIBRARY_PATH": os.pathsep.join(
                (str(MNCS_TEST / "native"), str(MNCS_LANGUAGE / "library"))
            ),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert inventory.returncode == 0, inventory.stderr + inventory.stdout
    inventory_document = json.loads(inventory.stdout)
    root = inventory_document["inventory"]["tests"][0]["function_identity"]
    plan = project / ".mncs-forge" / "verification-plan.json"
    result = subprocess.run(
        [
            sys.executable,
            str(RAVEL / "src" / "ravel" / "impact.py"),
            str(source),
            "--mncs",
            str(MNCS_BINARY),
            "--library",
            str(MNCS_TEST / "native"),
            "--library",
            str(MNCS_LANGUAGE / "library"),
            "--root",
            root,
            "--output",
            str(plan),
        ],
        cwd=project,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr + result.stdout
    assert plan.is_file()
    return plan


def test_failure_loop_preserves_lineage_and_verifies_exact_repair(config, project: Path) -> None:
    _require_native_toolchain()
    _fixture_manifest(project)
    test_command, debug_command = _commands()
    forge = _forge_with_native_headroom(config)

    output = forge.mncs_failure_loop(
        manifest="candidate/first-class-failing.toml",
        test_command=test_command,
        debug_command=debug_command,
        mncs_binary=str(MNCS_BINARY),
        library_paths=[str(MNCS_TEST / "native"), str(MNCS_LANGUAGE / "library")],
        embed_library=str(MNCS_EMBED),
        timeout_seconds=120,
        minimize=True,
        repair_path="candidate/first_class_failing.mncs",
        repair_from="equals_i64(7, 6, 2001)",
        repair_to="equals_i64(6, 6, 2001)",
        output_file="output/mncs-failure-loop.json",
    )

    assert output["verdict"] == "PASS"
    assert output["test"]["verdict"] == "FAIL"
    assert output["debug"]["status"] == "ESTABLISHED"
    assert output["diagnosis"]["status"] == "ESTABLISHED"
    assert output["debug"]["observation"]["schema_revision"] == "mncs.execution-observation/1"
    assert output["debug"]["observation"]["identity"]
    assert output["debug"]["source_map"]["schema_revision"] == "mncs.execution-source-map/1"
    assert output["diagnosis"]["evidence_basis"]["authority"] == "mncs-debug"
    assert output["diagnosis"]["evidence_basis"]["observation_identity"]
    assert output["diagnosis"]["completeness"]["observation"]["status"] == "complete"
    assert output["diagnosis"]["consumed_values"]
    assert output["repair"]["status"] == "APPLIED"
    assert output["verification"]["status"] == "PASS"
    continuity = output["provenance"]["identity_continuity"]
    assert continuity["before_test_id"] == output["debug"]["test_execution"]["test_id"]
    assert continuity["debug_witness_id"] == output["debug"]["witness_id"]
    assert continuity["before_test_run_id"] == output["test"]["run_id"]
    assert continuity["after_test_run_id"] == output["verification"]["run_id"]
    assert len(output["debug"]["references"]) == 9
    assert output["debug"]["references"][-1]["schema_revision"] == "mncs.debug-minimization/1"

    persisted = json.loads((project / "output/mncs-failure-loop.json").read_text(encoding="utf-8"))
    assert persisted["output_identity"] == output["output_identity"]
    assert output["artifact"]["sha256"]
    assert output["artifact"]["path"].endswith("output/mncs-failure-loop.json")
    schema = json.loads(DEVELOPMENT_SCHEMA.read_text(encoding="utf-8"))
    assert list(Draft202012Validator(schema).iter_errors(output)) == []


def test_failure_loop_consumes_ravel_plan_and_rebinds_after_repair(config, project: Path) -> None:
    _require_native_toolchain()
    manifest = _fixture_manifest(project)
    source = project / "candidate" / "first_class_failing.mncs"
    plan = _write_ravel_plan(project, source)
    test_command, debug_command = _commands()
    forge = _forge_with_native_headroom(config)

    output = forge.mncs_failure_loop(
        manifest="candidate/first-class-failing.toml",
        test_command=test_command,
        debug_command=debug_command,
        mncs_binary=str(MNCS_BINARY),
        library_paths=[str(MNCS_TEST / "native"), str(MNCS_LANGUAGE / "library")],
        embed_library=str(MNCS_EMBED),
        verification_plan_file=str(plan.relative_to(project)),
        ravel_command=[sys.executable, str(RAVEL / "src" / "ravel" / "impact.py")],
        diagnostic_depth="minimal",
        timeout_seconds=120,
        repair_path="candidate/first_class_failing.mncs",
        repair_from="equals_i64(7, 6, 2001)",
        repair_to="equals_i64(6, 6, 2001)",
        output_file="output/mncs-ravel-plan-loop.json",
    )

    assert output["verdict"] == "PASS"
    assert output["impact"]["plan"]["level"] == "direct_dependents"
    assert output["impact"]["after_plan"]["plan_id"] != output["impact"]["plan"]["plan_id"]
    assert output["debug"]["diagnostic_depth"] == "minimal"
    assert output["debug"]["diagnostic_operations"] == ["validation", "inspection", "sufficiency"]
    assert output["observability"]["selected_test_count"] == 1
    assert output["observability"]["reused_evidence"]["post_repair_plan"] is False
    assert output["verification"]["selection"]["selected_count"] == 1
    schema = json.loads(DEVELOPMENT_SCHEMA.read_text(encoding="utf-8"))
    assert list(Draft202012Validator(schema).iter_errors(output)) == []


def test_failure_loop_consumes_actions_handoff_and_verifies_exact_repair(
    config, project: Path
) -> None:
    handoff_root_value = os.environ.get("MNCS_ACTIONS_HANDOFF_ROOT")
    if not handoff_root_value:
        pytest.skip("Actions handoff artifacts are only available in the fixed family canary")
    handoff_root = Path(handoff_root_value)
    required = (
        "fail-result.json",
        "fail-check.json",
        "debug-check.json",
        "debug-witness.json",
        "fail-evidence/evidence-manifest.json",
        "fail-evidence/execution-receipt.json",
        "debug-evidence/evidence-manifest.json",
        "debug-evidence/execution-receipt.json",
    )
    handoff = project / ".mncs-actions"
    for relative in required:
        source = handoff_root / relative
        assert source.is_file(), f"Actions did not preserve required handoff artifact: {relative}"
        destination = handoff / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)

    _fixture_manifest(project)
    test_command, debug_command = _commands()
    forge = _forge_with_native_headroom(config)
    output = forge.mncs_failure_loop(
        manifest="candidate/first-class-failing.toml",
        test_command=test_command,
        debug_command=debug_command,
        mncs_binary=str(MNCS_BINARY),
        library_paths=[str(MNCS_TEST / "native"), str(MNCS_LANGUAGE / "library")],
        embed_library=str(MNCS_EMBED),
        provider_mode="consume",
        test_result_file=".mncs-actions/fail-result.json",
        test_check_file=".mncs-actions/fail-check.json",
        debug_witness_file=".mncs-actions/debug-witness.json",
        debug_check_file=".mncs-actions/debug-check.json",
        actions_evidence_files=[
            ".mncs-actions/fail-evidence/evidence-manifest.json",
            ".mncs-actions/fail-evidence/execution-receipt.json",
            ".mncs-actions/debug-evidence/evidence-manifest.json",
            ".mncs-actions/debug-evidence/execution-receipt.json",
        ],
        timeout_seconds=120,
        repair_path="candidate/first_class_failing.mncs",
        repair_from="equals_i64(7, 6, 2001)",
        repair_to="equals_i64(6, 6, 2001)",
        output_file="output/mncs-actions-handoff-loop.json",
    )

    assert output["verdict"] == "PASS"
    assert output["test"]["verdict"] == "FAIL"
    assert output["test"]["execution"]["source"] == "mncs-actions"
    assert output["debug"]["status"] == "ESTABLISHED"
    assert output["repair"]["status"] == "APPLIED"
    assert output["verification"]["status"] == "PASS"
    handoff_projection = output["provenance"]["provider_handoff"]
    assert handoff_projection["provider"] == "mncs-actions"
    assert handoff_projection["debug_check"]["schema_revision"] == "mncs.check-result/1"
    assert len(handoff_projection["references"]) == 4
    continuity = output["provenance"]["identity_continuity"]
    assert continuity["before_test_id"] == output["debug"]["test_execution"]["test_id"]
    assert continuity["debug_witness_id"] == output["debug"]["witness_id"]
    assert continuity["after_test_run_id"] == output["verification"]["run_id"]
    schema = json.loads(DEVELOPMENT_SCHEMA.read_text(encoding="utf-8"))
    assert list(Draft202012Validator(schema).iter_errors(output)) == []
