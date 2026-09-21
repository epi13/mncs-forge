from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path

import pytest
from conftest import with_native_latency_allowance

from mncs_forge.config import ForgeConfig
from mncs_forge.engine import Forge
from mncs_forge.errors import ForgeError
from mncs_forge.ledger import Ledger
from mncs_forge.mncs_native import (
    NativeForgeAdapter,
    NativeLifecycleResult,
    NativeReadinessProjection,
    NativeReadinessRequirement,
)
from mncs_forge.records import RecordType, new_record
from mncs_forge.state_machine import ForgeStateMachine


def begin(forge: Forge, *, parent: str | None = None) -> dict[str, object]:
    return forge.epoch_begin(
        generator_identity="generator-v1",
        evaluator_identity="evaluator-v1",
        parent_epoch=parent,
    )


def register(forge: Forge, *, parent: str | None = None) -> dict[str, object]:
    return forge.candidate_register(
        changed_files=["candidate/main.py"],
        hypothesis="state-machine fixture",
        generator_identity="generator-v1",
        generator_config_identity="generator-config-v1",
        parent_candidate=parent,
    )


def ready_candidate(forge: Forge) -> dict[str, object]:
    begin(forge)
    candidate = register(forge)
    forge.development_checks_run(["pass-check"], str(candidate["candidate_id"]))
    return candidate


def selected_candidate(forge: Forge) -> dict[str, object]:
    candidate = ready_candidate(forge)
    forge.candidate_disposition(
        str(candidate["candidate_id"]), disposition="selected", reason="required evidence PASS"
    )
    return candidate


def freeze_candidate(forge: Forge, candidate: dict[str, object]) -> dict[str, object]:
    return forge.candidate_freeze(
        str(candidate["candidate_id"]),
        environment_identity="environment-v1",
        required_evidence_plan="evaluator/policy.json",
    )


def assert_code(code: str, operation: object) -> None:
    with pytest.raises(ForgeError) as issue:
        operation()  # type: ignore[operator]
    assert issue.value.code == code


def test_native_lifecycle_mismatch_fails_closed() -> None:
    class MismatchingNative:
        def lifecycle_preflight(
            self, stage: str, operation: str, evidence: str = "UNKNOWN"
        ) -> NativeLifecycleResult:
            return NativeLifecycleResult(stage, operation, "NoEpoch", "PASS", 0)

    machine = ForgeStateMachine(
        mode="development",
        history=(),
        current_candidate_identity="",
        current_authority_identities={},
        current_freeze_bindings={},
        selection_policy_identity="policy",
        required_evidence=(),
        native=MismatchingNative(),  # type: ignore[arg-type]
    )

    with pytest.raises(ForgeError, match="disagrees with the Forge transition") as issue:
        machine.authorize_epoch_begin(None)
    assert issue.value.code == "NATIVE_LIFECYCLE_MISMATCH"


def test_native_readiness_reconstructs_blockers_by_identity_not_return_order(
    config: ForgeConfig, project: Path
) -> None:
    raw = {**config.raw, "native": {"mode": "off"}}
    policy = project / "evaluator/policy.json"
    policy.write_text(
        '{"required":["zeta-check","alpha-check","middle-check"]}\n', encoding="utf-8"
    )
    workflows = {
        **config.workflows,
        "zeta-check": replace(config.workflows["pass-check"], name="zeta-check"),
        "alpha-check": replace(config.workflows["fail-check"], name="alpha-check"),
        "middle-check": replace(config.workflows["provider-unknown"], name="middle-check"),
    }
    configured = replace(config, raw=raw, workflows=workflows)
    forge = Forge(configured)
    begin(forge)
    candidate = register(forge)
    candidate_id = str(candidate["candidate_id"])
    forge.development_checks_run(["zeta-check", "alpha-check", "middle-check"], candidate_id)

    class PermutingNative:
        @staticmethod
        def readiness_projection(
            requirements: Mapping[str, Mapping[str, object]],
            *,
            candidate_present: bool,
            policy_valid: bool,
        ) -> NativeReadinessProjection:
            assert candidate_present is True
            assert policy_valid is True
            projected: list[NativeReadinessRequirement] = []
            for requirement, normalized in requirements.items():
                raw_records = normalized["records"]
                statuses = [str(record.get("status") or "UNKNOWN") for record in raw_records]
                failed = statuses.count("FAIL")
                unknown = statuses.count("UNKNOWN")
                passed = statuses.count("PASS")
                classification = (
                    "Missing"
                    if not statuses
                    else "Failed"
                    if failed
                    else "Unknown"
                    if unknown
                    else "Present"
                )
                projected.append(
                    NativeReadinessRequirement(
                        identity=NativeForgeAdapter.readiness_requirement_identity(requirement),
                        classification=classification,
                        pass_count=passed,
                        fail_count=failed,
                        unknown_count=unknown,
                        observed_count=len(statuses),
                        stale=False,
                        noncomparable=False,
                        valid=True,
                    )
                )
            failed_count = sum(item.classification == "Failed" for item in projected)
            unknown_count = sum(item.classification == "Unknown" for item in projected)
            missing_count = sum(item.classification == "Missing" for item in projected)
            ready = not (failed_count or unknown_count or missing_count)
            return NativeReadinessProjection(
                requirements=tuple(reversed(projected)),
                status="PASS" if ready else "FAIL" if failed_count else "UNKNOWN",
                reason="Ready" if ready else "Failed" if failed_count else "Unknown",
                present_count=sum(item.observed_count > 0 for item in projected),
                missing_count=missing_count,
                failed_count=failed_count,
                unknown_count=unknown_count,
                stale_count=0,
                noncomparable_count=0,
                ready=ready,
                valid=True,
            )

    base = forge._state_machine()
    machine = ForgeStateMachine(
        mode=forge.mode,
        history=forge.ledger.records(),
        current_candidate_identity=forge._current_candidate_identity(),
        current_authority_identities=forge._current_authority_identities(),
        current_freeze_bindings=forge._current_freeze_bindings(),
        selection_policy_identity=base.selection_policy_identity,
        required_evidence=base.required_evidence,
        selection_policy_error=base.selection_policy_error,
        evidence_freshness=base.evidence_freshness,
        evidence_comparability=base.evidence_comparability,
        evidence_environment_keys=base.evidence_environment_keys,
        evidence_environment_identities=base.evidence_environment_identities,
        evidence_policy_identities=base.evidence_policy_identities,
        native=PermutingNative(),  # type: ignore[arg-type]
    )

    assert machine.projection.evidence.failed == ("alpha-check",)
    assert machine.projection.evidence.unknown == ("middle-check",)
    assert machine.projection.evidence.present == (
        "alpha-check",
        "middle-check",
        "zeta-check",
    )
    assert machine.projection.evidence.blocker is not None
    assert machine.projection.evidence.blocker.message == (
        "required candidate evidence is FAIL: alpha-check"
    )


@pytest.mark.native
@pytest.mark.parametrize(
    ("scenario", "expected_blocker"),
    [
        (
            "missing",
            ("EVIDENCE_INCOMPLETE", "required candidate evidence is missing: pass-check"),
        ),
        ("pass", None),
        (
            "fail",
            ("EVIDENCE_FAILED", "required candidate evidence is FAIL: fail-check"),
        ),
        (
            "unknown",
            ("EVIDENCE_UNKNOWN", "required candidate evidence is UNKNOWN: provider-unknown"),
        ),
        ("stale", ("EVIDENCE_STALE", "required candidate evidence is stale: pass-check")),
        (
            "noncomparable",
            (
                "EVIDENCE_NOT_COMPARABLE",
                "required candidate evidence is not comparable: pass-check",
            ),
        ),
    ],
)
def test_native_evidence_readiness_matches_compatibility_surface(
    config: ForgeConfig,
    project: Path,
    scenario: str,
    expected_blocker: tuple[str, str] | None,
) -> None:
    requirement = {
        "fail": "fail-check",
        "unknown": "provider-unknown",
    }.get(scenario, "pass-check")
    (project / "evaluator/policy.json").write_text(
        f'{{"required":["{requirement}"]}}\n', encoding="utf-8"
    )
    forge = Forge(replace(config, raw={**config.raw, "native": {"mode": "off"}}))
    output_identity: str | None = None
    if scenario != "missing":
        begin(forge)
        candidate = register(forge)
        candidate_id = str(candidate["candidate_id"])
        forge.development_checks_run([requirement], candidate_id)
        if scenario in {"stale", "noncomparable"}:
            output_identity = str(forge._result_records(candidate_id)[-1]["output_identity"])

    compatibility = state_with_overrides(
        forge,
        **(
            {"freshness": {output_identity: "STALE"}}
            if scenario == "stale" and output_identity is not None
            else {"comparability": {output_identity: False}}
            if scenario == "noncomparable" and output_identity is not None
            else {}
        ),
    )
    native = state_with_overrides(
        forge,
        **(
            {"freshness": {output_identity: "STALE"}}
            if scenario == "stale" and output_identity is not None
            else {"comparability": {output_identity: False}}
            if scenario == "noncomparable" and output_identity is not None
            else {}
        ),
        native=NativeForgeAdapter(forge.config.root),
    )

    assert native.projection.evidence.to_dict() == compatibility.projection.evidence.to_dict()
    blocker = native.projection.evidence.blocker
    if expected_blocker is None:
        assert blocker is None
    else:
        assert blocker is not None
        assert (blocker.code, blocker.message) == expected_blocker


def state_with_overrides(
    forge: Forge,
    *,
    freshness: dict[str, str] | None = None,
    comparability: dict[str, bool | None] | None = None,
    mode: str | None = None,
    native: object | None = None,
) -> ForgeStateMachine:
    base = forge._state_machine()
    return ForgeStateMachine(
        mode=mode or forge.mode,
        history=forge.ledger.records(),
        current_candidate_identity=forge._current_candidate_identity(),
        current_authority_identities=forge._current_authority_identities(),
        current_freeze_bindings=forge._current_freeze_bindings(),
        selection_policy_identity=base.selection_policy_identity,
        required_evidence=base.required_evidence,
        selection_policy_error=base.selection_policy_error,
        evidence_freshness=freshness,
        evidence_comparability=comparability,
        native=native,  # type: ignore[arg-type]
    )


@pytest.mark.native
def test_valid_lifecycle_and_inspection_at_every_stage(config: ForgeConfig, project: Path) -> None:
    development = Forge(config)
    fresh = development.state_inspect()
    assert fresh["stage"] == "no_epoch"
    assert "begin_epoch" in fresh["allowed_operations"]
    assert fresh["blocked_operations"]["register_candidate"][0]["code"] == "NO_ACTIVE_EPOCH"

    epoch = begin(development)
    active = development.state_inspect()
    assert active["stage"] == "epoch_active"
    assert active["epoch"]["active_identity"] == epoch["epoch_id"]
    assert "register_candidate" in active["allowed_operations"]

    candidate = register(development)
    registered = development.state_inspect()
    assert registered["stage"] == "candidate_registered"
    assert registered["candidate"]["identity"] == candidate["candidate_id"]
    assert registered["evidence"]["missing"] == ["pass-check"]
    assert registered["blocked_operations"]["select_candidate"][0]["code"] == "EVIDENCE_INCOMPLETE"

    development.development_checks_run(["provider-unknown"])
    incomplete = development.state_inspect()
    assert incomplete["stage"] == "candidate_registered"
    assert incomplete["evidence"]["ready"] is False

    development.development_checks_run(["pass-check"])
    ready = development.state_inspect()
    assert ready["stage"] == "candidate_ready"
    assert ready["evidence"]["status"] == "PASS"
    assert "select_candidate" in ready["allowed_operations"]

    disposition = development.candidate_disposition(
        str(candidate["candidate_id"]), disposition="selected", reason="ready"
    )
    selected = development.state_inspect()
    assert selected["stage"] == "candidate_selected"
    assert selected["disposition"]["identity"] == disposition["disposition_id"]
    assert "freeze_candidate" in selected["allowed_operations"]

    freeze = freeze_candidate(development, candidate)
    frozen = development.state_inspect()
    assert frozen["stage"] == "candidate_frozen"
    assert frozen["freeze"]["identity"] == freeze["freeze_id"]
    assert frozen["blocked_operations"]["register_candidate"][0]["code"] == "EPOCH_FROZEN"

    evaluator = Forge(config, mode="evaluator")
    evaluation = evaluator.final_evaluation_run(["evaluator-pass"])
    evaluated = evaluator.state_inspect()
    assert evaluation["aggregate_status"] == "PASS"
    assert evaluated["stage"] == "evaluation_complete"
    assert evaluated["evaluation"]["status"] == "complete"
    assert evaluated["reconciliation"]["status"] == "derived_on_request"
    assert evaluated["reconciliation"]["persistent"] is False
    reconciliation = evaluator.evidence_reconcile(str(candidate["candidate_id"]))
    assert reconciliation["candidate_identity"] == candidate["candidate_id"]

    bundle_workflow = replace(
        config.workflows["evaluator-pass"],
        name="evaluator-bundle",
        category="mncs_bundle_validation",
    )
    bundle_forge = Forge(
        replace(config, workflows={**config.workflows, "evaluator-bundle": bundle_workflow}),
        mode="evaluator",
    )
    bundle_forge.bundle_build("evaluator-bundle", str(candidate["candidate_id"]))
    bundled = bundle_forge.state_inspect()
    assert bundled["stage"] == "bundle_complete"
    assert bundled["bundle"]["status"] == "complete"
    assert (project / "candidate/main.py").read_text(encoding="utf-8") == "VALUE = 1\n"


def test_candidate_registration_requires_active_epoch(config: ForgeConfig) -> None:
    assert_code("NO_ACTIVE_EPOCH", lambda: register(Forge(config)))


def test_epoch_successor_requires_current_parent(config: ForgeConfig) -> None:
    forge = Forge(config)
    first = begin(forge)
    assert_code("EPOCH_PARENT_REQUIRED", lambda: begin(forge))
    second = begin(forge, parent=str(first["epoch_id"]))
    assert_code("EPOCH_SUPERSEDED", lambda: begin(forge, parent=str(first["epoch_id"])))
    third = begin(forge, parent=str(second["epoch_id"]))
    assert third["parent_epoch"] == second["epoch_id"]


def test_candidate_parent_must_be_current_and_same_epoch(
    config: ForgeConfig, project: Path
) -> None:
    forge = Forge(config)
    first_epoch = begin(forge)
    parent = register(forge)
    second_epoch = begin(forge, parent=str(first_epoch["epoch_id"]))
    assert_code(
        "CANDIDATE_LINEAGE_CONFLICT",
        lambda: register(forge, parent=str(parent["candidate_id"])),
    )
    state = forge._state_machine()
    assert_code(
        "EPOCH_SUPERSEDED",
        lambda: state.authorize_candidate_register(
            parent_candidate=None,
            proposed_identity=forge._current_candidate_identity(),
            epoch_identity=str(first_epoch["epoch_id"]),
        ),
    )
    (project / "candidate/main.py").write_text("VALUE = 2\n", encoding="utf-8")
    child = register(forge)
    (project / "candidate/main.py").write_text("VALUE = 3\n", encoding="utf-8")
    assert_code("CANDIDATE_PARENT_REQUIRED", lambda: register(forge))
    assert_code("CANDIDATE_PARENT_INVALID", lambda: register(forge, parent="missing"))
    descendant = register(forge, parent=str(child["candidate_id"]))
    assert descendant["source_epoch"] == second_epoch["epoch_id"]


def test_self_parent_and_historical_cycle_are_rejected_or_ambiguous(
    config: ForgeConfig,
) -> None:
    # This intentionally constructs the historical JSONL oracle: the test
    # injects a deliberately malformed duplicate history entry, which the
    # canonical Store binding correctly refuses as conflicting content.
    from mncs_forge.record_store import LocalRecordStore

    legacy_store = LocalRecordStore(config.state_dir)
    forge = Forge(config, record_store=legacy_store)
    begin(forge)
    candidate = register(forge)
    assert_code(
        "CANDIDATE_PARENT_INVALID",
        lambda: forge._state_machine().authorize_candidate_register(
            parent_candidate=str(candidate["candidate_id"]),
            proposed_identity=str(candidate["candidate_id"]),
        ),
    )
    raw = candidate.copy()
    raw.pop("record_type")
    raw.pop("schema_version")
    raw["parent_candidate"] = raw["candidate_id"]
    cyclic = new_record(RecordType.CANDIDATE, raw)
    legacy_store.ledger.append("candidate", cyclic)
    inspected = forge.state_inspect()
    assert inspected["stage"] == "ambiguous_history"
    assert any(item["code"] == "CANDIDATE_LINEAGE_CONFLICT" for item in inspected["limitations"])


def test_candidate_refresh_is_noop_when_current(config: ForgeConfig) -> None:
    forge = Forge(config)
    begin(forge)
    candidate = register(forge)
    refreshed = forge.candidate_refresh(
        hypothesis="no content change",
        generator_identity="generator-v1",
        generator_config_identity="generator-config-v1",
        changed_files=["candidate/main.py"],
    )
    assert refreshed["refreshed"] is False
    assert refreshed["candidate_identity"] == candidate["candidate_id"]
    assert refreshed["previous_candidate_identity"] == candidate["candidate_id"]


def test_candidate_refresh_after_modification_preserves_prior_evidence(
    config: ForgeConfig, project: Path
) -> None:
    forge = Forge(config)
    begin(forge)
    first = register(forge)
    first_result = forge.development_checks_run(["pass-check"], str(first["candidate_id"]))
    assert first_result["candidate_identity"] == first["candidate_id"]
    (project / "candidate/main.py").write_text("VALUE = 2\n", encoding="utf-8")
    assert_code(
        "STALE_CANDIDATE",
        lambda: forge.development_checks_run(["pass-check"], str(first["candidate_id"])),
    )
    refreshed = forge.candidate_refresh(
        hypothesis="value changed after baseline",
        generator_identity="generator-v1",
        generator_config_identity="generator-config-v1",
        changed_files=["candidate/main.py"],
    )
    assert refreshed["refreshed"] is True
    assert refreshed["previous_candidate_identity"] == first["candidate_id"]
    assert refreshed["candidate_identity"] != first["candidate_id"]
    second = forge.development_checks_run(["pass-check"], str(refreshed["candidate_identity"]))
    assert second["candidate_identity"] == refreshed["candidate_identity"]
    inspected = forge.state_inspect()
    assert inspected["candidate"]["identity"] == refreshed["candidate_identity"]
    assert inspected["candidate"]["freshness"] == "CURRENT"
    assert first["candidate_id"] in inspected["candidate"]["lineage"]
    prior_results = [
        entry.payload
        for entry in forge.ledger.records()
        if entry.payload.get("candidate_identity") == first["candidate_id"]
        and entry.payload.get("workflow") == "pass-check"
    ]
    assert prior_results
    assert all(item.get("candidate_identity") == first["candidate_id"] for item in prior_results)


def test_stale_candidate_cannot_continue_as_current(config: ForgeConfig, project: Path) -> None:
    forge = Forge(config)
    begin(forge)
    candidate = register(forge)
    (project / "candidate/main.py").write_text("VALUE = 2\n", encoding="utf-8")
    assert_code(
        "STALE_CANDIDATE",
        lambda: forge.candidate_disposition(
            str(candidate["candidate_id"]), disposition="rejected", reason="stale"
        ),
    )


@pytest.mark.parametrize(
    ("workflow", "code"),
    [
        (None, "EVIDENCE_INCOMPLETE"),
        ("provider-unknown", "EVIDENCE_INCOMPLETE"),
        ("fail-check", "EVIDENCE_INCOMPLETE"),
    ],
)
def test_selection_requires_declared_evidence(
    config: ForgeConfig, workflow: str | None, code: str
) -> None:
    forge = Forge(config)
    begin(forge)
    candidate = register(forge)
    if workflow is not None:
        forge.development_checks_run([workflow])
    assert_code(
        code,
        lambda: forge.candidate_disposition(
            str(candidate["candidate_id"]), disposition="selected", reason="premature"
        ),
    )


@pytest.mark.parametrize(
    ("replacement", "code"),
    [("provider-unknown", "EVIDENCE_UNKNOWN"), ("fail-check", "EVIDENCE_FAILED")],
)
def test_selection_rejects_unknown_and_fail_required_evidence(
    config: ForgeConfig, project: Path, replacement: str, code: str
) -> None:
    (project / "evaluator/policy.json").write_text(
        f'{{"required":["{replacement}"]}}\n', encoding="utf-8"
    )
    forge = Forge(config)
    begin(forge)
    candidate = register(forge)
    forge.development_checks_run([replacement])
    assert forge.state_inspect()["stage"] == "evidence_incomplete"
    assert_code(
        code,
        lambda: forge.candidate_disposition(
            str(candidate["candidate_id"]), disposition="selected", reason="not PASS"
        ),
    )


def test_required_evidence_fail_dominates_later_pass(config: ForgeConfig) -> None:
    forge = Forge(config)
    begin(forge)
    candidate = register(forge)
    failing_required = replace(config.workflows["fail-check"], name="pass-check")
    changed = Forge(replace(config, workflows={**config.workflows, "pass-check": failing_required}))
    changed.development_checks_run(["pass-check"], str(candidate["candidate_id"]))
    forge.development_checks_run(["pass-check"], str(candidate["candidate_id"]))
    assert_code(
        "EVIDENCE_FAILED",
        lambda: forge.candidate_disposition(
            str(candidate["candidate_id"]), disposition="selected", reason="FAIL dominates"
        ),
    )


def test_selection_rejects_noncomparable_evidence(config: ForgeConfig) -> None:
    forge = Forge(config)
    candidate = ready_candidate(forge)
    output = forge._result_records(str(candidate["candidate_id"]))[-1]
    state = state_with_overrides(forge, comparability={str(output["output_identity"]): False})
    assert_code(
        "EVIDENCE_NOT_COMPARABLE",
        lambda: state.authorize_candidate_disposition(str(candidate["candidate_id"]), "selected"),
    )


def test_selection_rejects_stale_evidence(config: ForgeConfig) -> None:
    forge = Forge(config)
    candidate = ready_candidate(forge)
    output = forge._result_records(str(candidate["candidate_id"]))[-1]
    state = state_with_overrides(forge, freshness={str(output["output_identity"]): "STALE"})
    assert_code(
        "EVIDENCE_STALE",
        lambda: state.authorize_candidate_disposition(str(candidate["candidate_id"]), "selected"),
    )


def test_selection_rejects_authority_envelope_drift(config: ForgeConfig, project: Path) -> None:
    forge = Forge(config)
    candidate = ready_candidate(forge)
    (project / "contract/contract.md").write_text("drifted contract\n", encoding="utf-8")
    assert_code(
        "EVIDENCE_NOT_COMPARABLE",
        lambda: forge.candidate_disposition(
            str(candidate["candidate_id"]), disposition="selected", reason="stale authority"
        ),
    )


def test_selection_rejects_workflow_environment_envelope_drift(
    config: ForgeConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("LC_ALL", raising=False)
    forge = Forge(config)
    candidate = ready_candidate(forge)
    changed_workflow = replace(config.workflows["pass-check"], environment={"LC_ALL": "C"})
    changed = Forge(replace(config, workflows={**config.workflows, "pass-check": changed_workflow}))
    assert_code(
        "EVIDENCE_NOT_COMPARABLE",
        lambda: changed.candidate_disposition(
            str(candidate["candidate_id"]), disposition="selected", reason="environment drift"
        ),
    )


def test_selection_rejects_partially_complete_required_envelope(
    config: ForgeConfig, project: Path
) -> None:
    (project / "evaluator/policy.json").write_text(
        '{"required":["pass-check","fail-check"]}\n', encoding="utf-8"
    )
    forge = Forge(config)
    begin(forge)
    candidate = register(forge)
    forge.development_checks_run(["pass-check"])
    assert_code(
        "EVIDENCE_INCOMPLETE",
        lambda: forge.candidate_disposition(
            str(candidate["candidate_id"]), disposition="selected", reason="partial"
        ),
    )


def test_project_pass_is_not_candidate_evidence(config: ForgeConfig) -> None:
    forge = Forge(config)
    begin(forge)
    candidate = register(forge)
    forge.development_checks_run(["project-check"])
    inspected = forge.state_inspect()
    assert inspected["evidence"]["missing"] == ["pass-check"]
    reconciled = forge.evidence_reconcile()
    assert reconciled["categories"] == {}
    assert_code(
        "EVIDENCE_INCOMPLETE",
        lambda: forge.candidate_disposition(
            str(candidate["candidate_id"]), disposition="selected", reason="project PASS"
        ),
    )


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("selected", "rejected"),
        ("rejected", "selected"),
        ("selected", "selected"),
        ("rejected", "rejected"),
    ],
)
def test_candidate_disposition_is_terminal(config: ForgeConfig, first: str, second: str) -> None:
    forge = Forge(config)
    candidate = ready_candidate(forge)
    forge.candidate_disposition(str(candidate["candidate_id"]), disposition=first, reason="first")
    assert_code(
        "CANDIDATE_ALREADY_DISPOSED",
        lambda: forge.candidate_disposition(
            str(candidate["candidate_id"]), disposition=second, reason="second"
        ),
    )


def test_rejected_candidate_state_is_terminal_and_inspectable(config: ForgeConfig) -> None:
    forge = Forge(config)
    candidate = ready_candidate(forge)
    forge.candidate_disposition(
        str(candidate["candidate_id"]), disposition="rejected", reason="not promoted"
    )
    inspected = forge.state_inspect()
    assert inspected["stage"] == "candidate_rejected"
    assert inspected["disposition"]["status"] == "rejected"
    assert inspected["blocked_operations"]["freeze_candidate"][0]["code"] == ("CANDIDATE_REJECTED")


def test_freeze_requires_current_selected_candidate(config: ForgeConfig) -> None:
    undisposed = Forge(config)
    candidate = ready_candidate(undisposed)
    assert_code("FREEZE_NOT_SELECTED", lambda: freeze_candidate(undisposed, candidate))

    undisposed.candidate_disposition(
        str(candidate["candidate_id"]), disposition="rejected", reason="reject"
    )
    assert_code("CANDIDATE_REJECTED", lambda: freeze_candidate(undisposed, candidate))


def test_frozen_epoch_blocks_new_candidate_development_work(config: ForgeConfig) -> None:
    forge = Forge(config)
    candidate = selected_candidate(forge)
    freeze_candidate(forge, candidate)
    assert_code("EPOCH_FROZEN", lambda: forge.development_checks_run(["pass-check"]))


def test_freeze_revalidates_candidate_authority_selection_and_plan(
    config: ForgeConfig, project: Path
) -> None:
    forge = Forge(config)
    candidate = selected_candidate(forge)
    (project / "contract/contract.md").write_text("authority drift\n", encoding="utf-8")
    assert_code("FREEZE_AUTHORITY_STALE", lambda: freeze_candidate(forge, candidate))


def test_freeze_rejects_stale_candidate(config: ForgeConfig, project: Path) -> None:
    forge = Forge(config)
    candidate = selected_candidate(forge)
    (project / "candidate/main.py").write_text("VALUE = 2\n", encoding="utf-8")
    assert_code("STALE_CANDIDATE", lambda: freeze_candidate(forge, candidate))


def test_freeze_rejects_invalid_evidence_plan(config: ForgeConfig) -> None:
    forge = Forge(config)
    candidate = selected_candidate(forge)
    assert_code(
        "FREEZE_EVIDENCE_PLAN_INVALID",
        lambda: forge.candidate_freeze(
            str(candidate["candidate_id"]),
            environment_identity="environment-v1",
            required_evidence_plan="contract/contract.md",
        ),
    )
    assert_code(
        "FREEZE_EVIDENCE_PLAN_INVALID",
        lambda: forge.candidate_freeze(
            str(candidate["candidate_id"]),
            environment_identity="environment-v1",
            required_evidence_plan="evidence/missing-plan.json",
        ),
    )


def test_freeze_requires_evidence_named_by_valid_plan(config: ForgeConfig, project: Path) -> None:
    forge = Forge(config)
    candidate = selected_candidate(forge)
    (project / "evidence/freeze-plan.json").write_text(
        '{"required":["missing-freeze-check"]}\n', encoding="utf-8"
    )
    assert_code(
        "EVIDENCE_INCOMPLETE",
        lambda: forge.candidate_freeze(
            str(candidate["candidate_id"]),
            environment_identity="environment-v1",
            required_evidence_plan="evidence/freeze-plan.json",
        ),
    )


def test_freeze_rejects_selection_after_policy_change(config: ForgeConfig, project: Path) -> None:
    forge = Forge(config)
    candidate = selected_candidate(forge)
    (project / "evaluator/policy.json").write_text(
        '{"required":["pass-check"],"revision":2}\n', encoding="utf-8"
    )
    assert forge.state_inspect()["blocked_operations"]["freeze_candidate"][0]["code"] == (
        "FREEZE_AUTHORITY_STALE"
    )
    assert_code("FREEZE_AUTHORITY_STALE", lambda: freeze_candidate(forge, candidate))


def test_freeze_revalidates_selected_evidence(config: ForgeConfig) -> None:
    config = with_native_latency_allowance(config)
    forge = Forge(config)
    candidate = selected_candidate(forge)
    failing_required = replace(config.workflows["fail-check"], name="pass-check")
    changed = Forge(replace(config, workflows={**config.workflows, "pass-check": failing_required}))
    changed.development_checks_run(["pass-check"], str(candidate["candidate_id"]))
    assert_code("EVIDENCE_FAILED", lambda: freeze_candidate(forge, candidate))


def test_evaluator_entry_rules(config: ForgeConfig, project: Path) -> None:
    assert_code(
        "NO_FREEZE",
        lambda: Forge(config, mode="evaluator").final_evaluation_run(["evaluator-pass"]),
    )
    assert_code("MODE_FORBIDDEN", lambda: Forge(config).final_evaluation_run(["evaluator-pass"]))

    development = Forge(config)
    candidate = selected_candidate(development)
    freeze_candidate(development, candidate)
    evaluator = Forge(config, mode="evaluator")
    assert_code(
        "EVALUATOR_CANDIDATE_MISMATCH",
        lambda: evaluator._state_machine(observe_epoch_authority=False).authorize_evaluator_entry(
            "wrong-candidate"
        ),
    )
    (project / "candidate/main.py").write_text("VALUE = 2\n", encoding="utf-8")
    assert_code("FREEZE_DRIFT", lambda: evaluator.final_evaluation_run(["evaluator-pass"]))


def test_evaluator_rejects_authority_drift(config: ForgeConfig, project: Path) -> None:
    development = Forge(config)
    candidate = selected_candidate(development)
    freeze_candidate(development, candidate)
    (project / "reference/reference.py").write_text("VALUE = 9\n", encoding="utf-8")
    assert_code(
        "FREEZE_DRIFT",
        lambda: Forge(config, mode="evaluator").final_evaluation_run(["evaluator-pass"]),
    )


def test_evaluator_rejects_required_evidence_plan_drift(config: ForgeConfig, project: Path) -> None:
    development = Forge(config)
    candidate = selected_candidate(development)
    plan = project / "evidence/freeze-plan.json"
    plan.write_text('{"required":["pass-check"]}\n', encoding="utf-8")
    development.candidate_freeze(
        str(candidate["candidate_id"]),
        environment_identity="environment-v1",
        required_evidence_plan="evidence/freeze-plan.json",
    )
    plan.write_text('{"required":["pass-check"],"post_freeze_change":true}\n', encoding="utf-8")
    assert_code(
        "FREEZE_DRIFT",
        lambda: Forge(config, mode="evaluator").final_evaluation_run(["evaluator-pass"]),
    )


def test_mode_separation_blocks_cross_mode_mutations(config: ForgeConfig) -> None:
    evaluator = Forge(config, mode="evaluator")
    assert_code("MODE_FORBIDDEN", lambda: begin(evaluator))
    assert_code("MODE_FORBIDDEN", lambda: register(evaluator))


def test_terminal_result_transition_requires_one_matching_recorded_action(
    config: ForgeConfig,
) -> None:
    forge = Forge(config)
    candidate = ready_candidate(forge)
    result = forge.verifier_run("verify-pass", changed_paths=["candidate/main.py"], scope="file")
    state = forge._state_machine(observe_epoch_authority=False)
    assert_code(
        "ACTION_NOT_RECORDED",
        lambda: state.authorize_terminal_result(
            action_id="missing",
            candidate_id=str(candidate["candidate_id"]),
            freeze_id=None,
            mode="development",
        ),
    )
    assert_code(
        "ACTION_ALREADY_TERMINATED",
        lambda: state.authorize_terminal_result(
            action_id=str(result["action_id"]),
            candidate_id=str(candidate["candidate_id"]),
            freeze_id=None,
            mode="development",
        ),
    )
    history_without_result = tuple(
        entry for entry in forge.ledger.records() if entry.kind != "verifier_result"
    )
    base = forge._state_machine(observe_epoch_authority=False)
    unterminated = ForgeStateMachine(
        mode="development",
        history=history_without_result,
        current_candidate_identity=forge._current_candidate_identity(),
        current_authority_identities={},
        current_freeze_bindings=forge._current_freeze_bindings(),
        selection_policy_identity=base.selection_policy_identity,
        required_evidence=base.required_evidence,
        selection_policy_error=base.selection_policy_error,
    )
    assert_code(
        "ACTION_BINDING_MISMATCH",
        lambda: unterminated.authorize_terminal_result(
            action_id=str(result["action_id"]),
            candidate_id="wrong-candidate",
            freeze_id=None,
            mode="development",
        ),
    )
    assert_code(
        "ACTION_BINDING_MISMATCH",
        lambda: unterminated.authorize_terminal_result(
            action_id=str(result["action_id"]),
            candidate_id=str(candidate["candidate_id"]),
            freeze_id="wrong-freeze",
            mode="development",
        ),
    )


def test_project_scoped_check_remains_available_without_epoch(config: ForgeConfig) -> None:
    forge = Forge(config)
    result = forge.development_checks_run(["project-check"])
    assert result["aggregate_status"] == "PASS"
    inspected = forge.state_inspect()
    assert "run_project_check" in inspected["allowed_operations"]


def test_legacy_history_projects_without_rewriting_or_prospective_rejection() -> None:
    state_dir = Path(__file__).parent / "fixtures/legacy-0.1/complete-state"
    history = Ledger(state_dir).records()
    epoch = next(entry.payload for entry in history if entry.kind == "epoch")
    candidate = [entry.payload for entry in history if entry.kind == "candidate"][-1]
    disposition = [entry.payload for entry in history if entry.kind == "disposition"][-1]
    freeze = next(entry.payload for entry in history if entry.kind == "freeze")
    freeze_bindings = {
        name: str(freeze[name])
        for name in (
            "candidate_identity",
            "contract_identity",
            "reference_identity",
            "evaluator_identity",
            "acceptance_policy_identity",
            "protected_identity",
        )
    }
    machine = ForgeStateMachine(
        mode="evaluator",
        history=history,
        current_candidate_identity=str(candidate["candidate_id"]),
        current_authority_identities=dict(epoch["authority_identities"]),  # type: ignore[arg-type]
        current_freeze_bindings=freeze_bindings,
        selection_policy_identity=str(disposition["selection_policy_identity"]),
        required_evidence=["pass-check"],
    )
    inspected = machine.inspect()
    assert inspected["stage"] == "bundle_complete"
    assert candidate.schema_version == "0.1-unversioned"
    assert Ledger(state_dir).verify()["ok"] is True
