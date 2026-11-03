from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from mncs_forge.mncs_native import (
    NativeAssuranceInput,
    NativeExecutionAssuranceInput,
    NativeForgeAdapter,
)

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.native


def _initial(**changes: object) -> NativeAssuranceInput:
    value = NativeAssuranceInput(
        phase="InitialVerification",
        test_status="PASS",
        post_repair_test_status="UNKNOWN",
        debug_status="UNKNOWN",
        family_proof_status="UNKNOWN",
        test_present=True,
        failing_execution_present=False,
        post_repair_test_present=False,
        family_proof_present=False,
        plan_present=False,
        plan_current=True,
        repair_requested=False,
        repair_arguments_complete=False,
        repair_admissible=False,
        source_changed=False,
        rebound_plan_present=False,
        rebound_plan_current=True,
        proof_required=False,
        proof_sufficient=True,
        identity_binding_valid=True,
        evidence_fresh=True,
        provider_evidence_present=True,
    )
    return replace(value, **changes)


def _after(**changes: object) -> NativeAssuranceInput:
    return replace(
        _initial(
            phase="AfterRepair",
            test_status="FAIL",
            debug_status="PASS",
            failing_execution_present=True,
            repair_requested=True,
            repair_arguments_complete=True,
            repair_admissible=True,
            source_changed=True,
        ),
        **changes,
    )


@pytest.mark.parametrize(
    ("name", "facts", "status", "decision", "reason"),
    [
        ("initial-test-pass", _initial(), "PASS", "StopPass", "InitialPass"),
        (
            "test-fail-sufficient-diagnosis-no-repair",
            _initial(
                test_status="FAIL",
                failing_execution_present=True,
                debug_status="PASS",
            ),
            "FAIL",
            "StopFail",
            "RepairNotRequested",
        ),
        (
            "test-fail-insufficient-diagnosis",
            _initial(
                test_status="FAIL",
                failing_execution_present=True,
                debug_status="FAIL",
            ),
            "FAIL",
            "RequestDiagnosis",
            "DiagnosisInsufficient",
        ),
        (
            "repair-requested-inadmissible",
            _initial(
                test_status="FAIL",
                failing_execution_present=True,
                debug_status="PASS",
                repair_requested=True,
                repair_arguments_complete=True,
                repair_admissible=False,
            ),
            "FAIL",
            "StopFail",
            "RepairInadmissible",
        ),
        (
            "repair-accepted",
            _initial(
                test_status="FAIL",
                failing_execution_present=True,
                debug_status="PASS",
                repair_requested=True,
                repair_arguments_complete=True,
                repair_admissible=True,
            ),
            "FAIL",
            "RequestRepair",
            "None",
        ),
        (
            "stale-verification-plan",
            _initial(plan_present=True, plan_current=False),
            "UNKNOWN",
            "StopUnknown",
            "PlanStale",
        ),
        (
            "missing-rebound-plan",
            _after(plan_present=True, rebound_plan_present=False),
            "FAIL",
            "RequireReboundPlan",
            "ReboundPlanMissing",
        ),
        (
            "rebound-verification-pass",
            _after(
                plan_present=True,
                rebound_plan_present=True,
                rebound_plan_current=True,
                post_repair_test_present=True,
                post_repair_test_status="PASS",
            ),
            "PASS",
            "Finalize",
            "AssuranceComplete",
        ),
        (
            "rebound-verification-fail",
            _after(
                plan_present=True,
                rebound_plan_present=True,
                rebound_plan_current=True,
                post_repair_test_present=True,
                post_repair_test_status="FAIL",
            ),
            "FAIL",
            "StopFail",
            "PostRepairVerificationFail",
        ),
        (
            "rebound-verification-unknown",
            _after(
                plan_present=True,
                rebound_plan_present=True,
                rebound_plan_current=True,
                post_repair_test_present=True,
                post_repair_test_status="UNKNOWN",
            ),
            "FAIL",
            "StopUnknown",
            "PostRepairVerificationUnknown",
        ),
        (
            "actions-proof-pass",
            _initial(
                plan_present=True,
                proof_required=True,
                family_proof_present=True,
                family_proof_status="PASS",
                proof_sufficient=True,
            ),
            "PASS",
            "StopPass",
            "InitialPass",
        ),
        (
            "actions-proof-fail",
            _initial(
                plan_present=True,
                proof_required=True,
                family_proof_present=True,
                family_proof_status="FAIL",
            ),
            "FAIL",
            "StopFail",
            "FamilyProofFail",
        ),
        (
            "actions-proof-unknown",
            _initial(
                plan_present=True,
                proof_required=True,
                family_proof_present=True,
                family_proof_status="UNKNOWN",
            ),
            "UNKNOWN",
            "StopUnknown",
            "FamilyProofUnknown",
        ),
        (
            "required-proof-missing",
            _initial(plan_present=True, proof_required=True),
            "UNKNOWN",
            "RequireFamilyProof",
            "FamilyProofMissing",
        ),
        (
            "owner-artifact-identity-mismatch",
            _initial(identity_binding_valid=False),
            "UNKNOWN",
            "StopUnknown",
            "IdentityBindingInvalid",
        ),
        (
            "missing-provider-evidence",
            _initial(provider_evidence_present=False),
            "UNKNOWN",
            "StopUnknown",
            "ProviderEvidenceMissing",
        ),
    ],
)
def test_native_assurance_decisions_match_differential_vectors(
    name: str,
    facts: NativeAssuranceInput,
    status: str,
    decision: str,
    reason: str,
) -> None:
    del name  # The parameter names the differential fixture for failure output.
    result = NativeForgeAdapter(ROOT).assurance_loop(facts)
    assert (result.status, result.decision, result.stop_reason) == (status, decision, reason)


def _execution_assurance_input(**changes: object) -> NativeExecutionAssuranceInput:
    value = NativeExecutionAssuranceInput(
        local_process_runner=False,
        property_map_complete=True,
        complete_receipt_missing=False,
        receipt_complete=True,
        requested_runner_capability=True,
        runner_capability_state="Established",
        requested_filesystem_isolation=False,
        filesystem_isolation_state="NotEstablished",
        requested_network_isolation=False,
        network_isolation_state="NotEstablished",
        requested_containerization=False,
        containerization_state="NotEstablished",
        requested_same_operator_execution=False,
        same_operator_execution_state="NotEstablished",
    )
    return replace(value, **changes)


@pytest.mark.parametrize(
    ("name", "facts", "status", "conflict", "runner", "network", "receipt_incomplete"),
    [
        (
            "complete-established-request",
            _execution_assurance_input(),
            "PASS",
            "None",
            "Satisfied",
            "NotRequested",
            False,
        ),
        (
            "requested-property-not-established",
            _execution_assurance_input(
                runner_capability_state="NotEstablished",
            ),
            "UNKNOWN",
            "None",
            "NotEstablished",
            "NotRequested",
            False,
        ),
        (
            "requested-property-unobservable",
            _execution_assurance_input(
                requested_runner_capability=False,
                requested_network_isolation=True,
                network_isolation_state="Unknown",
            ),
            "UNKNOWN",
            "None",
            "NotRequested",
            "Unknown",
            False,
        ),
        (
            "local-runner-isolation-contradiction",
            _execution_assurance_input(
                local_process_runner=True,
                requested_runner_capability=False,
                requested_same_operator_execution=True,
                same_operator_execution_state="Established",
                network_isolation_state="Established",
            ),
            "FAIL",
            "NetworkIsolation",
            "NotRequested",
            "NotRequested",
            False,
        ),
        (
            "incomplete-receipt",
            _execution_assurance_input(receipt_complete=False),
            "UNKNOWN",
            "None",
            "Satisfied",
            "NotRequested",
            True,
        ),
        (
            "incomplete-property-map",
            _execution_assurance_input(
                property_map_complete=False,
                requested_runner_capability=False,
                requested_network_isolation=True,
                network_isolation_state="Unreadable",
            ),
            "UNKNOWN",
            "None",
            "NotRequested",
            "Unreadable",
            False,
        ),
        (
            "missing-complete-receipt-envelope",
            _execution_assurance_input(complete_receipt_missing=True),
            "UNKNOWN",
            "None",
            "Satisfied",
            "NotRequested",
            False,
        ),
    ],
)
def test_native_execution_assurance_matches_independent_vectors(
    name: str,
    facts: NativeExecutionAssuranceInput,
    status: str,
    conflict: str,
    runner: str,
    network: str,
    receipt_incomplete: bool,
) -> None:
    result = NativeForgeAdapter(ROOT).execution_assurance_assess(facts)
    assert result.valid, name
    assert result.status == status, name
    assert result.isolation_conflict == conflict, name
    assert result.runner_capability == runner, name
    assert result.network_isolation == network, name
    assert result.receipt_incomplete is receipt_incomplete, name
