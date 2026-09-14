from __future__ import annotations

import asyncio
from pathlib import Path

from mncs_forge.config import load_config
from mncs_forge.engine import Forge
from mncs_forge.server import build_server


def test_mcp_tool_names_modes_and_schemas_come_from_registry(project: Path) -> None:
    config = load_config(project / "mncs-forge.toml")
    development_server = build_server(Forge(config))
    development = asyncio.run(development_server.list_tools())
    evaluator = asyncio.run(build_server(Forge(config, mode="evaluator")).list_tools())
    development_by_name = {tool.name: tool for tool in development}
    evaluator_names = {tool.name for tool in evaluator}
    assert "mncs_forge_final_evaluation_run" not in development_by_name
    assert evaluator_names == (
        set(development_by_name)
        - {
            "mncs_forge_compiler_experiment_record",
            "mncs_forge_concept_evaluation_record",
            "mncs_forge_compiler_candidate_register",
            "mncs_forge_compiler_candidate_attach",
            "mncs_forge_compiler_tournament",
            "mncs_forge_compiler_candidate_select",
            "mncs_forge_execution_assurance_assess",
            "mncs_forge_mncs_failure_loop",
        }
        | {"mncs_forge_final_evaluation_run"}
    )

    match_schema = development_by_name["mncs_forge_verifier_match"].inputSchema
    assert match_schema["required"] == ["uncertainty_classes"]
    assert set(match_schema["properties"]) == {
        "uncertainty_classes",
        "language",
        "artifact_type",
        "changed_paths",
        "scope",
        "maximum_cost",
        "required_category",
        "active_mode",
    }
    register_schema = development_by_name["mncs_forge_candidate_register"].inputSchema
    assert set(register_schema["required"]) == {
        "changed_files",
        "hypothesis",
        "generator_identity",
        "generator_config_identity",
    }
    resources = asyncio.run(development_server.list_resources())
    resource_uris = {str(resource.uri) for resource in resources}
    assert "mncs-forge://operations" in resource_uris
    assert "mncs-forge://compiler/experiments" in resource_uris
    assert "mncs-forge://compiler/candidates" in resource_uris


def test_mcp_generated_wrapper_preserves_result_compatibility(project: Path) -> None:
    server = build_server(Forge(load_config(project / "mncs-forge.toml")))
    result = asyncio.run(server.call_tool("mncs_forge_project_inspect", {}))
    assert isinstance(result, tuple)
    structured = result[1]
    assert structured["mode"] == "development"
    assert "lifecycle" in structured
