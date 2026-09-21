from __future__ import annotations

import hashlib
import json
from dataclasses import fields, replace

import pytest

from mncs_forge.config import ForgeConfig
from mncs_forge.engine import Forge
from mncs_forge.errors import ForgeError
from mncs_forge.operations import (
    ALL_MODES,
    DEFAULT_OPERATION_REGISTRY,
    DEVELOPMENT_ONLY,
    AuthorityRequirement,
    CliExposure,
    DisclosureClass,
    LifecycleRequirement,
    McpExposure,
    MutationClass,
    NoInput,
    OperationDefinition,
    OperationInput,
    OperationInterface,
    OperationRegistry,
    OutputContract,
    canonical_operation_inventory,
)

EXPECTED_CLI = {
    "blockers",
    "bundle",
    "candidate compare",
    "freeze",
    "candidate register",
    "candidate refresh",
    "candidate reject",
    "candidate select",
    "check development",
    "continuous run",
    "continuous status",
    "config validate",
    "compiler candidate-attach",
    "compiler candidate-compare",
    "compiler candidate-inspect",
    "compiler candidate-list",
    "compiler candidate-register",
    "compiler candidate-select",
    "compiler compare",
    "compiler list",
    "compiler record",
    "compiler tournament",
    "assessments list",
    "assessments request",
    "cell assess",
    "cell validate",
    "evaluations get",
    "evaluations list",
    "evaluations record",
    "doctor",
    "epoch begin",
    "evaluate",
    "explain",
    "inspect",
    "ledger verify",
    "license-evidence scan",
    "mncs failure-loop",
    "operations",
    "providers blockers",
    "providers learned-shadow",
    "providers list",
    "providers probe",
    "receipts get",
    "receipts list",
    "reconcile",
    "state",
    "status",
    "verifier batch",
    "verifier describe",
    "verifier explain",
    "verifier list",
    "verifier match",
    "verifier run",
}

EXPECTED_DEVELOPMENT_MCP = {
    "mncs_forge_bundle_build",
    "mncs_forge_license_evidence_scan",
    "mncs_forge_candidate_compare",
    "mncs_forge_candidate_freeze",
    "mncs_forge_candidate_register",
    "mncs_forge_candidate_refresh",
    "mncs_forge_candidate_reject",
    "mncs_forge_candidate_select",
    "mncs_forge_capability_blockers",
    "mncs_forge_claim_blockers",
    "mncs_forge_claim_status",
    "mncs_forge_compiler_candidate_attach",
    "mncs_forge_compiler_candidate_inspect",
    "mncs_forge_compiler_candidate_register",
    "mncs_forge_compiler_candidate_select",
    "mncs_forge_compiler_candidates_compare",
    "mncs_forge_compiler_candidates_list",
    "mncs_forge_compiler_experiment_record",
    "mncs_forge_compiler_experiments_compare",
    "mncs_forge_compiler_experiments_list",
    "mncs_forge_compiler_tournament",
    "mncs_forge_concept_evaluation_get",
    "mncs_forge_concept_evaluation_record",
    "mncs_forge_concept_evaluations_list",
    "mncs_forge_development_checks_run",
    "mncs_forge_continuous_run",
    "mncs_forge_continuous_status",
    "mncs_forge_mncs_failure_loop",
    "mncs_forge_evidence_reconcile",
    "mncs_forge_epoch_begin",
    "mncs_forge_cell_document_validate",
    "mncs_forge_cell_execution_assess",
    "mncs_forge_execution_assurance_assess",
    "mncs_forge_execution_assurance_list",
    "mncs_forge_execution_receipts_get",
    "mncs_forge_execution_receipts_list",
    "mncs_forge_failure_explain",
    "mncs_forge_project_inspect",
    "mncs_forge_provider_probe",
    "mncs_forge_learned_specialist_shadow",
    "mncs_forge_providers_list",
    "mncs_forge_state_inspect",
    "mncs_forge_verifier_batch",
    "mncs_forge_verifier_describe",
    "mncs_forge_verifier_explain",
    "mncs_forge_verifier_list",
    "mncs_forge_verifier_match",
    "mncs_forge_verifier_run",
}


def semantic_snapshot() -> list[tuple[object, ...]]:
    return [
        (
            item.operation_id,
            tuple(sorted(item.modes)),
            item.mutation.value,
            item.input_model.__name__,
            item.output.value,
            tuple(item.cli.command) if item.cli else None,
            item.mcp.tool_name if item.mcp else None,
            tuple(sorted(item.mcp.visible_modes)) if item.mcp else None,
        )
        for item in DEFAULT_OPERATION_REGISTRY.operations
    ]


def test_registry_is_unique_validated_and_deterministically_ordered() -> None:
    operation_ids = [item.operation_id for item in DEFAULT_OPERATION_REGISTRY.operations]
    assert operation_ids == sorted(operation_ids)
    assert len(operation_ids) == len(set(operation_ids)) == 53
    assert all(callable(item.handler) for item in DEFAULT_OPERATION_REGISTRY.operations)
    assert all(
        fields(item.input_model) is not None for item in DEFAULT_OPERATION_REGISTRY.operations
    )


def test_semantic_compatibility_snapshot() -> None:
    snapshot = semantic_snapshot()
    assert snapshot[0] == (
        "bundles.build",
        ("development", "evaluator"),
        "mutating",
        "BundleBuildInput",
        "bundle-result",
        ("bundle",),
        "mncs_forge_bundle_build",
        ("development", "evaluator"),
    )
    assert snapshot[-1][0] == "verifiers.run"
    assert {
        " ".join(item.cli.command) for item in DEFAULT_OPERATION_REGISTRY.for_cli() if item.cli
    } == EXPECTED_CLI


def test_inventory_is_canonical_json_and_omits_unstable_handler_details() -> None:
    first = json.dumps(canonical_operation_inventory(), sort_keys=True, separators=(",", ":"))
    second = json.dumps(canonical_operation_inventory(), sort_keys=True, separators=(",", ":"))
    assert first == second
    inventory = canonical_operation_inventory()
    assert inventory["schema_version"] == "1"
    assert len(inventory["operations"]) == 53
    assert "0x" not in first
    assert "handler" not in first
    semantic = json.dumps(
        {
            **inventory,
            "operations": [
                {
                    **item,
                    "mcp": {key: value for key, value in item["mcp"].items() if key != "reason"},
                }
                for item in inventory["operations"]
            ],
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    assert hashlib.sha256(semantic.encode()).hexdigest() == (
        "955e3fb96e4ff64025029537a3085ddf013c491422e31ac82c44b2cfe943c0c4"
    )


def test_cli_and_mcp_coverage_and_intentional_asymmetry() -> None:
    development_tools = {
        item.mcp.tool_name for item in DEFAULT_OPERATION_REGISTRY.for_mcp("development") if item.mcp
    }
    evaluator_tools = {
        item.mcp.tool_name for item in DEFAULT_OPERATION_REGISTRY.for_mcp("evaluator") if item.mcp
    }
    assert development_tools == EXPECTED_DEVELOPMENT_MCP
    assert evaluator_tools == (
        EXPECTED_DEVELOPMENT_MCP
        - {
            "mncs_forge_compiler_experiment_record",
            "mncs_forge_concept_evaluation_record",
            "mncs_forge_compiler_candidate_register",
            "mncs_forge_compiler_candidate_attach",
            "mncs_forge_compiler_tournament",
            "mncs_forge_compiler_candidate_select",
            "mncs_forge_execution_assurance_assess",
            "mncs_forge_mncs_failure_loop",
            "mncs_forge_continuous_run",
        }
        | {"mncs_forge_final_evaluation_run"}
    )
    excluded = [item for item in DEFAULT_OPERATION_REGISTRY.operations if item.mcp is None]
    assert {item.operation_id for item in excluded} == {
        "config.validate",
        "ledger.verify",
        "operations.inventory",
        "project.doctor",
    }
    assert all(item.mcp_exclusion for item in excluded)
    assert all(item.cli is not None for item in DEFAULT_OPERATION_REGISTRY.operations)


def test_mode_rejection_precedes_handler_execution() -> None:
    calls: list[str] = []

    def handler(_forge: object, _request: OperationInput) -> dict[str, object]:
        calls.append("called")
        return {"ok": True}

    operation = OperationDefinition(
        operation_id="test.development-only",
        modes=DEVELOPMENT_ONLY,
        mutation=MutationClass.READ_ONLY,
        input_model=NoInput,
        output=OutputContract.DIAGNOSTIC,
        handler=handler,  # type: ignore[arg-type]
        authority=AuthorityRequirement.DEVELOPMENT,
        lifecycle=LifecycleRequirement.NONE,
        disclosure=DisclosureClass.LOCAL_PROJECT,
        description="test operation",
        cli=None,
        mcp=McpExposure("test_tool", ALL_MODES),
        cli_exclusion="test intentionally has no CLI exposure",
    )
    registry = OperationRegistry([operation])
    forge = type("FakeForge", (), {"mode": "evaluator"})()
    with pytest.raises(ForgeError) as caught:
        registry.invoke(  # type: ignore[arg-type]
            forge, operation.operation_id, interface=OperationInterface.MCP
        )
    assert caught.value.code == "MODE_FORBIDDEN"
    assert calls == []


def test_same_definition_and_handler_serve_cli_and_mcp() -> None:
    calls: list[str] = []

    def handler(_forge: object, request: OperationInput) -> dict[str, object]:
        assert isinstance(request, NoInput)
        calls.append("called")
        return {"call": len(calls)}

    operation = OperationDefinition(
        operation_id="test.shared",
        modes=ALL_MODES,
        mutation=MutationClass.READ_ONLY,
        input_model=NoInput,
        output=OutputContract.DIAGNOSTIC,
        handler=handler,  # type: ignore[arg-type]
        authority=AuthorityRequirement.NONE,
        lifecycle=LifecycleRequirement.NONE,
        disclosure=DisclosureClass.PUBLIC_METADATA,
        description="shared test operation",
        cli=CliExposure(("shared",)),
        mcp=McpExposure("shared_tool"),
    )
    registry = OperationRegistry([operation])
    forge = type("FakeForge", (), {"mode": "development"})()
    assert registry.invoke(  # type: ignore[arg-type]
        forge, "test.shared", interface=OperationInterface.CLI
    ) == {"call": 1}
    assert registry.invoke(  # type: ignore[arg-type]
        forge, "test.shared", interface=OperationInterface.MCP
    ) == {"call": 2}
    assert registry.resolve("test.shared").handler is handler


def test_unregistered_malformed_and_wrong_interface_requests_fail_closed() -> None:
    forge = type("FakeForge", (), {"mode": "development"})()
    with pytest.raises(ForgeError) as missing:
        DEFAULT_OPERATION_REGISTRY.invoke(  # type: ignore[arg-type]
            forge, "not.registered", interface=OperationInterface.CLI
        )
    assert missing.value.code == "OPERATION_NOT_FOUND"
    with pytest.raises(ForgeError) as unexpected:
        DEFAULT_OPERATION_REGISTRY.invoke(  # type: ignore[arg-type]
            forge,
            "project.doctor",
            {"unexpected": True},
            interface=OperationInterface.CLI,
        )
    assert unexpected.value.code == "OPERATION_INPUT"
    with pytest.raises(ForgeError) as exposure:
        DEFAULT_OPERATION_REGISTRY.invoke(  # type: ignore[arg-type]
            forge, "project.doctor", interface=OperationInterface.MCP
        )
    assert exposure.value.code == "OPERATION_NOT_EXPOSED"


def test_mutation_metadata_matches_persisted_operation_set() -> None:
    mutating = {
        item.operation_id
        for item in DEFAULT_OPERATION_REGISTRY.operations
        if item.mutation is MutationClass.MUTATING
    }
    assert mutating == {
        "bundles.build",
        "candidates.freeze",
        "candidates.refresh",
        "candidates.register",
        "candidates.reject",
        "candidates.select",
        "compiler.candidates.attach-validation",
        "compiler.candidates.register",
        "compiler.experiments.record",
        "concept.evaluations.record",
        "development.checks.run",
        "development.continuous.run",
        "development.mncs.failure-loop",
        "epochs.begin",
        "evaluation.final.run",
        "execution.assurance.assess",
        "providers.probe",
        "verifiers.batch",
        "verifiers.run",
    }


def test_registry_gate_preserves_state_machine_authorization(config: ForgeConfig) -> None:
    forge = Forge(config, mode="development")
    with pytest.raises(ForgeError) as caught:
        DEFAULT_OPERATION_REGISTRY.invoke(
            forge,
            "candidates.register",
            {
                "changed_files": ["candidate/main.py"],
                "hypothesis": "must still require an epoch",
                "generator_identity": "generator",
                "generator_config_identity": "generator-config",
            },
            interface=OperationInterface.CLI,
        )
    assert caught.value.code == "NO_ACTIVE_EPOCH"


@pytest.mark.parametrize(
    ("operations", "message"),
    [
        (
            [
                OperationDefinition(
                    operation_id="test.duplicate",
                    modes=ALL_MODES,
                    mutation=MutationClass.READ_ONLY,
                    input_model=NoInput,
                    output=OutputContract.DIAGNOSTIC,
                    handler=lambda _forge, _request: {},
                    authority=AuthorityRequirement.NONE,
                    lifecycle=LifecycleRequirement.NONE,
                    disclosure=DisclosureClass.PUBLIC_METADATA,
                    description="duplicate",
                    cli=CliExposure(("one",)),
                    mcp=McpExposure("one"),
                ),
                OperationDefinition(
                    operation_id="test.duplicate",
                    modes=ALL_MODES,
                    mutation=MutationClass.READ_ONLY,
                    input_model=NoInput,
                    output=OutputContract.DIAGNOSTIC,
                    handler=lambda _forge, _request: {},
                    authority=AuthorityRequirement.NONE,
                    lifecycle=LifecycleRequirement.NONE,
                    disclosure=DisclosureClass.PUBLIC_METADATA,
                    description="duplicate",
                    cli=CliExposure(("two",)),
                    mcp=McpExposure("two"),
                ),
            ],
            "duplicate canonical operation ID",
        )
    ],
)
def test_invalid_registry_definitions_are_rejected(
    operations: list[OperationDefinition], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        OperationRegistry(operations)


def test_duplicate_public_names_and_undocumented_asymmetry_are_rejected() -> None:
    template = DEFAULT_OPERATION_REGISTRY.resolve("operations.inventory")
    first = replace(
        template,
        operation_id="test.first",
        cli=CliExposure(("duplicate",)),
        mcp=McpExposure("first_tool"),
        mcp_exclusion=None,
    )
    duplicate_cli = replace(
        first,
        operation_id="test.second",
        mcp=McpExposure("second_tool"),
    )
    with pytest.raises(ValueError, match="duplicate or empty CLI command"):
        OperationRegistry([first, duplicate_cli])

    duplicate_mcp = replace(
        first,
        operation_id="test.second",
        cli=CliExposure(("second",)),
    )
    with pytest.raises(ValueError, match="duplicate MCP tool name"):
        OperationRegistry([first, duplicate_mcp])

    undocumented = replace(first, cli=None, cli_exclusion=None)
    with pytest.raises(ValueError, match="CLI asymmetry is undocumented"):
        OperationRegistry([undocumented])


def test_evaluator_only_operation_visibility_fails_closed_at_construction() -> None:
    final_evaluation = DEFAULT_OPERATION_REGISTRY.resolve("evaluation.final.run")
    unsafe = replace(
        final_evaluation,
        operation_id="test.unsafe-evaluator",
        cli=CliExposure(("unsafe",), final_evaluation.cli.bindings if final_evaluation.cli else ()),
        mcp=McpExposure("unsafe_evaluator", ALL_MODES),
    )
    with pytest.raises(ValueError, match="evaluator-only tool visibility is unsafe"):
        OperationRegistry([unsafe])
