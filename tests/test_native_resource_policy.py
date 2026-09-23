from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from mncs_forge.mncs_native import (
    NativeForgeAdapter,
    NativeResourceAdmissionDecision,
    NativeResourceBudgetDecision,
    NativeResourceOutcomeDecision,
    NativeVerificationTransition,
)

ROOT = Path(__file__).resolve().parents[1]
MIB = 1024 * 1024
GIB = 1024 * MIB


@pytest.fixture(scope="module")
def native() -> Iterator[NativeForgeAdapter]:
    adapter = NativeForgeAdapter(ROOT)
    try:
        yield adapter
    finally:
        adapter.close()


# The expected values were differentially checked against the previous Python
# selector before that semantic implementation was removed.
@pytest.mark.parametrize(
    ("name", "settings", "host_memory", "cgroup_memory", "expected"),
    [
        (
            "default_8g",
            {},
            8 * GIB,
            None,
            ("Selected", 644245094, 1288490188, 0, 64, 1, 600.0, 8 * GIB),
        ),
        (
            "cgroup_2g",
            {},
            8 * GIB,
            2 * GIB,
            ("Selected", 161061273, 322122547, 0, 64, 1, 600.0, 2 * GIB),
        ),
        (
            "fraction_05",
            {"memory_fraction": 0.05},
            4 * GIB,
            None,
            ("Selected", 107374182, 214748364, 0, 64, 1, 600.0, 4 * GIB),
        ),
        (
            "fraction_25_cap_128m",
            {"memory_fraction": 0.25, "memory_cap_bytes": 128 * MIB},
            8 * GIB,
            None,
            ("Selected", 67108864, 134217728, 0, 64, 1, 600.0, 8 * GIB),
        ),
        (
            "explicit_128m",
            {"memory_max_bytes": 128 * MIB},
            8 * GIB,
            None,
            ("Selected", 67108864, 134217728, 0, 64, 1, 600.0, 8 * GIB),
        ),
        ("unavailable_unknown", {}, None, None, ("Unavailable", 0, 0, 0, 0, 0, 0.0, 0)),
        ("below_capacity", {}, 256 * MIB, None, ("Unavailable", 0, 0, 0, 0, 0, 0.0, 0)),
        (
            "tiny_limits",
            {
                "memory_max_bytes": 128 * MIB,
                "memory_cap_bytes": 128 * MIB,
                "pids_limit": 8,
                "runtime_max_seconds": 4,
            },
            2 * GIB,
            None,
            ("Selected", 67108864, 134217728, 0, 8, 1, 4.0, 2 * GIB),
        ),
        ("bad_fraction", {"memory_fraction": 0.26}, 8 * GIB, None, ("InvalidPolicy", 0, 0, 0, 0, 0, 0.0, 0)),
        ("bad_tasks", {"pids_limit": 4097}, 8 * GIB, None, ("InvalidPolicy", 0, 0, 0, 0, 0, 0.0, 0)),
        (
            "bad_concurrency",
            {"concurrency_limit": 2},
            8 * GIB,
            None,
            ("InvalidPolicy", 0, 0, 0, 0, 0, 0.0, 0),
        ),
        (
            "bad_runtime",
            {"runtime_max_seconds": 601},
            8 * GIB,
            None,
            ("InvalidPolicy", 0, 0, 0, 0, 0, 0.0, 0),
        ),
        (
            "ignored_explicit_type",
            {"memory_max_bytes": "128 MiB"},
            8 * GIB,
            None,
            ("Selected", 644245094, 1288490188, 0, 64, 1, 600.0, 8 * GIB),
        ),
        (
            "cgroup_only",
            {},
            None,
            2 * GIB,
            ("Selected", 161061273, 322122547, 0, 64, 1, 600.0, 2 * GIB),
        ),
        ("explicit_too_small", {"memory_max_bytes": 64 * MIB}, 8 * GIB, None, ("Unavailable", 0, 0, 0, 0, 0, 0.0, 0)),
        ("negative_cgroup", {}, 8 * GIB, -1, ("Unavailable", 0, 0, 0, 0, 0, 0.0, 0)),
        (
            "ignored_explicit_bool",
            {"memory_max_bytes": True},
            8 * GIB,
            None,
            ("Selected", 644245094, 1288490188, 0, 64, 1, 600.0, 8 * GIB),
        ),
        ("bad_cap", {"memory_cap_bytes": 127 * MIB}, 8 * GIB, None, ("InvalidPolicy", 0, 0, 0, 0, 0, 0.0, 0)),
    ],
    ids=lambda value: value if isinstance(value, str) else None,
)
def test_native_budget_decision_corpus(
    native: NativeForgeAdapter,
    name: str,
    settings: dict[str, object],
    host_memory: int | None,
    cgroup_memory: int | None,
    expected: tuple[str, int, int, int, int, int, float, int],
) -> None:
    decision: NativeResourceBudgetDecision = native.resource_budget_select(
        {
            "host_memory_total_bytes": host_memory,
            "cgroup_memory_max_bytes": cgroup_memory,
        },
        settings,
    )
    actual = (
        decision.status,
        decision.memory_high_bytes,
        decision.memory_max_bytes,
        decision.memory_swap_max_bytes,
        decision.tasks_max,
        decision.concurrency_max,
        decision.runtime_max_seconds,
        decision.effective_host_memory_bytes,
    )
    assert actual == expected, name


def test_process_resource_reproducer_exposes_current_native_gap(
    native: NativeForgeAdapter,
) -> None:
    reproducer = ROOT / "src" / "mncs_forge" / "resources" / "pressure-reproducers" / "process-resource-handle.mncs"
    result = native.invoke(
        ["declaration-inventory", str(reproducer)],
        output_cap=1_000_000,
    )
    diagnostics = (result.stdout + b"\n" + result.stderr).decode("utf-8", errors="replace")
    assert result.returncode != 0
    assert "MNP216" in diagnostics
    assert "process_run takes exactly one typed process-request argument" in diagnostics


def test_native_budget_identity_uses_structured_digest(native: NativeForgeAdapter) -> None:
    observation = {"host_memory_total_bytes": 8 * GIB, "cgroup_memory_max_bytes": None}
    default = native.resource_budget_select(observation, {})
    repeated = native.resource_budget_select(observation, {})
    smaller = native.resource_budget_select(observation, {"memory_fraction": 0.1})

    identity = native.resource_budget_identity(default)
    assert len(identity) == 64
    assert all(character in "0123456789abcdef" for character in identity)
    assert native.resource_budget_identity(repeated) == identity
    assert native.resource_budget_identity(smaller) != identity


@pytest.mark.parametrize(
    ("name", "observation", "policy", "expected"),
    [
        (
            "contained_with_headroom",
            {
                "containment_available": True,
                "has_budget": True,
                "available_memory_bytes": 1024 * MIB,
                "active_tasks": 0,
                "execution_slot_available": True,
                "requested_runtime_seconds": 30.0,
            },
            {
                "containment_required": True,
                "memory_max_bytes": 256 * MIB,
                "concurrency_max": 1,
                "runtime_max_seconds": 12.0,
            },
            ("Admit", "None", 512 * MIB, 12.0),
        ),
        (
            "headroom_uses_twice_budget",
            {
                "containment_available": True,
                "has_budget": True,
                "available_memory_bytes": 513 * MIB,
                "active_tasks": 0,
                "execution_slot_available": True,
                "requested_runtime_seconds": 30.0,
            },
            {
                "containment_required": True,
                "memory_max_bytes": 257 * MIB,
                "concurrency_max": 1,
                "runtime_max_seconds": 12.0,
            },
            ("Defer", "LowHostHeadroom", 514 * MIB, 0.0),
        ),
        (
            "unknown_host_availability_preserves_admission",
            {
                "containment_available": True,
                "has_budget": True,
                "available_memory_bytes": None,
                "active_tasks": 0,
                "execution_slot_available": True,
                "requested_runtime_seconds": 30.0,
            },
            {
                "containment_required": True,
                "memory_max_bytes": 256 * MIB,
                "concurrency_max": 1,
                "runtime_max_seconds": 12.0,
            },
            ("Admit", "None", 512 * MIB, 12.0),
        ),
        (
            "active_process_tree",
            {
                "containment_available": True,
                "has_budget": True,
                "available_memory_bytes": 2 * GIB,
                "active_tasks": 1,
                "execution_slot_available": True,
                "requested_runtime_seconds": 30.0,
            },
            {
                "containment_required": True,
                "memory_max_bytes": 256 * MIB,
                "concurrency_max": 1,
                "runtime_max_seconds": 12.0,
            },
            ("Defer", "ActiveProcessTree", 0, 0.0),
        ),
        (
            "occupied_execution_slot",
            {
                "containment_available": True,
                "has_budget": True,
                "available_memory_bytes": 2 * GIB,
                "active_tasks": None,
                "execution_slot_available": False,
                "requested_runtime_seconds": 30.0,
            },
            {
                "containment_required": True,
                "memory_max_bytes": 256 * MIB,
                "concurrency_max": 1,
                "runtime_max_seconds": 12.0,
            },
            ("Defer", "ExecutionSlotOccupied", 0, 0.0),
        ),
        (
            "unknown_process_count",
            {
                "containment_available": True,
                "has_budget": True,
                "available_memory_bytes": 2 * GIB,
                "active_tasks": None,
                "execution_slot_available": True,
                "requested_runtime_seconds": 30.0,
            },
            {
                "containment_required": True,
                "memory_max_bytes": 256 * MIB,
                "concurrency_max": 1,
                "runtime_max_seconds": 12.0,
            },
            ("Unavailable", "ProcessCountUnknown", 0, 0.0),
        ),
        (
            "required_containment_missing",
            {
                "containment_available": False,
                "has_budget": True,
                "available_memory_bytes": 2 * GIB,
                "active_tasks": 0,
                "execution_slot_available": True,
                "requested_runtime_seconds": 30.0,
            },
            {
                "containment_required": True,
                "memory_max_bytes": 256 * MIB,
                "concurrency_max": 1,
                "runtime_max_seconds": 12.0,
            },
            ("Unavailable", "ContainmentUnavailable", 0, 0.0),
        ),
        (
            "optional_uncontained_execution",
            {
                "containment_available": False,
                "has_budget": False,
                "available_memory_bytes": None,
                "active_tasks": None,
                "execution_slot_available": False,
                "requested_runtime_seconds": 30.0,
            },
            {
                "containment_required": False,
                "memory_max_bytes": 0,
                "concurrency_max": 1,
                "runtime_max_seconds": 12.0,
            },
            ("Uncontained", "None", 0, 12.0),
        ),
        (
            "invalid_deadline",
            {
                "containment_available": True,
                "has_budget": True,
                "available_memory_bytes": 2 * GIB,
                "active_tasks": 0,
                "execution_slot_available": True,
                "requested_runtime_seconds": 0.0,
            },
            {
                "containment_required": True,
                "memory_max_bytes": 256 * MIB,
                "concurrency_max": 1,
                "runtime_max_seconds": 12.0,
            },
            ("InvalidInput", "InvalidRuntime", 0, 0.0),
        ),
    ],
    ids=lambda value: value if isinstance(value, str) else None,
)
def test_native_admission_decision_corpus(
    native: NativeForgeAdapter,
    name: str,
    observation: dict[str, object],
    policy: dict[str, object],
    expected: tuple[str, str, int, float],
) -> None:
    decision: NativeResourceAdmissionDecision = native.resource_admission(observation, policy)
    assert (
        decision.status,
        decision.reason,
        decision.required_headroom_bytes,
        decision.runtime_seconds,
    ) == expected, name
    assert decision.deferred is (expected[0] in {"Defer", "Unavailable"}), name


@pytest.mark.parametrize(
    ("name", "host_available", "cgroup_available", "expected"),
    [
        ("host_headroom_is_tightest", 511 * MIB, 2 * GIB, ("Defer", "LowHostHeadroom")),
        ("cgroup_headroom_is_tightest", 2 * GIB, 513 * MIB, ("Defer", "LowHostHeadroom")),
        ("both_headrooms_sufficient", 2 * GIB, 514 * MIB, ("Admit", "None")),
    ],
)
def test_native_admission_chooses_from_raw_host_and_cgroup_headroom(
    native: NativeForgeAdapter,
    name: str,
    host_available: int,
    cgroup_available: int,
    expected: tuple[str, str],
) -> None:
    decision = native.resource_admission(
        {
            "containment_available": True,
            "has_budget": True,
            "host_available_memory_bytes": host_available,
            "cgroup_available_memory_bytes": cgroup_available,
            "active_tasks": 0,
            "execution_slot_available": True,
            "requested_runtime_seconds": 30.0,
        },
        {
            "containment_required": True,
            "memory_max_bytes": 257 * MIB,
            "concurrency_max": 1,
            "runtime_max_seconds": 30.0,
        },
    )
    assert (decision.status, decision.reason) == expected, name


@pytest.mark.parametrize(
    ("name", "updates", "expected"),
    [
        ("pass", {"exit_status": 0, "systemd_result": "success"}, ("Pass", "None", False, False)),
        ("semantic_fail", {"exit_status": 5, "systemd_result": "exit-code"}, ("Fail", "None", False, False)),
        (
            "memory_limit",
            {"exit_status": -9, "memory_max_events": 1},
            ("ResourceLimit", "Memory", True, True),
        ),
        (
            "oom_result",
            {"exit_status": -9, "systemd_result": "oom-kill"},
            ("ResourceLimit", "Memory", True, True),
        ),
        (
            "pid_limit",
            {"exit_status": 1, "process_limit_events": 1},
            ("ResourceLimit", "ProcessCount", True, True),
        ),
        (
            "timeout",
            {"exit_status": -9, "timed_out": True, "systemd_result": "timeout"},
            ("Timeout", "WallTime", True, True),
        ),
        (
            "output_limit",
            {"exit_status": 1, "output_limited": True},
            ("OutputLimit", "Output", True, True),
        ),
        (
            "memory_pressure",
            {"exit_status": 0, "memory_high_events": 2},
            ("ResourcePressure", "Memory", False, True),
        ),
        (
            "cleanup_failure",
            {"exit_status": 0, "cleanup_succeeded": False},
            ("CleanupFailure", "None", False, True),
        ),
        (
            "cancelled",
            {"exit_status": -9, "cancellation_requested": True},
            ("Cancelled", "None", False, False),
        ),
        (
            "superseded",
            {"exit_status": -9, "cancellation_requested": True, "superseded": True},
            ("Stale", "None", False, False),
        ),
        ("unknown_exit", {"systemd_result": "unknown"}, ("Unknown", "None", False, True)),
        (
            "wrapper_success_when_systemd_reaped_unit",
            {"systemd_result": "unknown", "wrapper_returncode": 0},
            ("Pass", "None", False, False),
        ),
        (
            "wrapper_nonzero_without_inner_status_stays_unknown",
            {"systemd_result": "unknown", "wrapper_returncode": 1},
            ("Unknown", "None", False, True),
        ),
        (
            "inner_wrapper_status_conflict_stays_unknown",
            {"exit_status": 0, "wrapper_returncode": 1},
            ("Unknown", "None", False, True),
        ),
        (
            "oom_counters_override_reaped_unit_status",
            {"systemd_result": "unknown", "wrapper_returncode": 1, "memory_oom_kill_events": 1},
            ("ResourceLimit", "Memory", True, True),
        ),
    ],
    ids=lambda value: value if isinstance(value, str) else None,
)
def test_native_outcome_classifies_raw_resource_facts(
    native: NativeForgeAdapter,
    name: str,
    updates: dict[str, object],
    expected: tuple[str, str, bool, bool],
) -> None:
    observation: dict[str, object] = {
        "systemd_result": "success",
        "cleanup_known": True,
        "cleanup_succeeded": True,
    }
    observation.update(updates)
    decision: NativeResourceOutcomeDecision = native.resource_outcome(observation)
    assert (
        decision.status,
        decision.metric,
        decision.resource_exhausted,
        decision.deferred,
    ) == expected, name
    expected_exit = None
    if decision.status in {"Pass", "Fail"}:
        raw_exit = updates.get("exit_status")
        if isinstance(raw_exit, int) and not isinstance(raw_exit, bool):
            expected_exit = raw_exit
        elif decision.status == "Pass" and updates.get("wrapper_returncode") == 0:
            expected_exit = 0
    assert decision.has_execution_exit_status is (expected_exit is not None), name
    if expected_exit is not None:
        assert decision.execution_exit_status == expected_exit, name


@pytest.mark.parametrize(
    ("name", "updates", "expected"),
    [
        (
            "run_current_obligation",
            {},
            ("Run", "NotRun", False, False, False, False),
        ),
        (
            "defer_behind_pressure",
            {"resource_gate_closed": True, "queue_remaining": 2},
            ("RemainPendingAndDeferRemaining", "Unknown", True, True, False, False),
        ),
        (
            "resource_pending_and_defer_remaining",
            {
                "has_outcome": True,
                "resource_outcome_observed": True,
                "outcome": "ResourceLimit",
                "evidence_status": "Unknown",
                "queue_remaining": 1,
            },
            ("RemainPendingAndDeferRemaining", "Unknown", True, True, False, False),
        ),
        (
            "pressure_remains_pending",
            {
                "has_outcome": True,
                "resource_outcome_observed": True,
                "outcome": "ResourcePressure",
                "evidence_status": "Unknown",
            },
            ("RemainPending", "Unknown", True, False, False, False),
        ),
        (
            "semantic_pass_resolves",
            {
                "has_outcome": True,
                "resource_outcome_observed": True,
                "outcome": "Pass",
                "evidence_status": "Pass",
            },
            ("Resolve", "Pass", False, False, False, False),
        ),
        (
            "pending_capacity_escalates",
            {
                "has_outcome": True,
                "resource_outcome_observed": True,
                "outcome": "Unknown",
                "evidence_status": "Unknown",
                "pending_count": 2,
                "pending_capacity": 2,
            },
            ("EscalateUnknown", "Unknown", False, False, False, False),
        ),
        (
            "stale_running_work_requests_cancel",
            {"current_generation": 8, "work_generation": 7, "in_flight": True},
            ("CancelStale", "Unknown", False, False, True, True),
        ),
        (
            "stale_completed_work_is_discarded",
            {
                "current_generation": 8,
                "work_generation": 7,
                "in_flight": False,
                "has_outcome": True,
                "resource_outcome_observed": True,
                "outcome": "Pass",
                "evidence_status": "Pass",
            },
            ("DiscardStale", "Unknown", False, False, False, False),
        ),
        (
            "semantic_pass_without_resource_observation_resolves",
            {
                "has_outcome": True,
                "resource_outcome_observed": False,
                "outcome": "Unknown",
                "evidence_status": "Pass",
            },
            ("Resolve", "Pass", False, False, False, False),
        ),
        (
            "unknown_resource_observation_blocks_semantic_pass",
            {
                "has_outcome": True,
                "resource_outcome_observed": True,
                "outcome": "Unknown",
                "evidence_status": "Pass",
            },
            ("RemainPending", "Unknown", True, False, False, False),
        ),
    ],
    ids=lambda value: value if isinstance(value, str) else None,
)
def test_native_continuous_transition_corpus(
    native: NativeForgeAdapter,
    name: str,
    updates: dict[str, object],
    expected: tuple[str, str, bool, bool, bool, bool],
) -> None:
    state: dict[str, object] = {
        "current_generation": 7,
        "work_generation": 7,
        "source_identity_matches": True,
        "verification_required": True,
        "in_flight": False,
        "has_outcome": False,
        "resource_outcome_observed": False,
        "outcome": "Unknown",
        "evidence_status": "NotRun",
        "pending_exists": False,
        "pending_count": 0,
        "pending_capacity": 64,
        "queue_remaining": 0,
        "resource_gate_closed": False,
    }
    state.update(updates)
    decision: NativeVerificationTransition = native.verification_resource_transition(state)
    assert (
        decision.disposition,
        decision.evidence_status,
        decision.retain_current_pending,
        decision.defer_remaining,
        decision.cancel_owned_work,
        decision.cleanup_required,
    ) == expected, name


@pytest.mark.parametrize(
    ("statuses", "verification_required", "expected", "stale", "escalation"),
    [
        ([], True, "UNKNOWN", False, True),
        ([], False, "PASS", False, False),
        (["PASS"], True, "PASS", False, False),
        (["PASS", "PASS"], True, "PASS", False, False),
        (["PASS", "UNKNOWN"], True, "UNKNOWN", False, True),
        (["UNKNOWN", "FAIL", "PASS"], True, "FAIL", False, True),
        (["PASS"] * 16, True, "PASS", False, False),
        (["PASS"] * 15 + ["FAIL"], True, "FAIL", False, True),
        (["STALE"], True, "UNKNOWN", True, True),
        (["PASS", "STALE"], False, "UNKNOWN", True, True),
        (["FAIL", "STALE"], False, "FAIL", True, True),
    ],
)
def test_native_continuous_status_decision(
    native: NativeForgeAdapter,
    statuses: list[str],
    verification_required: bool,
    expected: str,
    stale: bool,
    escalation: bool,
) -> None:
    decision = native.verification_status_decide(
        statuses,
        verification_required=verification_required,
    )
    assert decision.status == expected
    assert decision.stale_observed is stale
    assert decision.escalation_required is escalation


@pytest.mark.parametrize(
    ("statuses", "unresolved_count", "expected", "escalation"),
    [
        (["PASS"], 1, "UNKNOWN", True),
        (["FAIL"], 1, "FAIL", True),
        ([], 1, "UNKNOWN", True),
        (["PASS"], 0, "PASS", False),
    ],
)
def test_native_continuous_status_includes_deferred_obligations(
    native: NativeForgeAdapter,
    statuses: list[str],
    unresolved_count: int,
    expected: str,
    escalation: bool,
) -> None:
    decision = native.verification_status_decide(
        statuses,
        verification_required=True,
        unresolved_count=unresolved_count,
    )
    assert decision.status == expected
    assert decision.escalation_required is escalation


@pytest.mark.parametrize(
    ("count", "expected"),
    [(0, (0, 0)), (4, (4, 0)), (16, (16, 0)), (19, (16, 3))],
)
def test_native_verifier_queue_admission(
    native: NativeForgeAdapter, count: int, expected: tuple[int, int]
) -> None:
    assert native.verification_queue_admit(count) == expected


@pytest.mark.parametrize(
    ("verifier_cost", "maximum_cost"),
    [
        ("low", "low"),
        ("low", "high"),
        ("medium", "low"),
        ("medium", "medium"),
        ("high", "medium"),
        ("high", "high"),
        ("unknown", "high"),
        ("low", "unknown"),
        (None, "high"),
    ],
)
def test_native_verification_cost_admission_matches_previous_oracle(
    native: NativeForgeAdapter, verifier_cost: str | None, maximum_cost: str
) -> None:
    oracle_order = {"low": 0, "medium": 1, "high": 2}
    oracle = oracle_order.get(verifier_cost, 99) <= oracle_order.get(maximum_cost, -1)
    assert native.verification_cost_admit(verifier_cost, maximum_cost) is oracle
