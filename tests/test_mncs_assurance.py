from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from mncs_forge.mncs_native import NativeAssuranceInput, NativeForgeAdapter

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
