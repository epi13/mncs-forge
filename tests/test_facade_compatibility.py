from __future__ import annotations

import inspect

from mncs_forge.config import ForgeConfig
from mncs_forge.engine import Forge


def test_forge_public_signatures_remain_stable() -> None:
    expected = {
        "doctor": "(self) -> 'dict[str, object]'",
        "project_inspect": "(self) -> 'dict[str, object]'",
        "state_inspect": "(self) -> 'dict[str, object]'",
        "provider_list": "(self) -> 'dict[str, object]'",
        "provider_probe": "(self, provider_id: 'str') -> 'dict[str, object]'",
        "verifier_list": "(self) -> 'dict[str, object]'",
        "verifier_explain": "(self, output_identity: 'str') -> 'dict[str, object]'",
        "failure_explain": "(self, output_identity: 'str | None' = None) -> 'dict[str, object]'",
        "candidate_compare": "(self, candidate_ids: 'list[str]') -> 'dict[str, object]'",
        "final_evaluation_run": "(self, workflow_names: 'list[str]') -> 'dict[str, object]'",
        "claim_status": "(self) -> 'dict[str, object]'",
        "claim_blockers": "(self, requested_claim: 'str') -> 'dict[str, object]'",
        "evidence_reconcile": "(self, candidate_id: 'str | None' = None) -> 'dict[str, object]'",
        "bundle_build": (
            "(self, workflow_name: 'str', candidate_id: 'str | None' = None) -> 'dict[str, object]'"
        ),
    }
    assert {name: str(inspect.signature(getattr(Forge, name))) for name in expected} == expected


def test_facade_preserves_representative_public_shapes(config: ForgeConfig) -> None:
    forge = Forge(config)
    doctor = forge.doctor()
    inspection = forge.project_inspect()
    lifecycle = forge.state_inspect()
    providers = forge.provider_list()
    verifiers = forge.verifier_list()
    claims = forge.claim_status()
    blockers = forge.claim_blockers("promotion")

    assert set(doctor) == {
        "ok",
        "forge_version",
        "config",
        "project_root",
        "mode",
        "ledger",
        "commands",
        "native_execution",
        "network_required",
        "limitations",
    }
    assert doctor["native_execution"]["mode"] in {"off", "prefer", "required"}
    assert {"project", "lifecycle", "configured_providers", "declared_micro_verifiers"} <= set(
        inspection
    )
    assert lifecycle["stage"] == "no_epoch"
    assert providers["dominance"] == "FAIL > UNKNOWN > PASS"
    assert verifiers["inspection_executed_providers"] is False
    assert claims["missing_is_pass"] is False
    assert blockers["blocked"] is True
