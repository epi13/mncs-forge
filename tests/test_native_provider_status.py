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
