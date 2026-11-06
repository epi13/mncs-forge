from __future__ import annotations

import shutil
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from mncs_forge.application.support import aggregate_status
from mncs_forge.config import ForgeConfig, load_config
from mncs_forge.engine import Forge
from mncs_forge.errors import ForgeError

STATUS_VALUES = st.sampled_from(("PASS", "UNKNOWN", "FAIL"))
LIFECYCLE_ACTIONS = st.sampled_from(
    ("begin", "register", "pass", "unknown", "fail", "select", "reject", "freeze", "evaluate")
)


@given(statuses=st.lists(STATUS_VALUES, min_size=0, max_size=8))
@settings(max_examples=40, deadline=None, derandomize=True, database=None)
def test_status_aggregation_preserves_failure_and_unknown_precedence(
    statuses: list[str],
) -> None:
    expected = (
        "FAIL"
        if "FAIL" in statuses
        else "UNKNOWN"
        if "UNKNOWN" in statuses
        else "PASS"
        if statuses
        else "UNKNOWN"
    )

    assert aggregate_status(statuses) == expected
    assert aggregate_status(reversed(statuses)) == expected


def test_status_aggregation_delegates_recognized_values_to_native_kernel() -> None:
    from mncs_forge.application.support import aggregate_status as aggregate_status_with_native

    observed: list[list[str]] = []

    def native_aggregate(statuses: list[str]) -> str:
        observed.append(list(statuses))
        return "UNKNOWN" if "UNKNOWN" in statuses else "PASS"

    assert aggregate_status_with_native(
        ["PASS", "ignored", "UNKNOWN"], native_status_aggregate=native_aggregate
    ) == "UNKNOWN"
    assert observed == [["PASS", "UNKNOWN"]]
    assert aggregate_status_with_native(
        [], native_status_aggregate=lambda _statuses: "REVIEW_REQUIRED"
    ) == "UNKNOWN"

    with pytest.raises(ForgeError, match="native status aggregate is not recognized"):
        aggregate_status_with_native(
            ["PASS"], native_status_aggregate=lambda _statuses: "REVIEW_REQUIRED"
        )


def _current_identity(observation: dict[str, object], section: str) -> str | None:
    value = observation.get(section)
    if not isinstance(value, dict):
        return None
    identity = value.get("identity", value.get("active_identity"))
    return identity if isinstance(identity, str) else None


def _assert_history_invariants(forge: Forge) -> None:
    history = forge.ledger.records()
    dispositions: dict[str, list[str]] = {}
    freezes: dict[str, int] = {}
    terminal_results: dict[str, int] = {}
    for entry in history:
        payload = entry.payload
        if entry.kind == "disposition":
            candidate_id = payload.get("candidate_identity")
            disposition = payload.get("disposition")
            if isinstance(candidate_id, str) and isinstance(disposition, str):
                dispositions.setdefault(candidate_id, []).append(disposition)
        elif entry.kind == "freeze":
            candidate_id = payload.get("candidate_identity")
            if isinstance(candidate_id, str):
                freezes[candidate_id] = freezes.get(candidate_id, 0) + 1
        elif entry.kind == "verifier_result":
            action_id = payload.get("action_id")
            if isinstance(action_id, str):
                terminal_results[action_id] = terminal_results.get(action_id, 0) + 1

    for values in dispositions.values():
        assert len(values) == 1
        assert values[0] in {"selected", "rejected"}
    assert all(count == 1 for count in freezes.values())
    assert all(count == 1 for count in terminal_results.values())

    observation = forge.state_inspect()
    disposition = observation.get("disposition")
    freeze = observation.get("freeze")
    if isinstance(freeze, dict) and freeze.get("identity") is not None:
        assert isinstance(disposition, dict)
        assert disposition.get("disposition") == "selected"
        assert freeze.get("candidate_identity") == _current_identity(observation, "candidate")


@given(actions=st.lists(LIFECYCLE_ACTIONS, min_size=1, max_size=10))
@settings(
    max_examples=20,
    deadline=None,
    derandomize=True,
    database=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_generated_lifecycle_sequences_never_create_contradictory_terminal_history(
    actions: list[str], config: ForgeConfig
) -> None:
    # Each Hypothesis example gets an isolated state directory while retaining the fixture's
    # declared project and authority paths. The test observes the real Forge state machine;
    # it does not implement a second transition model.
    with TemporaryDirectory(prefix="forge-task9a-") as directory:
        isolated_root = Path(directory)
        shutil.copytree(config.root, isolated_root, dirs_exist_ok=True)
        isolated = load_config(isolated_root / "mncs-forge.toml")
        development = Forge(isolated)
        evaluator = Forge(isolated, mode="evaluator")

        for action in actions:
            observation = development.state_inspect()
            candidate_id = _current_identity(observation, "candidate")
            epoch_id = _current_identity(observation, "epoch")
            try:
                if action == "begin":
                    development.epoch_begin(
                        generator_identity="generator-v1",
                        evaluator_identity="evaluator-v1",
                        parent_epoch=epoch_id,
                    )
                elif action == "register":
                    development.candidate_register(
                        changed_files=["candidate/main.py"],
                        hypothesis="property-generated lifecycle action",
                        generator_identity="generator-v1",
                        generator_config_identity="generator-config-v1",
                        parent_candidate=candidate_id,
                    )
                elif action in {"pass", "unknown", "fail"}:
                    workflow = {
                        "pass": "pass-check",
                        "unknown": "provider-unknown",
                        "fail": "fail-check",
                    }[action]
                    development.development_checks_run([workflow], candidate_id)
                elif action in {"select", "reject"} and candidate_id is not None:
                    development.candidate_disposition(
                        candidate_id,
                        disposition="selected" if action == "select" else "rejected",
                        reason="property-generated terminal action",
                    )
                elif action == "freeze" and candidate_id is not None:
                    development.candidate_freeze(
                        candidate_id,
                        environment_identity="environment-v1",
                        required_evidence_plan="evaluator/policy.json",
                    )
                elif action == "evaluate":
                    evaluator.final_evaluation_run(["evaluator-pass"])
            except ForgeError:
                # Invalid ordering is the input under test. Any successful operation must still
                # leave the durable history and projected state within their terminal invariants.
                pass

            _assert_history_invariants(development)


def test_same_history_projects_deterministically(config: ForgeConfig) -> None:
    first = Forge(config)
    first.epoch_begin(generator_identity="generator-v1", evaluator_identity="evaluator-v1")
    first.candidate_register(
        changed_files=["candidate/main.py"],
        hypothesis="deterministic projection",
        generator_identity="generator-v1",
        generator_config_identity="generator-config-v1",
    )
    first.development_checks_run(["pass-check"])

    second = Forge(config)
    assert second.state_inspect() == first.state_inspect()
    assert [entry.to_json() for entry in second.ledger.records()] == [
        entry.to_json() for entry in first.ledger.records()
    ]
