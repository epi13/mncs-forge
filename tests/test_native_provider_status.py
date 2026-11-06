from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from mncs_forge.engine import Forge
from mncs_forge.mncs_native import NativeForgeAdapter

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def native_adapter():
    adapter = NativeForgeAdapter(ROOT)
    yield adapter
    adapter.close()


@pytest.mark.parametrize(
    ("statuses", "expected"),
    [
        ([], "UNKNOWN"),
        (["PASS"], "PASS"),
        (["PASS", "UNKNOWN"], "UNKNOWN"),
        (["FAIL", "PASS", "UNKNOWN"], "FAIL"),
        (["ignored", "PASS"], "PASS"),
        (["PASS"] * 16 + ["UNKNOWN"], "UNKNOWN"),
        (["PASS"] * 31 + ["FAIL"] + ["UNKNOWN"] * 7, "FAIL"),
        (["PASS"] * 37, "PASS"),
    ],
)
def test_native_status_aggregation_matches_independent_expectation(
    native_adapter: NativeForgeAdapter, statuses: list[str], expected: str
) -> None:
    assert native_adapter.status_aggregate(statuses) == expected


def test_native_provider_capability_policy_streams_unbounded_lists(
    native_adapter: NativeForgeAdapter,
) -> None:
    capability = "capability/" + ("long-name-" * 12)
    decoys = [f"unrelated-capability-{index}" for index in range(35)]
    result = native_adapter.provider_capability_projection(
        capability,
        declared_capabilities=[*decoys, capability],
        probed_capabilities=[*decoys[:19], capability, *decoys[19:]],
        unsupported_constructs=decoys,
        provider_status="PASS",
        provider_required=False,
        provider_available=True,
    )
    assert result.candidate is True
    assert result.established is True
    assert result.required_blocker is False
    assert result.optional_unavailable is False
    assert result.valid is True


def test_native_provider_policy_keeps_late_unsupported_fact_authoritative(
    native_adapter: NativeForgeAdapter,
) -> None:
    capability = "bounded-structural"
    unsupported = [f"unrelated-{index}" for index in range(24)] + [capability]
    result = native_adapter.provider_capability_projection(
        capability,
        declared_capabilities=[capability],
        probed_capabilities=[capability],
        unsupported_constructs=unsupported,
        provider_status="PASS",
        provider_required=True,
        provider_available=True,
    )
    assert result.candidate is True
    assert result.established is False
    assert result.required_blocker is False
    assert result.optional_unavailable is False


def test_native_provider_policy_classifies_required_and_optional_provider_state(
    native_adapter: NativeForgeAdapter,
) -> None:
    required = native_adapter.provider_capability_projection(
        None,
        declared_capabilities=(),
        probed_capabilities=(),
        unsupported_constructs=(),
        provider_status="UNKNOWN",
        provider_required=True,
        provider_available=False,
    )
    optional = native_adapter.provider_capability_projection(
        None,
        declared_capabilities=(),
        probed_capabilities=(),
        unsupported_constructs=(),
        provider_status="UNKNOWN",
        provider_required=False,
        provider_available=False,
    )
    assert required.required_blocker is True
    assert required.optional_unavailable is False
    assert optional.required_blocker is False
    assert optional.optional_unavailable is True


def _continuous_consumer_inputs(**changes: bool) -> dict[str, bool]:
    return {
        "durable_status_present": False,
        "cursor_recovery_required": False,
        "owner_retry_required": False,
        "supervisor_present": False,
        "test_supervisor_without_status_path": False,
        "test_supervisor_has_stream_identity": False,
        "stream_identity_matches": False,
        "event_cursor_is_integer": False,
        "event_cursor_matches": False,
        **changes,
    }


@pytest.mark.parametrize(
    ("evidence", "expected"),
    [
        (_continuous_consumer_inputs(), "starting"),
        (
            _continuous_consumer_inputs(
                test_supervisor_without_status_path=True,
                test_supervisor_has_stream_identity=True,
            ),
            "ready",
        ),
        (
            _continuous_consumer_inputs(test_supervisor_without_status_path=True),
            "starting",
        ),
        (
            _continuous_consumer_inputs(
                durable_status_present=True,
                cursor_recovery_required=True,
            ),
            "blocked",
        ),
        (
            _continuous_consumer_inputs(
                durable_status_present=True,
                owner_retry_required=True,
            ),
            "retry_required",
        ),
        (
            _continuous_consumer_inputs(
                durable_status_present=True,
                supervisor_present=True,
                stream_identity_matches=True,
                event_cursor_is_integer=True,
                event_cursor_matches=True,
            ),
            "ready",
        ),
        (
            _continuous_consumer_inputs(
                durable_status_present=True,
                supervisor_present=True,
            ),
            "reconciling",
        ),
        (
            _continuous_consumer_inputs(
                durable_status_present=True,
                supervisor_present=True,
                stream_identity_matches=True,
                event_cursor_is_integer=False,
                event_cursor_matches=True,
            ),
            "reconciling",
        ),
        (
            _continuous_consumer_inputs(
                durable_status_present=True,
                cursor_recovery_required=True,
                owner_retry_required=True,
            ),
            "blocked",
        ),
        (
            _continuous_consumer_inputs(durable_status_present=True),
            "unknown",
        ),
    ],
)
def test_native_continuous_consumer_projection_matches_independent_expectation(
    native_adapter: NativeForgeAdapter,
    evidence: dict[str, bool],
    expected: str,
) -> None:
    assert native_adapter.continuous_consumer_state(**evidence) == expected


def test_provider_inventory_and_capability_blockers_use_native_status(
    config, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MNCS_FORGE_NATIVE_MODE", raising=False)
    raw = {
        **config.raw,
        "native": {"mode": "required"},
        "required_capabilities": ["bounded-structural"],
    }
    forge = Forge(replace(config, raw=raw))
    try:
        native = forge._native
        assert native is not None
        before = native.retained_status()["call_count"]

        inventory = forge.provider_list()
        after_inventory = native.retained_status()["call_count"]
        assert inventory["status"] == "UNKNOWN"
        assert after_inventory > before

        probe = forge.provider_probe("provider-pass")
        assert probe["status"] == "PASS"
        before_blockers = native.retained_status()["call_count"]
        blockers = forge.capability_blockers()

        assert blockers["status"] == "PASS"
        assert blockers["blocked"] is False
        assert native.retained_status()["call_count"] > before_blockers
    finally:
        forge.close()


def test_native_capability_policy_drives_provider_blocker_projection(
    config, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MNCS_FORGE_NATIVE_MODE", raising=False)
    provider = replace(
        config.providers["provider-pass"],
        required=True,
        unsupported_constructs=["bounded-structural"],
    )
    raw = {
        **config.raw,
        "native": {"mode": "required"},
        "required_capabilities": ["bounded-structural"],
    }
    forge = Forge(replace(config, raw=raw, providers={provider.provider_id: provider}))
    try:
        native = forge._native
        assert native is not None
        forge.provider_probe(provider.provider_id)
        before = native.retained_status()["call_count"]
        result = forge.capability_blockers()

        assert result["status"] == "UNKNOWN"
        assert result["blocked"] is True
        assert result["satisfied"] == []
        assert result["blockers"][0]["providers"] == [provider.provider_id]
        assert native.retained_status()["call_count"] > before
    finally:
        forge.close()


def test_native_off_keeps_the_explicit_compatibility_classifier(
    config, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MNCS_FORGE_NATIVE_MODE", "off")
    raw = {**config.raw, "required_capabilities": ["bounded-structural"]}
    forge = Forge(replace(config, raw=raw))
    try:
        assert forge._native is None
        forge.provider_probe("provider-pass")
        result = forge.capability_blockers()
        assert result["status"] == "PASS"
        assert result["blocked"] is False
        assert result["satisfied"][0]["providers"] == ["provider-pass"]
    finally:
        forge.close()
