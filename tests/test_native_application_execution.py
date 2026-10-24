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

    current = verified_build()
    current["schema_version"] = "mncs.compiler.application-build/11"
    provider.validate_build(current)

    current = verified_build()
    current["schema_version"] = "mncs.compiler.application-build/16"
    provider.validate_build(current)

    unsupported = verified_build()
    unsupported["schema_version"] = "mncs.compiler.application-build/17"
    with pytest.raises(provider.ProviderError, match="supported verified"):
        provider.validate_build(unsupported)

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
        provider.read_call(
            ["--call-json", '{ "arguments": [{"record": {"name":"Request","fields":[]}}] }'],
            build,
        )
    with pytest.raises(provider.ProviderError, match="32 KiB"):
        provider.read_call(["--call-json", "x" * (32 * 1024 + 1)], build)


def test_typed_call_accepts_two_distinct_verified_capability_providers() -> None:
    build = verified_build()
    call = {
        "arguments": [{"record": {"name": "Request", "fields": []}}],
        "capability_providers": [
            {
                "schema_version": provider.FORGE_STRUCTURED_PROVIDER_SCHEMA,
                "capability_identity": "mncs:capability:file",
                "input_schema": "mncs.request/1",
                "input_value": {"integer": {"value": 7}},
                "output_schema": "mncs.response/1",
            },
            {
                "schema_version": provider.FORGE_STRUCTURED_DIGEST_PROVIDER_SCHEMA,
                "capability_identity": "mncs:capability:digest",
            },
        ],
    }
    encoded = json.dumps(call, sort_keys=True, separators=(",", ":"))
    assert provider.read_call(["--call-json", encoded], build) == call

    duplicate = json.loads(json.dumps(call))
    duplicate["capability_providers"][1]["capability_identity"] = "mncs:capability:file"
    with pytest.raises(provider.ProviderError, match="duplicated"):
        provider.read_call(
            ["--call-json", json.dumps(duplicate, sort_keys=True, separators=(",", ":"))],
            build,
        )

    too_many = json.loads(json.dumps(call))
    too_many["capability_providers"].append({
        "schema_version": provider.FORGE_STRUCTURED_DIGEST_PROVIDER_SCHEMA,
        "capability_identity": "mncs:capability:third",
    })
    with pytest.raises(provider.ProviderError, match="at most two"):
        provider.read_call(
            ["--call-json", json.dumps(too_many, sort_keys=True, separators=(",", ":"))],
            build,
        )


def test_structured_file_provider_is_bound_to_verified_application_capability(
    tmp_path: Path,
) -> None:
    build = verified_build()
    capability = "mncs:0.2:capability:mncs.test.digest::digest_app::test_artifact"
    context_identity = "mncs:0.2:record-type:mncs.std.application.v1::ApplicationContext"
    build["artifact"]["value"]["requirements"] = {"capabilities": [capability]}
    build["capability_dispatch"]["bindings"] = [
        {"capability_identity": capability, "runtime_key_kind": "source-alias",
         "runtime_capability_key": "test_artifact", "operation": operation}
        for operation in ("structured_read", "structured_write")
    ]
    build["entry"]["interface"]["types"]["records"] = [
        {"name": "ApplicationContext", "identity": context_identity},
    ]
    build["entry"]["interface"]["signature"]["inputs"] = [
        {"name": "context", "type": context_identity},
    ]

    def bytes_value(value: str) -> dict:
        return {"sequence": {"values": [
            {"byte": {"value": byte}} for byte in value.encode("utf-8")
        ]}}

    context = {"record": {
        "name": "ApplicationContext", "type_identity": context_identity,
        "fields": [
            ["application_identity", bytes_value("x" * 32)],
            ["argv", {"sequence": {"values": [
                bytes_value("request.json"), bytes_value("result.json"),
            ]}}],
            ["argv_count", {"integer": {
                "type": {"bits": 64, "signed": False}, "value": 2,
            }}],
            ["current_dir", bytes_value(".")],
        ],
    }}
    input_value = {"record": {"name": "Request", "fields": []}}
    provider_input = {
        "schema_version": provider.FORGE_STRUCTURED_PROVIDER_SCHEMA,
        "capability_identity": capability,
        "input_schema": "mncs.test.request/1",
        "input_value": input_value,
        "output_schema": "mncs.test.result/1",
    }
    call = {"arguments": [context], "capability_providers": [provider_input]}
    encoded = json.dumps(call, sort_keys=True, separators=(",", ":"))
    parsed = provider.read_call(["--call-json", encoded], build)
    mapped, alias, config_path, output = provider.capability_provider_binding(
        parsed["capability_providers"][0], build, tmp_path, parsed["arguments"],
    )
    assert alias == "test_artifact"
    assert json.loads(config_path.read_text()) == {
        "schema_version": "mncs.vm.structured-file-provider/1",
        "identity": "mncs-forge.native-application.structured-file/1",
        "read": {"path": str(tmp_path / "capability-0" / "request.json"),
                 "schema": "mncs.test.request/1"},
        "write": {"path": str(tmp_path / "capability-0" / "result.json"),
                  "schema": "mncs.test.result/1"},
        "max_input_bytes": 2 * 1024 * 1024,
        "max_output_bytes": 256 * 1024,
    }
    staged_input = tmp_path / "capability-0" / "request.json"
    assert json.loads(staged_input.read_text()) == input_value
    mapped_fields = {row[0]: row[1] for row in mapped[0]["record"]["fields"]}
    assert mapped_fields["argv"]["sequence"]["values"] == [
        bytes_value(str(staged_input)),
        bytes_value(str(tmp_path / "capability-0" / "result.json")),
    ]
    assert output["capability_identity"] == capability
    assert output["output_schema"] == "mncs.test.result/1"

    duplicate_dispatch = json.loads(json.dumps(build))
    duplicate_dispatch["capability_dispatch"]["bindings"].append(
        duplicate_dispatch["capability_dispatch"]["bindings"][0]
    )
    with pytest.raises(provider.ProviderError, match="does not verify one read/write alias"):
        provider.capability_provider_binding(
            call["capability_providers"][0], duplicate_dispatch,
            tmp_path / "duplicate-dispatch", parsed["arguments"],
        )

    provider_input["capability_identity"] += ":wrong"
    mismatched = {"arguments": [context], "capability_providers": [provider_input]}
    encoded = json.dumps(mismatched, sort_keys=True, separators=(",", ":"))
    parsed = provider.read_call(["--call-json", encoded], build)
    with pytest.raises(provider.ProviderError, match="absent from the admitted artifact"):
        provider.capability_provider_binding(
            parsed["capability_providers"][0], build, tmp_path / "mismatch", parsed["arguments"],
        )


def test_structured_digest_provider_is_bound_to_exact_artifact_and_source_capability(
    tmp_path: Path,
) -> None:
    build = verified_build()
    capability = "mncs:0.2:capability:mncs.test.provider::run_selected_batch::provider_digest"
    alias = "provider_digest"
    build["artifact"]["value"]["requirements"] = {"capabilities": [capability]}
    build["capability_dispatch"]["bindings"] = [
        {
            "capability_identity": capability,
            "runtime_key_kind": "source-alias",
            "runtime_capability_key": alias,
            "operation": "structured_digest",
            "host_call_identity": "mncs:host-call:digest-1",
        },
        {
            "capability_identity": capability,
            "runtime_key_kind": "source-alias",
            "runtime_capability_key": alias,
            "operation": "structured_digest",
            "host_call_identity": "mncs:host-call:digest-2",
        },
    ]
    digest_input = {
        "schema_version": provider.FORGE_STRUCTURED_DIGEST_PROVIDER_SCHEMA,
        "capability_identity": capability,
    }
    arguments = [{"record": {"name": "Request", "fields": []}}]
    mapped, bound_alias, config_path, binding = provider.capability_provider_binding(
        digest_input, build, tmp_path, arguments,
    )
    assert mapped == arguments
    assert bound_alias == alias
    assert json.loads(config_path.read_text()) == {
        "schema_version": "mncs.vm.structured-digest-provider/1",
        "identity": "mncs-forge.native-application.structured-digest/1",
        "artifact_identity": IDENTITY,
        "capability_identity": capability,
    }
    assert binding["operations"] == ["structured_digest"]

    wrong_operation = json.loads(json.dumps(build))
    wrong_operation["capability_dispatch"]["bindings"][0]["operation"] = "structured_write"
    with pytest.raises(provider.ProviderError, match="one digest alias"):
        provider.capability_provider_binding(
            digest_input, wrong_operation, tmp_path / "wrong-operation", arguments,
        )

    wrong_capability = dict(digest_input, capability_identity=capability + ":wrong")
    with pytest.raises(provider.ProviderError, match="absent from the admitted artifact"):
        provider.capability_provider_binding(
            wrong_capability, build, tmp_path / "wrong-capability", arguments,
        )


def test_multi_output_structured_file_provider_binds_each_application_path(
    tmp_path: Path,
) -> None:
    build = verified_build()
    capability = "mncs:0.2:capability:mncs.test.provider::provider_batch_app::provider_artifact"
    context_identity = "mncs:0.2:record-type:mncs.std.application.v1::ApplicationContext"
    build["artifact"]["value"]["requirements"] = {"capabilities": [capability]}
    build["capability_dispatch"]["bindings"] = [
        {
            "capability_identity": capability,
            "runtime_key_kind": "source-alias",
            "runtime_capability_key": "provider_artifact",
            "operation": operation,
            "host_call_identity": f"mncs:host-call:{index}",
        }
        for index, operation in enumerate((
            "structured_read", "structured_write", "structured_write",
        ))
    ]
    build["entry"]["interface"]["types"]["records"] = [
        {"name": "ApplicationContext", "identity": context_identity},
    ]
    build["entry"]["interface"]["signature"]["inputs"] = [
        {"name": "context", "type": context_identity},
    ]

    def bytes_value(value: str) -> dict:
        return {"sequence": {"values": [
            {"byte": {"value": byte}} for byte in value.encode("utf-8")
        ]}}

    names = ["request.json", "result.json", "check.json"]
    context = {"record": {
        "name": "ApplicationContext", "type_identity": context_identity,
        "fields": [
            ["application_identity", {"sequence": {"values": [
                {"byte": {"value": 0}} for _ in range(32)
            ]}}],
            ["argv", {"sequence": {"values": [bytes_value(name) for name in names]}}],
            ["argv_count", {"integer": {
                "type": {"bits": 64, "signed": False}, "value": 3,
            }}],
            ["current_dir", bytes_value(".")],
        ],
    }}
    provider_input = {
        "schema_version": provider.FORGE_STRUCTURED_PROVIDER_MULTI_OUTPUT_SCHEMA,
        "capability_identity": capability,
        "input_schema": "mncs.test.provider-request/1",
        "input_value": {"record": {"name": "ProviderRequest", "fields": []}},
        "output_schemas": ["mncs.test.provider-result/1", "mncs.test.provider-check/1"],
    }
    arguments = [context]
    mapped, alias, config_path, binding = provider.capability_provider_binding(
        provider_input, build, tmp_path, arguments,
    )
    assert alias == "provider_artifact"
    assert json.loads(config_path.read_text()) == {
        "schema_version": "mncs.vm.structured-file-provider/2",
        "identity": "mncs-forge.native-application.structured-file/2",
        "read": {
            "path": str(tmp_path / "capability-0" / "request.json"),
            "schema": "mncs.test.provider-request/1",
        },
        "writes": [
            {
                "path": str(tmp_path / "capability-0" / "result.json"),
                "schema": "mncs.test.provider-result/1",
            },
            {
                "path": str(tmp_path / "capability-0" / "check.json"),
                "schema": "mncs.test.provider-check/1",
            },
        ],
        "max_input_bytes": 2 * 1024 * 1024,
        "max_output_bytes": 256 * 1024,
    }
    mapped_fields = {row[0]: row[1] for row in mapped[0]["record"]["fields"]}
    assert mapped_fields["argv"]["sequence"]["values"] == [
        bytes_value(str(tmp_path / "capability-0" / name)) for name in names
    ]
    assert binding["operations"] == ["structured_read", "structured_write"]
    assert binding["output_paths"] == [
        tmp_path / "capability-0" / "result.json",
        tmp_path / "capability-0" / "check.json",
    ]


def test_structured_provider_output_read_is_bounded_regular_and_no_follow(
    tmp_path: Path,
) -> None:
    regular = tmp_path / "result.json"
    regular.write_bytes(b'{"integer":{"value":1}}')
    assert provider.read_bounded_regular_file(
        regular, byte_limit=64, label="structured-write output",
    ) == b'{"integer":{"value":1}}'

    oversized = tmp_path / "oversized.json"
    oversized.write_bytes(b"x" * 65)
    with pytest.raises(provider.ProviderError, match="exceeds its 64-byte bound"):
        provider.read_bounded_regular_file(
            oversized, byte_limit=64, label="structured-write output",
        )

    directory = tmp_path / "directory"
    directory.mkdir()
    with pytest.raises(provider.ProviderError, match="not a regular file"):
        provider.read_bounded_regular_file(
            directory, byte_limit=64, label="structured-write output",
        )

    target = tmp_path / "target.json"
    target.write_bytes(b"{}")
    link = tmp_path / "link.json"
    link.symlink_to(target)
    with pytest.raises(provider.ProviderError, match="unavailable as a regular file"):
        provider.read_bounded_regular_file(
            link, byte_limit=64, label="structured-write output",
        )


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

    large_content = json.dumps(
        verified_build(), sort_keys=True, separators=(",", ":"),
    ).encode()
    assert len(large_content) < 1_071_468
    large_content += b" " * (1_071_468 - len(large_content))
    (input_root / "0000.artifact").write_bytes(large_content)
    manifest["artifacts"][0]["byte_count"] = len(large_content)
    manifest["artifacts"][0]["sha256"] = hashlib.sha256(large_content).hexdigest()
    monkeypatch.setenv("MNCS_ENV_INPUT_ARTIFACTS_JSON", json.dumps(manifest))
    build, identity = provider.input_build()
    assert build["artifact"]["identity"] == IDENTITY
    assert identity["byte_count"] == 1_071_468
    assert identity["sha256"] == hashlib.sha256(large_content).hexdigest()

    oversized = b"x" * (provider.BUILD_INPUT_LIMIT_BYTES + 4096)
    (input_root / "0000.artifact").write_bytes(oversized)
    manifest["artifacts"][0]["byte_count"] = len(oversized)
    manifest["artifacts"][0]["sha256"] = hashlib.sha256(oversized).hexdigest()
    monkeypatch.setenv("MNCS_ENV_INPUT_ARTIFACTS_JSON", json.dumps(manifest))
    with pytest.raises(provider.ProviderError, match="2 MiB"):
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
    success = capsys.readouterr()
    assert json.loads(success.out)["witnesses"][0]["execution"] == completed
    assert success.err == ""

    unsupported = {"outcome": {"kind": "unsupported"}, "record": {
        "artifact_id": artifact_id, "returned": [], "usage": {"steps": 1},
    }}
    responses.extend([
        {"status": "admitted", "artifact_id": artifact_id}, unsupported,
    ])
    assert task.main(task_args) == 1
    failure = capsys.readouterr()
    assert json.loads(failure.out)["status"] == "FAIL"
    diagnostic = json.loads(failure.err)
    assert diagnostic["limitations"][0].startswith("native application did not complete")


def test_vm_task_passes_explicit_provider_bindings_to_canonical_vm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    vm = tmp_path / "mncs-vm"
    artifact = tmp_path / "artifact.json"
    arguments = tmp_path / "arguments.json"
    type_arguments = tmp_path / "type-arguments.json"
    provider_config = tmp_path / "provider.json"
    for path in (vm, artifact, arguments, type_arguments, provider_config):
        path.write_text("{}")
    commands: list[list[str]] = []
    completed = {"outcome": {"kind": "completed"}, "record": {
        "artifact_id": IDENTITY, "returned": [{"integer": {"value": 4}}],
        "usage": {"steps": 12},
    }}

    def fake_run(argv, **_kwargs):
        commands.append(argv)
        document = (
            {"status": "admitted", "artifact_id": IDENTITY}
            if argv[1] == "admit"
            else completed
        )
        return SimpleNamespace(returncode=0, stdout=json.dumps(document), stderr="")

    monkeypatch.setattr(task.subprocess, "run", fake_run)
    task_args = [
        "--vm", str(vm), "--artifact", str(artifact), "--arguments", str(arguments),
        "--type-arguments", str(type_arguments), "--callable", "tests.native_readiness::run",
        "--expected-artifact-identity", IDENTITY,
        "--provider", f"test_artifact={provider_config}",
    ]
    assert task.main(task_args) == 0
    capsys.readouterr()
    vm_run = commands[1]
    assert vm_run[-2:] == ["--provider", f"test_artifact={provider_config.resolve()}"]


def test_failed_reexecution_cannot_retain_an_old_success_artifact(tmp_path: Path) -> None:
    for name in provider.SUCCESS_FILES:
        (tmp_path / name).write_text("old success")
    provider.clear_prior_outputs(tmp_path)
    provider.write_output(tmp_path, provider.FAILURE_FILE, {"status": "failed"})
    assert not any((tmp_path / name).exists() for name in provider.SUCCESS_FILES)
    assert json.loads((tmp_path / provider.FAILURE_FILE).read_text()) == {"status": "failed"}

