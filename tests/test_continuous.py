from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from mncs_forge.continuous import ContinuousSupervisor
from mncs_forge.engine import Forge
from mncs_forge.record_store import LocalRecordStore

from conftest import with_native_latency_allowance


def _supervisor() -> ContinuousSupervisor:
    return object.__new__(ContinuousSupervisor)


def _event() -> dict[str, object]:
    return {
        "current_generation": 7,
        "current": {"uri": "file:///workspace/src/main.mncs", "identity": "mncs:source:7"},
        "semantic_subjects": [{"identity": "mncs:fn:main", "change": "changed"}],
        "diagnostics": {"added": ["E-MODULE-OLD"], "resolved": []},
        "obligations": {"added": [], "resolved": [], "status_changed": []},
        "impact_complete": True,
        "impact": {
            "complete": True,
            "change_kinds": ["public_contract"],
            "guarantee_domains": ["compatibility"],
            "risk_flags": ["effect_semantics"],
            "nodes": [{"kind": "function"}],
        },
    }


def test_trigger_matching_is_explicit_and_compact() -> None:
    supervisor = _supervisor()
    event = _event()
    assert supervisor._matches(
        {
            "id": "contract-security",
            "action": "security_micro_verifier",
            "maximum_cost": "low",
            "event_kinds": ["public_contract_changed"],
            "change_kinds": ["public_contract"],
            "guarantee_domains": ["compatibility"],
            "subject_kinds": ["function"],
            "diagnostics": ["E-MODULE-*"],
            "risk_flags": ["effect_semantics"],
            "security": True,
        },
        event,
    )
    assert not supervisor._matches(
        {
            "id": "unrelated",
            "action": "micro_verifier",
            "maximum_cost": "low",
            "event_kinds": ["obligation_changed"],
        },
        event,
    )


def test_event_kinds_preserve_security_and_attention_classifications() -> None:
    supervisor = _supervisor()
    kinds = supervisor._event_kinds(_event())
    assert {
        "source_changed",
        "diagnostic_added",
        "semantic_subject_changed",
        "security_boundary_changed",
        "public_contract_changed",
    }.issubset(kinds)


def test_trigger_can_bind_contract_and_obligation_identities() -> None:
    supervisor = _supervisor()
    event = _event()
    event["obligations"] = {
        "added": ["mncs:obligation:authority-boundary"],
        "resolved": [],
        "status_changed": [],
    }
    event["impact"]["nodes"] = [
        {"kind": "contract", "identity": "mncs:contract:public-api"},
        {"kind": "function", "identity": "mncs:fn:main"},
    ]
    assert supervisor._matches(
        {
            "id": "declared-boundary",
            "action": "security_micro_verifier",
            "maximum_cost": "low",
            "contract_identities": ["mncs:contract:public-*"],
            "obligation_identities": ["mncs:obligation:authority-*"],
        },
        event,
    )
    assert not supervisor._matches(
        {
            "id": "other-boundary",
            "action": "security_micro_verifier",
            "maximum_cost": "low",
            "obligation_identities": ["mncs:obligation:unrelated"],
        },
        event,
    )


def test_debounce_policy_is_bounded_and_trigger_declared() -> None:
    supervisor = _supervisor()
    supervisor.settings = {
        "debounce_ms": 120,
        "triggers": [{"id": "burst", "debounce_ms": 9000}],
    }
    assert supervisor._debounce_ms() == 5000


def test_escalation_policy_controls_attention_without_changing_verdict() -> None:
    supervisor = _supervisor()
    event = _event()
    supervisor.attention = []
    supervisor._escalate(event, {"escalation": "silent"}, "hidden", status="FAIL")
    assert supervisor.attention == []
    supervisor._escalate(event, {"escalation": "unknown"}, "fail-hidden", status="FAIL")
    assert supervisor.attention == []
    supervisor._escalate(event, {"escalation": "unknown"}, "unknown-visible")
    assert len(supervisor.attention) == 1


def test_unknown_action_keeps_reconciled_event_unknown() -> None:
    supervisor = _supervisor()
    supervisor.settings = {
        "triggers": [
            {
                "id": "reconcile",
                "action": "verification_plan",
                "maximum_cost": "low",
                "event_kinds": ["workspace_reconciled"],
            }
        ]
    }
    supervisor.current_generation = 0
    supervisor.current_source_identity = None
    supervisor.current_cursor = 0
    supervisor.stale_jobs = 0
    supervisor.repairs = []
    supervisor.attention = []
    supervisor.statuses = []
    supervisor.selected_test_ids = []
    supervisor.active_tier = "edit-time"
    supervisor._selected_verification = lambda _client, _event, _trigger: {
        "status": "UNKNOWN",
        "reason": "incomplete compiler impact",
    }
    event = {**_event(), "reconciled": True, "impact_complete": False, "impact": None}
    result = supervisor._process_event(SimpleNamespace(request=lambda *_args: {"generation": 7}), event)
    assert result["status"] == "UNKNOWN"


def test_reconciled_security_trigger_runs_bounded_verifier_and_reuses_evidence(
    config, project
) -> None:
    configured = with_native_latency_allowance(
        replace(
            config,
            raw={
                **config.raw,
                "native": {"mode": "off"},
                "continuous": {
                    "enabled": True,
                    "triggers": [
                        {
                            "id": "offline-security",
                            "action": "security_micro_verifier",
                            "maximum_cost": "low",
                            "security": True,
                            "event_kinds": ["security_boundary_changed"],
                            "verifier_ids": ["verify-security-pass"],
                            "complete_dependency_envelope": True,
                        }
                    ],
                },
            },
        )
    )
    # This focused trigger test uses the explicitly identified in-process
    # adapter so provider/trigger behavior is isolated from the native Store
    # benchmark. Store-backed evidence reuse is covered separately.
    forge = Forge(configured, record_store=LocalRecordStore(project / ".local-state"))
    forge.epoch_begin(generator_identity="generator-v1", evaluator_identity="evaluator-v1")
    candidate = forge.candidate_register(
        changed_files=["candidate/main.py"],
        hypothesis="bounded security trigger fixture",
        generator_identity="generator-v1",
        generator_config_identity="generator-config-v1",
    )
    supervisor = ContinuousSupervisor(forge)
    supervisor.settings["candidate_identity"] = str(candidate["candidate_id"])
    event = {
        "current_generation": 2,
        "cursor": 1,
        "reconciled": True,
        "current": {
            "uri": f"file://{project / 'candidate/main.py'}",
            "identity": "mncs:source:offline-edit",
        },
        "semantic_subjects": [],
        "diagnostics": {"added": [], "resolved": []},
        "obligations": {"added": [], "resolved": [], "status_changed": []},
        "impact_complete": False,
        "impact": {"change_kinds": [], "risk_flags": ["effect_semantics"], "nodes": []},
    }
    trigger = supervisor.settings["triggers"][0]
    assert supervisor._matches(trigger, event)  # type: ignore[arg-type]

    client = SimpleNamespace(request=lambda method, _params: {"generation": 2})
    cold = supervisor._process_event(client, event)
    warm = supervisor._process_event(client, {**event, "cursor": 2})
    assert cold["status"] == "PASS"
    assert cold["actions"][0]["result"]["results"][0]["reused"] is False  # type: ignore[index]
    assert warm["status"] == "PASS"
    assert warm["actions"][0]["result"]["results"][0]["reused"] is True  # type: ignore[index]
    assert supervisor.recomputed_evidence == 1
    assert supervisor.reused_evidence == 1

    unrelated = {
        **event,
        "cursor": 2,
        "current": {
            "uri": f"file://{project / 'contract/contract.md'}",
            "identity": "mncs:source:unrelated-edit",
        },
    }
    unrelated_result = supervisor._micro(unrelated, trigger)  # type: ignore[arg-type]
    assert unrelated_result["results"][0]["reused"] is True  # type: ignore[index]
    assert supervisor.reused_evidence == 2

    (project / "reference/reference.py").write_text("VALUE = 2\n", encoding="utf-8")
    dependency_change = {
        **event,
        "cursor": 3,
        "current": {
            "uri": f"file://{project / 'reference/reference.py'}",
            "identity": "mncs:source:dependency-edit",
        },
    }
    invalidated = supervisor._micro(dependency_change, trigger)  # type: ignore[arg-type]
    assert invalidated["results"][0]["reused"] is False  # type: ignore[index]
    assert supervisor.recomputed_evidence == 2


def test_ravel_plan_passes_existing_commons_family_overlay(tmp_path: Path) -> None:
    output_commands: list[list[str]] = []

    class DevelopmentService:
        @staticmethod
        def _validate_verification_plan(_plan: dict[str, object]) -> None:
            return None

    class Config:
        root = tmp_path
        continuous_settings = {
            "library_paths": [],
            "cross_repository": True,
            "family_graph_file": "family/semantic-edges-v1.json",
            "commons_root": "../MNCS-Commons",
            "repository": "mncs-forge",
            "obligation_inventory": "family/obligations.json",
            "current_evidence": "family/evidence.json",
            "obligation_output": "family/verification-plan.json",
        }

        @staticmethod
        def public_commands() -> dict[str, list[str]]:
            return {"ravel_impact": ["ravel"], "mncs": ["mncs"]}

    supervisor = object.__new__(ContinuousSupervisor)
    supervisor.config = Config()
    supervisor.settings = Config.continuous_settings
    supervisor.forge = SimpleNamespace(
        _mncs_development_service=DevelopmentService(),
    )

    def run(command: list[str], *, cwd: Path, timeout: float) -> dict[str, object]:
        del cwd, timeout
        output_commands.append(command)
        output = Path(command[command.index("--output") + 1])
        output.write_text(json.dumps({"selection": {"selected_test_identities": []}}))
        return {"returncode": 0, "stderr": ""}

    supervisor._run_command = run  # type: ignore[method-assign]
    (tmp_path / "run").mkdir()
    plan = supervisor._ravel_plan(
        {
            "impact": {
                "roots": ["mncs:fn:producer"],
                "change_kinds": ["cross_repository_contract"],
            }
        },
        tmp_path / "producer.mncs",
        tmp_path / "run",
    )
    assert plan["selection"]["selected_test_identities"] == []  # type: ignore[index]
    command = output_commands[0]
    assert "--cross-repository" in command
    assert command[command.index("--family-graph") + 1].endswith(
        "family/semantic-edges-v1.json"
    )
    assert command[command.index("--commons-root") + 1].endswith("/MNCS-Commons")
    assert command[command.index("--repository") + 1] == "mncs-forge"
