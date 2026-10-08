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
    status = resident.resident_status(config, identity)
    assert status["state"] == "stopped"
    assert not config.state_dir.exists()
    # Readiness predicates address the consumer position; a stopped
    # resident reports it (never ready) so recovery stays admissible.
    assert status["continuous_consumer"]["state"] != "ready"


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


def test_resident_health_exposes_and_preserves_cursor_recovery(config, identity, monkeypatch):
    status_path = config.state_dir / "continuous" / "status.json"
    continuous._write_json_path(
        status_path,
        {
            "event_stream_identity": "old-stream",
            "event_cursor": 2,
            "cursor_recovery": {
                "status": "required",
                "requested_stream_identity": "old-stream",
                "requested_cursor": 2,
                "observed_stream_identity": "new-stream",
                "current_cursor": 8,
            },
        },
    )
    write_lease(config, identity, instance="cursor-test")
    endpoint = resident.ResidentEndpoint(config, identity, "cursor-test")
    endpoint.supervisor = SimpleNamespace(
        stream_identity="old-stream",
        current_generation=9,
        current_cursor=2,
        _stop_requested=False,
        _status_path=lambda: status_path,
    )
    monkeypatch.setattr(
        resident,
        "_probe_language_service",
        lambda _c: pytest.fail("blocked cursor must not claim readiness"),
    )
    try:
        health = resident.resident_status(config, identity)
        assert health["state"] == "degraded"
        assert health.get("continuous_consumer", {}).get("state") == "blocked", health
        assert health["diagnostics"][0]["code"] == "RESIDENT_EVENT_CURSOR_RECOVERY"

        monkeypatch.setattr(resident, "signal_owned", lambda *_args: pytest.fail("unsafe reset"))
        result = resident.resident_reconcile(config, identity)
        assert result["operation"] == "blocked"
        assert result["status"]["continuous_consumer"]["state"] == "blocked"
    finally:
        endpoint.close()


def test_resident_consumer_readiness_requires_matching_durable_pair(config):
    status_path = config.state_dir / "continuous" / "status.json"
    continuous._write_json_path(
        status_path,
        {"event_stream_identity": "selected-stream", "event_cursor": 3},
    )
    supervisor = SimpleNamespace(
        stream_identity="selected-stream", current_cursor=3, _status_path=lambda: status_path
    )
    assert resident._continuous_consumer_status(config, supervisor)["state"] == "ready"
    supervisor.current_cursor = 4
    assert resident._continuous_consumer_status(config, supervisor)["state"] == "reconciling"


def test_another_checkout_cannot_satisfy_or_be_controlled(config, identity, monkeypatch):
    write_lease(config, {**identity, "checkout": "other-checkout"})
    monkeypatch.setattr(resident, "signal_owned", lambda *_a: pytest.fail("foreign control"))
    status = resident.resident_status(config, identity)
    assert status["state"] == "incompatible"
    # Environment predicates need this field even for stale selections so
    # the provider response remains schema-valid and recovery stays available.
    assert status["continuous_consumer"]["state"] == "starting"
    assert resident.resident_reconcile(config, identity)["operation"] == "blocked"


def test_nonresponding_owned_lease_keeps_consumer_response_shape(config, identity):
    write_lease(config, identity)
    status = resident.resident_status(config, identity)
    assert status["state"] == "degraded"
    assert status["continuous_consumer"]["state"] == "starting"


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


def test_environment_selected_language_service_is_attached_without_launch(
    config, identity, monkeypatch
):
    selected = {
        "MNLS_SERVICE_SOCKET": "/selected/.mncs/mnls.sock",
        "MNLS_SERVICE_STREAM_IDENTITY": "mnls-stream-selected",
        "MNLS_SERVICE_WORKSPACE_ROOT": str(config.root.parent),
        "MNLS_SERVICE_REPOSITORY_ROOTS_JSON": json.dumps([str(config.root)]),
    }
    for name, value in selected.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(
        continuous.LanguageServiceSocket,
        "request",
        lambda *_a, **_k: {
            "workspace_root": str(config.root.parent),
            "stream_identity": "mnls-stream-selected",
        },
    )
    monkeypatch.setattr(
        continuous.subprocess,
        "Popen",
        lambda *_a, **_k: pytest.fail("Environment-selected LS must not be duplicated"),
    )

    attached = continuous.ensure_language_service(
        config, selected_bindings=resident.language_bindings(identity)
    )
    assert attached["state"] == "attached"
    assert attached["ownership"] == "environment-selected"
    assert continuous._language_service_socket(config) == Path(selected["MNLS_SERVICE_SOCKET"])


def test_environment_selected_language_service_mismatch_fails_closed(config, monkeypatch):
    monkeypatch.setenv("MNLS_SERVICE_SOCKET", "/selected/.mncs/mnls.sock")
    monkeypatch.setenv("MNLS_SERVICE_STREAM_IDENTITY", "mnls-stream-current")
    monkeypatch.setenv("MNLS_SERVICE_WORKSPACE_ROOT", str(config.root.parent))
    monkeypatch.setenv("MNLS_SERVICE_REPOSITORY_ROOTS_JSON", json.dumps([str(config.root)]))
    monkeypatch.setattr(
        continuous.LanguageServiceSocket,
        "request",
        lambda *_a, **_k: {
            "workspace_root": str(config.root.parent),
            "stream_identity": "old-stream",
        },
    )
    monkeypatch.setattr(
        continuous.subprocess,
        "Popen",
        lambda *_a, **_k: pytest.fail("stale selected LS must not trigger a duplicate"),
    )
    with pytest.raises(ForgeError, match="does not match the Environment stream"):
        continuous.ensure_language_service(config)


def test_orphaned_forge_owned_language_service_can_be_stopped(config, identity, monkeypatch):
    language = {
        "pid": os.getpid(),
        "process_identity": resident.process_identity(os.getpid()),
        "selected_bindings": resident.language_bindings(identity),
    }
    continuous._write_json_path(continuous._language_service_lease_path(config), language)
    signalled = []
    monkeypatch.setattr(resident, "signal_owned", signalled.append)

    result = resident.resident_reconcile(
        config,
        identity,
        stop=True,
        include_language_service=True,
    )

    assert result["operation"] == "stopping"
    assert [item["pid"] for item in signalled] == [os.getpid()]


def test_stale_orphan_lease_removes_only_its_dead_socket(config, identity, monkeypatch):
    lease_path = continuous._language_service_lease_path(config)
    socket_path = continuous._language_service_socket(config)
    socket_path.parent.mkdir(mode=0o700, parents=True)
    bound = socket.socket(socket.AF_UNIX)
    try:
        bound.bind(str(socket_path))
    finally:
        bound.close()
    lease = {
        "pid": 2**30,
        "process_identity": {"boot": "old", "start_ticks": "old"},
        "selected_bindings": resident.language_bindings(identity),
        "owned_by_continuous": True,
        "workspace_root": str(config.root.resolve()),
    }
    continuous._write_json_path(lease_path, lease)
    monkeypatch.setattr(
        resident,
        "_probe_language_service",
        lambda _c: (_ for _ in ()).throw(
            ForgeError("LANGUAGE_SERVICE_UNAVAILABLE", "connection refused")
        ),
    )

    result = resident.resident_reconcile(
        config,
        identity,
        stop=True,
        include_language_service=True,
    )

    assert result["operation"] == "stopped"
    assert not lease_path.exists()
    assert not socket_path.exists()


def test_stale_orphan_lease_preserves_occupied_project_file(config, identity):
    lease_path = continuous._language_service_lease_path(config)
    socket_path = continuous._language_service_socket(config)
    socket_path.parent.mkdir(mode=0o700, parents=True)
    socket_path.write_text("authored file", encoding="utf-8")
    continuous._write_json_path(
        lease_path,
        {
            "pid": 2**30,
            "process_identity": {"boot": "old", "start_ticks": "old"},
            "selected_bindings": resident.language_bindings(identity),
            "owned_by_continuous": True,
            "workspace_root": str(config.root.resolve()),
        },
    )

    result = resident.resident_reconcile(
        config,
        identity,
        stop=True,
        include_language_service=True,
    )

    assert result["operation"] == "blocked"
    assert result["status"]["diagnostics"][0]["code"] == "RESIDENT_LANGUAGE_SOCKET_OCCUPIED"
    assert socket_path.read_text(encoding="utf-8") == "authored file"
    assert lease_path.exists()


def test_reconcile_capability_exposes_owned_stop_flags(config, identity, monkeypatch, capsys):
    observed = {}
    monkeypatch.setattr(resident, "resolve_continuous_config", lambda **_kw: config)
    monkeypatch.setattr(resident, "selected_identity", lambda _c: identity)

    def reconcile(_config, _identity, *, stop=False, include_language_service=False):
        observed.update(stop=stop, include_language_service=include_language_service)
        return {"schema_version": resident.RECONCILE_SCHEMA, "operation": "stopped"}

    monkeypatch.setattr(resident, "resident_reconcile", reconcile)
    assert (
        resident.main(
            [
                "reconcile",
                "--stop",
                "--include-language-service",
                "--workspace",
                str(config.root),
            ]
        )
        == 0
    )
    assert observed == {"stop": True, "include_language_service": True}
    assert json.loads(capsys.readouterr().out)["operation"] == "stopped"


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


def test_provider_status_resolves_config_from_selected_workspace(
    config, identity, monkeypatch, capsys
):
    observed = []

    def resolve(*, workspace, explicit_config):
        observed.append((workspace, explicit_config))
        assert workspace == config.root
        assert explicit_config is None
        return config

    monkeypatch.setattr(resident, "resolve_continuous_config", resolve)
    monkeypatch.setattr(resident, "selected_identity", lambda _config: identity)
    monkeypatch.setattr(
        resident,
        "resident_status",
        lambda *_args: {"schema_version": resident.STATUS_SCHEMA, "state": "stopped"},
    )

    assert resident.main(["status", "--workspace", str(config.root)]) == 0
    assert observed == [(config.root, None)]
    assert json.loads(capsys.readouterr().out)["state"] == "stopped"


def test_provider_refuses_ambient_configuration_fallback():
    with pytest.raises(SystemExit) as result:
        resident.main(["status"])
    assert result.value.code == 2
