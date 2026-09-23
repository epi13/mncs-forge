from __future__ import annotations

import ast
import re
import tomllib
from pathlib import Path

PACKAGE = Path(__file__).parents[1] / "src" / "mncs_forge"
APPLICATION = PACKAGE / "application"
OPERATIONS = PACKAGE / "operations.py"


def imports(path: Path) -> list[tuple[str, str | None]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[tuple[str, str | None]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend((item.name, None) for item in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = "." * node.level + (node.module or "")
            found.extend((module, item.name) for item in node.names)
    return found


def test_application_imports_point_inward() -> None:
    forbidden_modules = {
        "argparse",
        "subprocess",
        "mcp",
        "mncs_forge.cli",
        "mncs_forge.server",
        "mncs_forge.engine",
        "mncs_forge.adapters",
    }
    failures: list[str] = []
    for path in sorted(APPLICATION.glob("*.py")):
        for module, name in imports(path):
            normalized = module.lstrip(".")
            if normalized in forbidden_modules or name in {
                "Forge",
                "LocalRecordStore",
                "run_bounded",
            }:
                failures.append(f"{path.name}: forbidden import {module}:{name}")
    assert failures == []


def test_domain_modules_do_not_import_interfaces_or_concrete_adapters() -> None:
    failures: list[str] = []
    for name in ("records.py", "state_machine.py"):
        for module, imported_name in imports(PACKAGE / name):
            normalized = module.lstrip(".")
            if normalized in {
                "cli",
                "server",
                "adapters",
                "record_store",
                "argparse",
                "subprocess",
                "mcp",
            }:
                failures.append(f"{name}: forbidden import {module}:{imported_name}")
    assert failures == []


def test_services_never_depend_on_forge_or_local_execution_function() -> None:
    service_paths = [*APPLICATION.glob("*.py"), PACKAGE / "micro_verifiers.py"]
    failures: list[str] = []
    for path in service_paths:
        source = path.read_text(encoding="utf-8")
        if "from .engine import" in source or "from ..engine import" in source:
            failures.append(f"{path.name}: imports engine")
        if "run_bounded(" in source:
            failures.append(f"{path.name}: calls concrete run_bounded")
        if "from .identity import" in source or "from ..identity import" in source:
            failures.append(f"{path.name}: imports concrete filesystem identity implementation")
        tree = ast.parse(source, filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == "__init__"
                and any(argument.arg == "forge" for argument in node.args.args)
            ):
                failures.append(f"{path.name}:{node.lineno}: constructor receives Forge")
    assert failures == []


def test_application_services_use_runner_port_without_subprocess_bypass() -> None:
    failures: list[str] = []
    for path in sorted(APPLICATION.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import) and any(
                alias.name == "subprocess" for alias in node.names
            ):
                failures.append(f"{path.name}: imports subprocess")
            if isinstance(node, ast.ImportFrom) and node.module == "subprocess":
                failures.append(f"{path.name}: imports subprocess")
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "run_bounded"
            ):
                failures.append(f"{path.name}:{node.lineno}: calls run_bounded")
        source = path.read_text(encoding="utf-8")
        if "CommandExecutor" in source or "LocalProcessRunner" in source:
            failures.append(f"{path.name}: depends on concrete/legacy executor name")
    assert failures == []


def test_subprocess_implementation_is_confined_to_execution_modules() -> None:
    # The continuous coordinator owns detached Language Service and Forge
    # lifetimes; the resource envelope owns systemd/cgroup realization. These
    # are the bounded host boundaries around process execution.
    direct_subprocess = sorted(
        path.relative_to(PACKAGE).as_posix()
        for path in PACKAGE.rglob("*.py")
        if any(
            isinstance(node, (ast.Import, ast.ImportFrom))
            and (
                (
                    isinstance(node, ast.Import)
                    and any(alias.name == "subprocess" for alias in node.names)
                )
                or (isinstance(node, ast.ImportFrom) and node.module == "subprocess")
            )
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), filename=str(path)))
        )
    )
    assert direct_subprocess == [
        "continuous.py",
        "execution.py",
        "execution_windows.py",
        "resource_envelope.py",
    ]


def test_core_does_not_import_fabric_or_fleet_mechanics() -> None:
    failures: list[str] = []
    for path in PACKAGE.rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        if "mncs_fabric" in source or "from mncs_fabric" in source:
            failures.append(f"{path.relative_to(PACKAGE)}: imports Fabric")
    for name in ("records.py", "state_machine.py"):
        source = (PACKAGE / name).read_text(encoding="utf-8")
        if "fabric_execution" in source:
            failures.append(f"{name}: domain imports Fabric adapter")
    for path in APPLICATION.glob("*.py"):
        source = path.read_text(encoding="utf-8")
        if "fabric_execution" in source or "ScriptedRunner" in source:
            failures.append(f"{path.name}: application imports Fabric adapter")
    assert failures == []


def test_mncs_receipt_adapter_consumes_observations_without_execution_bypass() -> None:
    adapter = PACKAGE / "mncs_execution_receipt.py"
    source = adapter.read_text(encoding="utf-8")
    assert "subprocess" not in source
    assert "run_bounded" not in source
    assert "LocalProcessRunner" not in source


def test_facade_composes_services_and_services_do_not_import_facade() -> None:
    engine_imports = imports(PACKAGE / "engine.py")
    application_imports = {
        module for module, _ in engine_imports if module.startswith(".application.")
    }
    assert {
        ".application.candidates",
        ".application.evaluation",
        ".application.evidence",
        ".application.execution_receipts",
        ".application.project",
        ".application.providers",
        ".application.recovery",
        ".application.workflows",
    }.issubset(application_imports)
    assert all(
        not any(module.lstrip(".") == "engine" for module, _ in imports(path))
        for path in APPLICATION.glob("*.py")
    )


def test_new_application_import_graph_is_acyclic() -> None:
    modules = {path.stem: path for path in APPLICATION.glob("*.py")}
    graph: dict[str, set[str]] = {name: set() for name in modules}
    for name, path in modules.items():
        for module, _ in imports(path):
            if module.startswith("."):
                target = module.lstrip(".").split(".")[-1]
                if target in modules:
                    graph[name].add(target)

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(name: str, trail: tuple[str, ...]) -> None:
        if name in visiting:
            raise AssertionError("application import cycle: " + " -> ".join((*trail, name)))
        if name in visited:
            return
        visiting.add(name)
        for target in sorted(graph[name]):
            visit(target, (*trail, name))
        visiting.remove(name)
        visited.add(name)

    for name in sorted(graph):
        visit(name, ())


def test_record_and_execution_boundaries_have_no_application_bypass() -> None:
    application_source = "\n".join(
        path.read_text(encoding="utf-8") for path in sorted(APPLICATION.glob("*.py"))
    )
    assert "ledger.append(" not in application_source
    assert "_write_immutable(" not in application_source
    assert "LocalRecordStore(" not in application_source
    direct_execution = sorted(
        path.relative_to(PACKAGE).as_posix()
        for path in PACKAGE.rglob("*.py")
        if "run_bounded(" in path.read_text(encoding="utf-8")
    )
    assert direct_execution == [
        "adapters.py",
        "execution.py",
        "mncs_native.py",
        "podman_runner.py",
    ]


def test_operation_registry_does_not_become_domain_storage_or_execution_layer() -> None:
    forbidden_modules = {
        "adapters",
        "execution",
        "identity",
        "record_store",
        "state_machine",
        "cli",
        "server",
        "argparse",
        "mcp",
        "subprocess",
    }
    failures = [
        f"forbidden import {module}:{name}"
        for module, name in imports(OPERATIONS)
        if module.lstrip(".") in forbidden_modules
    ]
    source = OPERATIONS.read_text(encoding="utf-8")
    for forbidden_call in (
        "LocalRecordStore(",
        "run_bounded(",
        "content_identity(",
        "authorize_",
        "record_store.commit(",
        "ledger.append(",
    ):
        if forbidden_call in source:
            failures.append(f"forbidden operation-registry behavior: {forbidden_call}")
    assert failures == []


def test_cli_and_mcp_have_no_independent_forge_business_dispatch() -> None:
    business_methods = {
        "doctor",
        "project_inspect",
        "state_inspect",
        "claim_status",
        "claim_blockers",
        "provider_list",
        "provider_probe",
        "capability_blockers",
        "verifier_list",
        "verifier_describe",
        "verifier_match",
        "verifier_run",
        "verifier_batch",
        "verifier_explain",
        "epoch_begin",
        "candidate_register",
        "development_checks_run",
        "failure_explain",
        "candidate_compare",
        "candidate_disposition",
        "candidate_freeze",
        "final_evaluation_run",
        "evidence_reconcile",
        "bundle_build",
        "execution_receipts_list",
        "execution_receipts_get",
    }
    failures: list[str] = []
    for interface in (PACKAGE / "cli.py", PACKAGE / "server.py"):
        tree = ast.parse(interface.read_text(encoding="utf-8"), filename=str(interface))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in business_methods
            ):
                failures.append(f"{interface.name}:{node.lineno}: direct {node.func.attr} call")
    assert failures == []

    server_source = (PACKAGE / "server.py").read_text(encoding="utf-8")
    assert server_source.count("server.tool(") == 1
    assert 'name="mncs_forge_' not in server_source
    cli_tree = ast.parse((PACKAGE / "cli.py").read_text(encoding="utf-8"))
    dispatch = next(
        node
        for node in ast.walk(cli_tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_dispatch"
    )
    calls = [node for node in ast.walk(dispatch) if isinstance(node, ast.Call)]
    assert any(
        isinstance(node.func, ast.Attribute) and node.func.attr == "invoke" for node in calls
    )
    assert not any(
        isinstance(node.func, ast.Attribute) and node.func.attr in business_methods
        for node in calls
    )


def test_resource_semantics_have_identity_bound_native_authority() -> None:
    workspace = PACKAGE.parents[1]
    architecture = (workspace / "docs" / "architecture.md").read_text(encoding="utf-8")
    block = re.search(r"```toml\n(.*?)\n```", architecture, re.DOTALL)
    assert block is not None, "architecture ownership table is missing"
    contract = tomllib.loads(block.group(1))
    assert contract.get("schema") == "mncs-forge.semantic-authority/1"
    entries = contract.get("capability")
    assert isinstance(entries, list)
    by_identity = {entry["identity"]: entry for entry in entries}
    assert len(by_identity) == len(entries)

    required = {
        "forge.resource-budget.v1": "resource_budget_select",
        "forge.resource-policy-identity.v1": "resource_budget_identity",
        "forge.resource-admission.v1": "resource_admission",
        "forge.resource-outcome.v1": "resource_outcome",
        "forge.continuous-verification-transition.v1": "verification_resource_transition",
        "forge.verification-queue-admission.v1": "verification_queue_admit",
        "forge.verification-status-decision.v1": "verification_status_decide",
        "forge.inflight-process-cancellation.v1": "verification_resource_transition",
    }
    assert required.keys() <= by_identity.keys()
    for identity, function in required.items():
        entry = by_identity[identity]
        source_path = workspace / entry["native_source"]
        host_path = workspace / entry["host_realization"].split("::", 1)[0]
        assert source_path.is_file(), identity
        assert f"fn {function}(" in source_path.read_text(encoding="utf-8"), identity
        assert host_path.is_file(), identity
        assert entry["status"].startswith("NATIVE_AUTHORITY"), identity
        assert entry["bootstrap_provisional"] is False, identity
        assert entry["host_must_not"], identity

    pressure = by_identity["mncs.process-resource-envelope.v1"]
    assert pressure["status"] == "LANGUAGE_PRESSURE"
    assert pressure["bootstrap_provisional"] is True
    assert pressure["pressure_ids"] == [
        "MNCS-LANG-64AD712CD2DE",
        "MNCS-TOOLING-B665F138D324",
    ]
    assert (workspace / pressure["reproducer"]).is_file()

    resource_source = (PACKAGE / "resource_envelope.py").read_text(encoding="utf-8")
    continuous_source = (PACKAGE / "continuous.py").read_text(encoding="utf-8")
    assert "resource_semantics.resource_budget_select(" in resource_source
    assert "resource_semantics.resource_budget_identity(" in resource_source
    assert "self._resource_semantics.resource_admission(" in resource_source
    assert "self._resource_semantics.resource_outcome(" in resource_source
    assert "verification_resource_transition" in continuous_source
    assert "verification_queue_admit" in continuous_source
    assert "verification_status_decide" in continuous_source
    assert "def select_resource_budget(" not in resource_source
