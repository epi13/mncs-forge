from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import native_application_execution_provider as provider  # noqa: E402
import native_application_vm_task as task  # noqa: E402


IDENTITY = "sha256:" + "a" * 64


def verified_build() -> dict:
    vm_artifact = {
        "schema_version": "mncs.vm.artifact/1",
        "artifact_id": IDENTITY,
        "callables": [],
    }
    interface = {
        "schema_version": "mncs.compiler.callable-interface/1",
        "identity": IDENTITY,
        "status": "verified",
        "types": {"status": "verified", "unresolved_references": []},
        "signature": {"inputs": [{"name": "request", "type": "Request"}]},
    }
    return {
        "schema_version": "mncs.compiler.application-build/4",
        "status": "verified",
        "source_snapshot": {"identity": IDENTITY},
        "dependency_graph": {"identity": IDENTITY},
        "entry": {"module": "tests.native_readiness", "function": "run",
                  "status": "verified", "interface": interface},
        "artifact": {"schema_version": "mncs.vm.artifact/1", "identity": IDENTITY,
                     "unresolved_obligations": [], "value": vm_artifact},
        "capability_dispatch": {"status": "verified", "unresolved_obligations": []},
        "execution_target_compatibility": {
            "target": "mncs-vm-0.1", "status": "verified", "compatible": True,
        },
        "unresolved_obligations": [],
    }


def test_verified_build_requires_closed_vm_artifact_and_typed_interface() -> None:
    provider.validate_build(verified_build())

    bad = verified_build()
    bad["entry"]["interface"]["types"]["unresolved_references"] = ["Missing"]
    with pytest.raises(provider.ProviderError, match="unresolved"):
        provider.validate_build(bad)

    bad = verified_build()
    bad["artifact"]["identity"] = "sha256:" + "b" * 64
    with pytest.raises(provider.ProviderError, match="unresolved"):
        provider.validate_build(bad)

    bad = verified_build()
    bad["status"] = "unsupported"
    with pytest.raises(provider.ProviderError, match="verified"):
        provider.validate_build(bad)


def test_typed_call_is_canonical_bounded_and_matches_callable_arity() -> None:
    build = verified_build()
    typed = {"arguments": [{"record": {"name": "Request", "fields": []}}]}
    encoded = json.dumps(typed, sort_keys=True, separators=(",", ":"))
    assert provider.read_call(["--call-json", encoded], build) == typed

    with pytest.raises(provider.ProviderError, match="arity"):
        provider.read_call(["--call-json", '{"arguments":[]}'], build)
    with pytest.raises(provider.ProviderError, match="canonical"):
        provider.read_call(["--call-json", '{ "arguments": [{"record": {"name":"Request","fields":[]}}] }'], build)
    with pytest.raises(provider.ProviderError, match="32 KiB"):
        provider.read_call(["--call-json", "x" * (32 * 1024 + 1)], build)


def test_store_build_input_bytes_are_bound_to_the_environment_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    content = json.dumps(verified_build(), sort_keys=True, separators=(",", ":")).encode()
    input_root = tmp_path / "input"
    input_root.mkdir()
    (input_root / "0000.artifact").write_bytes(content)
    manifest = {
        "schema_version": "mncs.environment.provider-input-artifacts/1",
        "artifacts": [{
            "name": "native-application-build",
            "capability": "mncs-compiler:native-application-build",
            "binding_id": "cap_1234567890abcdef",
            "path": "native-application-build.json",
            "local_path": "0000.artifact",
            "store_reference": "mncs-store:session:artifact:build",
            "byte_count": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
        }],
    }
    monkeypatch.setenv("MNCS_ENV_INPUT_ARTIFACTS_JSON", json.dumps(manifest))
    monkeypatch.setenv("MNCS_ENV_INPUT_ARTIFACT_DIR", str(input_root))
    build, identity = provider.input_build()
    assert build["artifact"]["identity"] == IDENTITY
    assert identity["sha256"] == hashlib.sha256(content).hexdigest()

    manifest["artifacts"][0]["sha256"] = "0" * 64
    monkeypatch.setenv("MNCS_ENV_INPUT_ARTIFACTS_JSON", json.dumps(manifest))
    with pytest.raises(provider.ProviderError, match="Store provenance"):
        provider.input_build()

    oversized = b"x" * (provider.BUILD_INPUT_LIMIT_BYTES + 4096)
    (input_root / "0000.artifact").write_bytes(oversized)
    manifest["artifacts"][0]["byte_count"] = len(oversized)
    manifest["artifacts"][0]["sha256"] = hashlib.sha256(oversized).hexdigest()
    monkeypatch.setenv("MNCS_ENV_INPUT_ARTIFACTS_JSON", json.dumps(manifest))
    with pytest.raises(provider.ProviderError, match="1 MiB"):
        provider.input_build()


def test_vm_task_preserves_structured_result_and_rejects_unsupported_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    vm = tmp_path / "mncs-vm"
    artifact = tmp_path / "artifact.json"
    arguments = tmp_path / "arguments.json"
    type_arguments = tmp_path / "type-arguments.json"
    for path in (vm, artifact, arguments, type_arguments):
        path.write_text("{}")
    artifact_id = IDENTITY
    completed = {"outcome": {"kind": "completed"}, "record": {
        "artifact_id": artifact_id, "returned": [{"integer": {"value": 4}}],
        "usage": {"steps": 12},
    }}
    responses = [
        {"status": "admitted", "artifact_id": artifact_id}, completed,
    ]

    def fake_run(argv, **_kwargs):
        return SimpleNamespace(returncode=0, stdout=json.dumps(responses.pop(0)), stderr="")

    monkeypatch.setattr(task.subprocess, "run", fake_run)
    task_args = [
        "--vm", str(vm), "--artifact", str(artifact), "--arguments", str(arguments),
        "--type-arguments", str(type_arguments), "--callable", "tests.native_readiness::run",
        "--expected-artifact-identity", artifact_id,
    ]
    assert task.main(task_args) == 0
    assert json.loads(capsys.readouterr().out)["witnesses"][0]["execution"] == completed

    unsupported = {"outcome": {"kind": "unsupported"}, "record": {
        "artifact_id": artifact_id, "returned": [], "usage": {"steps": 1},
    }}
    responses.extend([
        {"status": "admitted", "artifact_id": artifact_id}, unsupported,
    ])
    assert task.main(task_args) == 1
    assert json.loads(capsys.readouterr().out)["status"] == "FAIL"


def test_failed_reexecution_cannot_retain_an_old_success_artifact(tmp_path: Path) -> None:
    for name in provider.SUCCESS_FILES:
        (tmp_path / name).write_text("old success")
    provider.clear_prior_outputs(tmp_path)
    provider.write_output(tmp_path, provider.FAILURE_FILE, {"status": "failed"})
    assert not any((tmp_path / name).exists() for name in provider.SUCCESS_FILES)
    assert json.loads((tmp_path / provider.FAILURE_FILE).read_text()) == {"status": "failed"}

