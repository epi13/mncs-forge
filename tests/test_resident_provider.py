"""Observable bounded resident contracts; real Store path has a separate proof."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from mncs_forge import continuous, resident
from mncs_forge.errors import ForgeError


@pytest.fixture
def config(tmp_path):
    return SimpleNamespace(
        root=tmp_path,
        state_dir=tmp_path / "state",
        config_path=tmp_path / "forge.toml",
        project_identity="test",
        raw={},
        continuous_settings={"enabled": True},
    )


@pytest.fixture
def identity(config):
    return {
        "checkout": str(Path(__file__).resolve().parents[1]),
        "revision": "test",
        "workspace_root": str(config.root),
        "config_path": str(config.config_path),
        "runtime": {"MNCS_LANGUAGE_ROOT": "selected-language"},
    }


def write_lease(config, identity, **changes):
    lease = {
        "pid": os.getpid(),
        "process_identity": resident.process_identity(os.getpid()),
        "provider_identity": identity,
        "instance": "test",
        "phase": "running",
        "started_monotonic": time.monotonic(),
    }
    lease.update(changes)
    continuous._write_json_path(continuous._supervisor_lease_path(config), lease)
    return lease


def test_absent_status_is_read_only(config, identity):
    assert resident.resident_status(config, identity)["state"] == "stopped"
    assert not config.state_dir.exists()


@pytest.mark.parametrize(
    "lease",
    [
        {"pid": os.getpid()},
        {"pid": "bad"},
        {"pid": os.getpid(), "process_identity": {"start_ticks": "0"}},
    ],
)
def test_pid_alone_is_never_readiness_or_control(config, identity, lease):
    path = continuous._supervisor_lease_path(config)
    continuous._write_json_path(path, lease)
    before = path.read_bytes()
    assert resident.resident_status(config, identity)["state"] == "stale"
    assert path.read_bytes() == before
    assert not resident.owns_process(lease)


@pytest.mark.parametrize("content", [b"broken", b"[]", b"x" * (resident.MAX_BYTES + 1)])
def test_corrupt_lease_fails_closed_without_launch(config, identity, content, monkeypatch):
    path = continuous._supervisor_lease_path(config)
    path.parent.mkdir(parents=True)
    path.write_bytes(content)
    monkeypatch.setattr(resident.subprocess, "Popen", lambda *_a, **_k: pytest.fail("launch"))
    with pytest.raises(ForgeError, match="malformed"):
        resident.resident_reconcile(config, identity)
    assert path.read_bytes() == content


def test_live_challenge_and_language_stream_are_required(config, identity, monkeypatch):
    write_lease(config, identity)
    continuous._write_json_path(
        continuous._language_service_lease_path(config),
        {
            "pid": os.getpid(),
            "process_identity": resident.process_identity(os.getpid()),
            "selected_bindings": resident.language_bindings(identity),
        },
    )
    endpoint = resident.ResidentEndpoint(config, identity, "test")
    try:
        assert resident.resident_status(config, identity)["state"] == "starting"
        endpoint.supervisor = SimpleNamespace(
            stream_identity="selected-stream",
            current_generation=2,
            current_cursor=3,
            _stop_requested=False,
        )
        monkeypatch.setattr(
            resident,
            "_probe_language_service",
            lambda _c: {"workspace_root": str(config.root), "stream_identity": "selected-stream"},
        )
        result = resident.resident_status(config, identity)
        assert result["state"] == "ready"
        assert "nonce" not in result["observed"]
        assert resident.resident_reconcile(config, identity)["operation"] == "reused"
        assert resident.resident_reconcile(config, identity)["operation"] == "reused"
        monkeypatch.setattr(
            resident,
            "_probe_language_service",
            lambda _c: {"workspace_root": str(config.root), "stream_identity": "ambient-stream"},
        )
        assert resident.resident_status(config, identity)["state"] == "degraded"
    finally:
        endpoint.close()


def test_another_checkout_cannot_satisfy_or_be_controlled(config, identity, monkeypatch):
    write_lease(config, {**identity, "checkout": "other-checkout"})
    monkeypatch.setattr(resident, "signal_owned", lambda *_a: pytest.fail("foreign control"))
    assert resident.resident_status(config, identity)["state"] == "incompatible"
    assert resident.resident_reconcile(config, identity)["operation"] == "blocked"


def test_revision_change_reconciles_only_the_owned_instance(config, identity, monkeypatch):
    lease = write_lease(config, {**identity, "revision": "old"})
    signalled = []
    monkeypatch.setattr(resident, "signal_owned", signalled.append)
    assert resident.resident_reconcile(config, identity)["operation"] == "stopping"
    assert signalled == [lease]


def test_receive_has_output_and_total_time_bounds():
    reader, writer = socket.socketpair()
    try:
        writer.sendall(b"x" * (resident.MAX_BYTES + 1))
        with pytest.raises(ValueError, match="bound"):
            resident._receive(reader)
    finally:
        reader.close()
        writer.close()


def test_pidfd_controls_the_verified_birth_only():
    child = subprocess.Popen(["/usr/bin/sleep", "10"])
    try:
        lease = {"pid": child.pid, "process_identity": resident.process_identity(child.pid)}
        with pytest.raises(ForgeError, match="birth"):
            resident.signal_owned({**lease, "process_identity": {"boot": "wrong"}})
        assert child.poll() is None
        resident.signal_owned(lease)
        child.wait(timeout=1)
    finally:
        if child.poll() is None:
            child.terminate()
            child.wait(timeout=1)


def test_missing_selected_runtime_never_uses_ambient(config, monkeypatch):
    monkeypatch.delenv("MNCS_LANGUAGE_ROOT", raising=False)
    with pytest.raises(ForgeError, match="ambient fallback"):
        resident.selected_identity(config)


def test_child_runtime_ignores_ambient_imports_and_compiler(config, identity, monkeypatch):
    monkeypatch.setenv("PYTHONPATH", "/ambient-forge:/ambient-store")
    monkeypatch.setenv("MNCS_CLI", "/ambient-mncs")
    monkeypatch.setenv("MNCS_FORGE_NATIVE_MODE", "off")
    runtime = {
        "MNCS_BIN": "/selected/language/mncs",
        "MNCS_LANGUAGE_SERVICE_HOST": "/selected/service",
        "MNCS_LANGUAGE_ROOT": "/selected/language",
        "MNCS_STORE_ROOT": "/selected/store",
        "MNCS_LIBRARY_ROOT": "/selected/stdlib/library",
    }
    environment = resident.selected_environment({**identity, "runtime": runtime})
    assert environment["MNCS_LIBRARY_PATH"] == runtime["MNCS_LIBRARY_ROOT"]
    assert environment["MNCS_CLI"] == runtime["MNCS_BIN"]
    assert "MNCS_FORGE_NATIVE_MODE" not in environment
    assert environment["PYTHONPATH"] == identity["checkout"] + "/src:/selected/store/python"


def test_long_default_socket_is_short_stable_and_project_specific(config):
    config.root = config.root / ("x" * 100)
    first = continuous._language_service_socket(config)
    assert len(os.fsencode(first)) < 104
    assert first == continuous._language_service_socket(config)
    config.root = config.root / "other"
    assert first != continuous._language_service_socket(config)


def test_live_ambient_language_service_is_not_attached(config, identity, monkeypatch):
    monkeypatch.setattr(
        continuous, "_probe_language_service", lambda _c: {"workspace_root": str(config.root)}
    )
    with pytest.raises(ForgeError, match="not owned"):
        continuous.ensure_language_service(config, selected_bindings=identity["runtime"])


@pytest.mark.parametrize("operation", ["status", "reconcile"])
def test_provider_deadline_includes_configuration_and_artifact_reads(
    operation, monkeypatch, capsys
):
    monkeypatch.setattr(resident, "DEADLINE_SECONDS", 0.02)
    monkeypatch.setattr(resident, "resolve_continuous_config", lambda **_kw: time.sleep(1))
    before = time.monotonic()
    assert resident.main([operation, "--config", "/selected/forge.toml"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert time.monotonic() - before < 0.5
    assert result["state"] == "blocked"
    assert result["diagnostics"][0]["code"] == "RESIDENT_DEADLINE"


def test_provider_result_output_has_a_total_bound(config, identity, monkeypatch, capsys):
    monkeypatch.setattr(resident, "resolve_continuous_config", lambda **_kw: config)
    monkeypatch.setattr(resident, "selected_identity", lambda _c: identity)
    monkeypatch.setattr(
        resident, "resident_status", lambda *_a: {"extra": "x" * resident.MAX_BYTES}
    )
    assert resident.main(["status", "--config", "/selected/forge.toml"]) == 0
    output = capsys.readouterr().out
    assert len(output.encode()) <= resident.MAX_BYTES
    assert json.loads(output)["diagnostics"][0]["code"] == "RESIDENT_OUTPUT_LIMIT"
