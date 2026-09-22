"""Local stdio MCP adapter generated from the canonical Forge operation registry."""

from __future__ import annotations

import argparse
import inspect
import json
import sys
from collections.abc import Callable, Mapping
from dataclasses import MISSING, fields
from pathlib import Path
from typing import get_type_hints

from mcp.server.fastmcp import FastMCP

from .config import ForgeConfig, load_config
from .engine import Forge
from .errors import ForgeError
from .operations import (
    DEFAULT_OPERATION_REGISTRY,
    OperationDefinition,
    OperationInterface,
)


class _LazyForge:
    """Construct Forge only when a request actually needs application state."""

    def __init__(self, factory: Callable[[], Forge], *, mode: str) -> None:
        self._factory = factory
        self.mode = mode
        self._forge: Forge | None = None

    def _instance(self) -> Forge:
        if self._forge is None:
            self._forge = self._factory()
        return self._forge

    def __getattr__(self, name: str) -> object:
        return getattr(self._instance(), name)


def _mcp_callable(
    forge: Forge | _LazyForge, operation: OperationDefinition
) -> Callable[..., dict[str, object]]:
    """Create a flat-signature FastMCP adapter from one typed input model."""

    def invoke(**values: object) -> dict[str, object]:
        return DEFAULT_OPERATION_REGISTRY.invoke(
            forge,
            operation.operation_id,
            values,
            interface=OperationInterface.MCP,
        )

    hints = get_type_hints(operation.input_model)
    parameters: list[inspect.Parameter] = []
    for item in fields(operation.input_model):
        default: object = inspect.Parameter.empty
        if item.default is not MISSING:
            default = item.default
        elif item.default_factory is not MISSING:
            default = item.default_factory()
        parameters.append(
            inspect.Parameter(
                item.name,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                default=default,
                annotation=hints[item.name],
            )
        )
    invoke.__signature__ = inspect.Signature(  # type: ignore[attr-defined]
        parameters, return_annotation=dict[str, object]
    )
    exposure = operation.mcp
    if exposure is None:  # pragma: no cover - created only for MCP-exposed operations
        raise RuntimeError(f"operation is not MCP-exposed: {operation.operation_id}")
    invoke.__name__ = exposure.tool_name.removeprefix("mncs_forge_")
    invoke.__doc__ = operation.description
    return invoke


def build_server(forge: Forge | _LazyForge) -> FastMCP:
    server = FastMCP(
        "MNCS Forge",
        instructions=(
            "Experimental MNCS-native development control plane. Keep MNCS and MNCDS "
            "statuses separate, preserve UNKNOWN, and use offline validators as authorities."
        ),
    )

    for operation in DEFAULT_OPERATION_REGISTRY.for_mcp(forge.mode):
        exposure = operation.mcp
        if exposure is None:  # pragma: no cover - filtered by for_mcp
            continue
        server.tool(
            name=exposure.tool_name,
            description=operation.description,
        )(_mcp_callable(forge, operation))

    def resource(
        operation_id: str,
        uri: str,
        payload: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        return DEFAULT_OPERATION_REGISTRY.invoke(
            forge,
            operation_id,
            payload,
            interface=OperationInterface.RESOURCE,
            resource_uri=uri,
        )

    @server.resource("mncs-forge://project/authority-map")
    def authority_map() -> str:
        return json.dumps(
            resource("project.inspect", "mncs-forge://project/authority-map"), sort_keys=True
        )

    @server.resource("mncs-forge://state/active-epoch")
    def active_epoch() -> str:
        return json.dumps(
            resource("project.inspect", "mncs-forge://state/active-epoch")["current_epoch"],
            sort_keys=True,
        )

    @server.resource("mncs-forge://state/lifecycle")
    def lifecycle_state() -> str:
        return json.dumps(
            resource("lifecycle.inspect", "mncs-forge://state/lifecycle"), sort_keys=True
        )

    @server.resource("mncs-forge://state/active-candidate")
    def active_candidate() -> str:
        return json.dumps(
            resource("project.inspect", "mncs-forge://state/active-candidate")["active_candidate"],
            sort_keys=True,
        )

    @server.resource("mncs-forge://evidence/latest-summary")
    def evidence_summary() -> str:
        return json.dumps(
            resource("evidence.reconcile", "mncs-forge://evidence/latest-summary"),
            sort_keys=True,
        )

    @server.resource("mncs-forge://claims/blockers")
    def blockers_resource() -> str:
        return json.dumps(
            resource(
                "claims.blockers",
                "mncs-forge://claims/blockers",
                {"requested_claim": "promotion"},
            ),
            sort_keys=True,
        )

    @server.resource("mncs-forge://providers/configured")
    def configured_providers() -> str:
        return json.dumps(
            resource("providers.list", "mncs-forge://providers/configured"), sort_keys=True
        )

    @server.resource("mncs-forge://providers/capability-blockers")
    def provider_blockers_resource() -> str:
        return json.dumps(
            resource(
                "providers.capability-blockers",
                "mncs-forge://providers/capability-blockers",
            ),
            sort_keys=True,
        )

    @server.resource("mncs-forge://verifiers/declared")
    def declared_verifiers() -> str:
        return json.dumps(
            resource("verifiers.list", "mncs-forge://verifiers/declared"), sort_keys=True
        )

    @server.resource("mncs-forge://operations")
    def operation_inventory() -> str:
        return json.dumps(
            resource("operations.inventory", "mncs-forge://operations"), sort_keys=True
        )

    @server.resource("mncs-forge://compiler/experiments")
    def compiler_experiments() -> str:
        return json.dumps(
            resource("compiler.experiments.list", "mncs-forge://compiler/experiments"),
            sort_keys=True,
        )

    @server.resource("mncs-forge://compiler/candidates")
    def compiler_candidates() -> str:
        return json.dumps(
            resource("compiler.candidates.list", "mncs-forge://compiler/candidates"),
            sort_keys=True,
        )

    @server.resource("mncs-forge://execution/assessments")
    def execution_assessments() -> str:
        return json.dumps(
            resource(
                "execution.assurance.list",
                "mncs-forge://execution/assessments",
            ),
            sort_keys=True,
        )

    @server.resource("mncs-forge://guide/usage")
    def usage_guide() -> str:
        return (
            "Inspect authority, begin an epoch, register candidates, run declared development "
            "checks or explicitly selected bounded micro-verifiers, compare under policy, select, "
            "freeze, then start a separate evaluator-mode server. Never use final evaluation as "
            "repair feedback. MNCS/MNCDS validators remain offline authorities; missing evidence "
            "stays UNKNOWN."
        )

    @server.prompt(name="start_controlled_machine_native_epoch")
    def start_epoch_prompt() -> str:
        return (
            "Inspect the Forge project and claim boundaries. Begin a development epoch with "
            "explicit generator/evaluator identities and authority overlap. Modify only declared "
            "candidate/generated paths and register every candidate identity."
        )

    @server.prompt(name="evaluate_and_compare_candidates")
    def compare_prompt() -> str:
        return (
            "Run only declared development checks, reconcile FAIL > UNKNOWN > PASS, then compare "
            "candidate identities under the configured selection policy. Do not select from one "
            "benchmark observation or when evidence is missing or incomparable."
        )

    @server.prompt(name="explain_unknown_claim")
    def explain_unknown_prompt() -> str:
        return (
            "Read separate claim statuses and blockers. State the exact unresolved fact, affected "
            "claim, provider limitation, required evidence class, and whether external authority "
            "is required. Never silently promote UNKNOWN to PASS."
        )

    @server.prompt(name="prepare_candidate_for_freeze")
    def freeze_prompt() -> str:
        return (
            "Verify candidate identity, complete declared required evidence, compare under the "
            "predeclared rule, record selection, and freeze contract/reference/evaluator/policy/"
            "environment identities. Final evaluation belongs to evaluator mode and is not repair "
            "feedback for the same epoch."
        )

    @server.prompt(name="review_failed_development_check")
    def failed_check_prompt() -> str:
        return (
            "Use the compact failure explanation, inspect only disclosed witnesses and locations, "
            "repair within declared writable paths, register a descendant candidate, and rerun the "
            "declared check. Preserve the failed record."
        )

    return server


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="mncs-forge-server")
    parser.add_argument("--config", type=Path, default=Path("mncs-forge.toml"))
    parser.add_argument("--mode", choices=("development", "evaluator"), default="development")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    try:
        # Configuration validation is cheap and remains on the process-start
        # path. Store-backed persistence and native service construction are
        # intentionally deferred until the first request so MCP initialize and
        # tool discovery do not wait for Store artifact compilation.
        config: ForgeConfig = load_config(args.config)
        forge = _LazyForge(lambda: Forge(config, mode=args.mode), mode=args.mode)
        build_server(forge).run(transport="stdio")
    except ForgeError as exc:
        print(f"MNCS Forge startup failed [{exc.code}]: {exc.message}", file=sys.stderr)
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
