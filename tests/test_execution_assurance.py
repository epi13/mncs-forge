from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import pytest

from mncs_forge.config import ForgeConfig
from mncs_forge.engine import Forge
from mncs_forge.errors import ForgeError
from mncs_forge.records import RecordType, new_record


def begin(forge: Forge) -> None:
    forge.epoch_begin(generator_identity="generator-v1", evaluator_identity="evaluator-v1")


def register(forge: Forge) -> dict[str, object]:
    return forge.candidate_register(
        changed_files=["candidate/main.py"],
        hypothesis="execution assurance",
        generator_identity="generator-v1",
        generator_config_identity="generator-config-v1",
    )


def first_binding_id(forge: Forge) -> str:
    bindings = forge.ledger.records("execution_receipt_binding")
    assert bindings
    return str(bindings[-1].payload["binding_id"])


def test_functional_pass_cannot_launder_isolation_assurance(
    config: ForgeConfig,
) -> None:
    forge = Forge(config)
    begin(forge)
    candidate = register(forge)
    result = forge.development_checks_run(["pass-check"], str(candidate["candidate_id"]))
    assert result["aggregate_status"] == "PASS"

    assessment = forge.execution_assurance_assess(
        binding_id=first_binding_id(forge),
        requested_properties=["network_isolation", "filesystem_isolation"],
    )

    assert result["aggregate_status"] == "PASS"
    assert assessment["assurance_status"] == "UNKNOWN"
    assert sorted(cast(list[str], list(assessment["unmet_properties"]))) == [
        "filesystem_isolation",
        "network_isolation",
    ]
    reasons = cast(list[str], assessment["reasons"])
    assert any("not established" in reason for reason in reasons)


def test_established_local_property_can_pass_assessment(config: ForgeConfig) -> None:
    forge = Forge(config)
    begin(forge)
    candidate = register(forge)
    forge.development_checks_run(["pass-check"], str(candidate["candidate_id"]))

    assessment = forge.execution_assurance_assess(
        binding_id=first_binding_id(forge),
        requested_properties=["same_operator_execution"],
    )

    assert assessment["assurance_status"] == "PASS"
    assert list(assessment["unmet_properties"]) == []
    assert "same-operator" not in str(assessment["note"]).lower()


def test_property_outside_vocabulary_is_rejected_without_persistence(
    config: ForgeConfig,
) -> None:
    forge = Forge(config)
    begin(forge)
    candidate = register(forge)
    forge.development_checks_run(["pass-check"], str(candidate["candidate_id"]))
    binding_id = first_binding_id(forge)

    with pytest.raises(ForgeError) as excinfo:
        forge.execution_assurance_assess(
            binding_id=binding_id,
            requested_properties=["protected_custody"],
        )
    assert excinfo.value.code == "ASSURANCE_REQUEST"
    assert forge.ledger.records("execution_assurance") == []


@pytest.mark.parametrize(
    ("requested_properties", "message"),
    [
        ([], "requested execution-assurance properties cannot be empty"),
        (
            ["network_isolation", "network_isolation"],
            "requested execution-assurance properties must be unique",
        ),
        (
            ["unknown_a", "unknown_b"],
            "requested properties are outside the declared vocabulary",
        ),
        (
            ["unknown_a", "unknown_a"],
            "requested execution-assurance properties must be unique",
        ),
    ],
)
def test_native_request_policy_rejects_invalid_requests_before_binding_lookup(
    config: ForgeConfig, requested_properties: list[str], message: str
) -> None:
    forge = Forge(config)

    with pytest.raises(ForgeError) as excinfo:
        forge.execution_assurance_assess(
            binding_id="execution-receipt-binding:does-not-exist",
            requested_properties=requested_properties,
        )

    assert excinfo.value.code == "ASSURANCE_REQUEST"
    assert excinfo.value.message == message
    assert forge.ledger.records("execution_assurance") == []


def test_native_request_transport_bound_is_reported_as_assurance_request(
    config: ForgeConfig,
) -> None:
    forge = Forge(config)

    with pytest.raises(ForgeError) as excinfo:
        forge.execution_assurance_assess(
            binding_id="execution-receipt-binding:does-not-exist",
            requested_properties=[f"unknown_{index}" for index in range(9)],
        )

    assert excinfo.value.code == "ASSURANCE_REQUEST"
    assert excinfo.value.message == "assurance properties exceed 8 items"
    assert forge.ledger.records("execution_assurance") == []


def test_incomplete_execution_keeps_assurance_unknown(config: ForgeConfig) -> None:
    forge = Forge(config)
    begin(forge)
    register(forge)
    with pytest.raises(ForgeError):
        forge.development_checks_run(["timeout-check"])
    binding_id = first_binding_id(forge)

    assessment = forge.execution_assurance_assess(
        binding_id=binding_id,
        requested_properties=["same_operator_execution"],
    )

    assert assessment["assurance_status"] == "UNKNOWN"
    reasons = cast(list[str], assessment["reasons"])
    assert any("incomplete" in reason for reason in reasons)


def test_unknown_binding_identity_is_rejected(config: ForgeConfig) -> None:
    forge = Forge(config)
    with pytest.raises(ForgeError) as excinfo:
        forge.execution_assurance_assess(
            binding_id="execution-receipt-binding:does-not-exist",
            requested_properties=["network_isolation"],
        )
    assert excinfo.value.code == "RECORD_NOT_FOUND"


def test_forged_isolation_claim_against_local_runner_fails_closed(
    config: ForgeConfig,
) -> None:
    forge = Forge(config)
    begin(forge)
    candidate = register(forge)
    forge.development_checks_run(["pass-check"], str(candidate["candidate_id"]))
    action = forge.ledger.records("workflow_action")[0].payload
    forged = new_record(
        RecordType.EXECUTION_RECEIPT_BINDING,
        {
            "project_identity": config.project_identity,
            "epoch_identity": None,
            "candidate_identity": candidate["candidate_id"],
            "action_kind": "workflow_action",
            "action_identity": action["action_id"],
            "result_identity": None,
            "request_identity": None,
            "workflow_or_verifier": "pass-check",
            "runner_identity": "runner.local-process-v1",
            "runner_kind": "local-process",
            "runner_version": "1",
            "worker_identity": None,
            "host_identity": None,
            "os_family": "linux",
            "architecture": "x86_64",
            "executable_identity": None,
            "image_identity": None,
            "environment_identity": "environment-forged",
            "execution_scope": "local",
            "termination_category": "completed",
            "receipt_schema_version": None,
            "receipt_identity": None,
            "receipt_completeness": "incomplete",
            "status": "UNKNOWN",
            "established_properties": {
                "execution_completed": "unknown",
                "local_result_validity": "unknown",
                "runner_capability": "established",
                "filesystem_isolation": "established",
                "network_isolation": "established",
                "containerization": "established",
                "same_operator_execution": "established",
                "external_anchoring": "not-established",
                "witnessing": "not-established",
                "protected_custody": "not-established",
                "evaluator_independence": "not-established",
                "governance_certification": "not-established",
            },
            "mncs_receipt": None,
            "recorded_at": "2026-01-01T00:00:00.000000Z",
        },
    )
    forge.record_store.commit("execution-receipt-bindings", "execution_receipt_binding", forged)

    assessment = forge.execution_assurance_assess(
        binding_id=str(forged["binding_id"]),
        requested_properties=["network_isolation", "containerization"],
    )

    assert assessment["assurance_status"] == "FAIL"
    reasons = cast(list[str], assessment["reasons"])
    assert any("cannot enforce it" in reason for reason in reasons)


def test_conflicting_assessments_are_retained_side_by_side(config: ForgeConfig) -> None:
    forge = Forge(config)
    begin(forge)
    candidate = register(forge)
    forge.development_checks_run(["pass-check"], str(candidate["candidate_id"]))
    binding_id = first_binding_id(forge)

    strict = forge.execution_assurance_assess(
        binding_id=binding_id,
        requested_properties=["network_isolation", "filesystem_isolation", "containerization"],
    )
    narrow = forge.execution_assurance_assess(
        binding_id=binding_id,
        requested_properties=["same_operator_execution"],
    )
    listed = forge.execution_assurance_list()

    assert strict["assurance_status"] == "UNKNOWN"
    assert narrow["assurance_status"] == "PASS"
    assert listed["count"] == 2
    statuses = {item["assessment_id"]: item["assurance_status"] for item in listed["assessments"]}
    assert len(statuses) == 2


def test_verifier_receipt_can_be_assessed(config: ForgeConfig) -> None:
    forge = Forge(config)
    begin(forge)
    register(forge)
    forge.verifier_run("verify-pass", changed_paths=["candidate/main.py"], scope="file")

    assessment = forge.execution_assurance_assess(
        binding_id=first_binding_id(forge),
        requested_properties=["network_isolation"],
    )

    assert assessment["action_kind"] == "verifier_action"
    assert assessment["assurance_status"] == "UNKNOWN"


ROOT = Path(__file__).parents[1]


def _example(name: str) -> dict[str, object]:
    value = json.loads((ROOT / "examples/forge-cell" / name).read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def test_cell_document_validate_operation(config: ForgeConfig) -> None:
    forge = Forge(config)
    ok = forge.cell_document_validate("policy", _example("policy.json"))
    bad = forge.cell_document_validate("policy", {"policy_id": 1})

    assert ok["ok"] is True
    assert bad["ok"] is False and bad["code"] == "DOCUMENT_INVALID"


def test_cell_execution_assess_operation_separates_result_from_assurance(
    config: ForgeConfig,
) -> None:
    forge = Forge(config)
    record = _example("execution-record.json")

    assessment = forge.cell_execution_assess(
        _example("policy.json"), record, expected_nonce="reference-nonce-0000000000000001"
    )

    assert record["result"] == "PASS"
    assert assessment["assurance_status"] == "UNKNOWN"
    assert assessment["unmet"] == ["process-isolated"]

    with pytest.raises(ForgeError) as excinfo:
        forge.cell_execution_assess({"policy_id": 1}, record)
    assert excinfo.value.code == "CELL_DOCUMENT_INVALID"
