from __future__ import annotations

from mncs_forge.continuous import ContinuousSupervisor


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
