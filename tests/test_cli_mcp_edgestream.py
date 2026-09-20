from __future__ import annotations

import asyncio
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from mncs_forge.cli import run
from mncs_forge.edgestream import inspect


def installed_mcp_executable() -> Path:
    executable = shutil.which("mncs-forge-server")
    if executable is not None:
        return Path(executable)
    return Path(__file__).parents[1] / ".venv" / "bin" / "mncs-forge-server"


def required_mcp_executable() -> Path:
    executable = installed_mcp_executable()
    if not executable.is_file():
        pytest.skip(
            "INFRASTRUCTURE_FAILURE: missing prerequisite mncs-forge-server; "
            "provision the Forge environment before running MCP integration tests"
        )
    return executable


def test_cli_smoke(project: Path) -> None:
    code, result = run(["--config", str(project / "mncs-forge.toml"), "config", "validate"])
    assert code == 0
    assert result["ok"] is True
    code, result = run(["--config", str(project / "mncs-forge.toml"), "inspect"])
    assert code == 0
    assert result["mode"] == "development"


# Both smoke timeouts below are hang guards, not performance contracts: the
# spawned server performs native toolchain calls, so slow toolchains need
# headroom while a wedged server still fails loudly.
def test_direct_mcp_protocol_smoke(project: Path) -> None:
    root = Path(__file__).parents[1]
    executable = required_mcp_executable()
    result = subprocess.run(
        [
            sys.executable,
            str(root / "scripts/mcp-smoke.py"),
            str(executable),
            str(project / "mncs-forge.toml"),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert '"expected_tools_present": true' in result.stdout


def test_mcp_health_probe_reports_healthy(project: Path) -> None:
    root = Path(__file__).parents[1]
    executable = required_mcp_executable()
    result = subprocess.run(
        [
            sys.executable,
            str(root / "scripts/mcp-health.py"),
            str(executable),
            str(project / "mncs-forge.toml"),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert '"status": "healthy"' in result.stdout
    assert '"reachable": true' in result.stdout


def test_mcp_startup_reports_missing_configuration(tmp_path: Path) -> None:
    executable = required_mcp_executable()
    result = subprocess.run(
        [str(executable), "--config", str(tmp_path / "missing.toml"), "--mode", "development"],
        input="",
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 2
    assert "MNCS Forge startup failed [CONFIG_READ]" in result.stderr


def test_codex_launcher_uses_relocatable_module_entrypoint(project: Path) -> None:
    root = Path(__file__).parents[1]
    launcher = root / "scripts" / "codex-mcp"
    command = [
        str(launcher),
        "--config",
        str(project / "mncs-forge.toml"),
        "--mode",
        "development",
    ]
    if sys.platform == "win32":
        bash = shutil.which("bash")
        if bash is None:
            pytest.skip("the Codex launcher is a Bash entry point")
        command.insert(0, bash)
    result = subprocess.run(
        command,
        input="",
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0
    assert "bad interpreter" not in result.stderr


async def _provider_mcp_calls(executable: Path, config: Path) -> None:
    parameters = StdioServerParameters(
        command=str(executable),
        args=["--config", str(config), "--mode", "development"],
    )
    async with (
        stdio_client(parameters) as (reader, writer),
        ClientSession(reader, writer) as session,
    ):
        await session.initialize()
        tools = await session.list_tools()
        names = {tool.name for tool in tools.tools}
        assert "mncs_forge_providers_list" in names
        assert "mncs_forge_provider_probe" in names
        assert "mncs_forge_capability_blockers" in names
        assert "mncs_forge_verifier_list" in names
        assert "mncs_forge_verifier_describe" in names
        assert "mncs_forge_verifier_match" in names
        assert "mncs_forge_verifier_run" in names
        assert "mncs_forge_verifier_batch" in names
        assert "mncs_forge_verifier_explain" in names
        assert "mncs_forge_state_inspect" in names
        assert "mncs_forge_final_evaluation_run" not in names
        listing = await session.call_tool("mncs_forge_providers_list", {})
        assert not listing.isError
        probe = await session.call_tool(
            "mncs_forge_provider_probe", {"provider_id": "provider-pass"}
        )
        assert not probe.isError
        assert probe.structuredContent["status"] == "PASS"  # type: ignore[index]
        blockers = await session.call_tool(
            "mncs_forge_capability_blockers",
            {"required_capabilities": ["bounded-structural"]},
        )
        assert not blockers.isError
        assert blockers.structuredContent["status"] == "PASS"  # type: ignore[index]
        verifiers = await session.call_tool("mncs_forge_verifier_list", {})
        assert not verifiers.isError
        assert verifiers.structuredContent["configured_count"] > 0  # type: ignore[index]
        described = await session.call_tool(
            "mncs_forge_verifier_describe", {"verifier_id": "verify-pass"}
        )
        assert not described.isError
        assert described.structuredContent["method"] == "bounded-structural"  # type: ignore[index]
        matched = await session.call_tool(
            "mncs_forge_verifier_match",
            {
                "uncertainty_classes": ["structural"],
                "language": "python",
                "artifact_type": "source",
                "scope": "file",
                "maximum_cost": "low",
            },
        )
        assert not matched.isError
        assert matched.structuredContent["match_outcome"] == "MATCHED"  # type: ignore[index]
        epoch = await session.call_tool(
            "mncs_forge_epoch_begin",
            {
                "generator_identity": "mcp-generator",
                "evaluator_identity": "mcp-evaluator",
            },
        )
        assert not epoch.isError
        candidate = await session.call_tool(
            "mncs_forge_candidate_register",
            {
                "changed_files": ["candidate/main.py"],
                "hypothesis": "MCP verifier response shape",
                "generator_identity": "mcp-generator",
                "generator_config_identity": "mcp-generator-config",
            },
        )
        assert not candidate.isError
        verified = await session.call_tool(
            "mncs_forge_verifier_run",
            {
                "verifier_id": "verify-pass",
                "candidate_identity": candidate.structuredContent["candidate_id"],
                "changed_paths": ["candidate/main.py"],
                "scope": "file",
            },
        )
        assert not verified.isError
        assert verified.structuredContent["status"] == "PASS"  # type: ignore[index]
        assert str(verified.structuredContent["output_identity"]).startswith(
            "forge-json-sha256-v1:"
        )
        lifecycle = await session.call_tool("mncs_forge_state_inspect", {})
        assert not lifecycle.isError
        code, cli_lifecycle = run(["--config", str(config), "state"])
        assert code == 0
        assert lifecycle.structuredContent == cli_lifecycle


def test_mcp_provider_list_probe_and_blockers(project: Path) -> None:
    executable = required_mcp_executable()
    asyncio.run(_provider_mcp_calls(executable, project / "mncs-forge.toml"))


def test_edgestream_read_only_integration_fixture(tmp_path: Path) -> None:
    required = [
        "specification/contract.md",
        "reference/edgestream_reference.c",
        "machine/edgestream_generated.c",
        "preregistration.json",
        "mncds/development-record.json",
        "evidence/results/study-summary.json",
    ]
    for relative in required:
        path = tmp_path / "case-studies/edgestream" / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}\n", encoding="utf-8")
    before = {
        path.relative_to(tmp_path): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    }
    result = inspect(tmp_path)
    after = {
        path.relative_to(tmp_path): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    }
    assert result["status"] == "PASS"
    assert before == after
    assert "protected custody" in str(result["limitations"])
