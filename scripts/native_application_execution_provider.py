#!/usr/bin/env python3
"""Compose a verified Compiler artifact with Forge admission and canonical VM execution.

This boundary handles Store artifact validation, selected toolchain dispatch,
process lifecycle, and Forge receipt collection. Application semantics remain
in the compiled MNCS program and canonical VM.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mncs_forge.adapters import LocalProcessRunner  # noqa: E402
from mncs_forge.config import load_config  # noqa: E402
from mncs_forge.engine import Forge  # noqa: E402
from mncs_forge.execution_observations import canonical_sha256  # noqa: E402
from mncs_forge.record_store import LocalRecordStore  # noqa: E402
from mncs_forge.resource_envelope import SystemdCgroupEnvelope  # noqa: E402

CAPABILITY = "mncs-forge:native-application-execution"
INPUT_SCHEMA = "mncs.environment.provider-input-artifacts/1"
BUILD_SCHEMA = "mncs.compiler.application-build/4"
VM_ARTIFACT_SCHEMA = "mncs.vm.artifact/1"
CALL_LIMIT_BYTES = 32 * 1024
BUILD_INPUT_LIMIT_BYTES = 1024 * 1024
RESOURCE_ENVELOPE = {
    "memory_max_bytes": 512 * 1024 * 1024,
    "memory_cap_bytes": 512 * 1024 * 1024,
    "pids_limit": 64,
    "concurrency_limit": 1,
    "runtime_max_seconds": 45,
}
SUCCESS_FILES = ("native-application-execution.json", "forge-execution-receipt.json")
FAILURE_FILE = "native-application-failure.json"


class ProviderError(RuntimeError):
    pass


def json_bytes(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def required_identity(value: Any, label: str) -> str:
    if (not isinstance(value, str) or len(value) != 71
            or not value.startswith("sha256:")
            or any(ch not in "0123456789abcdef" for ch in value[7:])):
        raise ProviderError(f"{label} is not a SHA-256 identity")
    return value


def input_build() -> tuple[dict[str, Any], dict[str, Any]]:
    raw_manifest = os.environ.get("MNCS_ENV_INPUT_ARTIFACTS_JSON", "")
    raw_root = os.environ.get("MNCS_ENV_INPUT_ARTIFACT_DIR", "")
    if not raw_manifest or not raw_root:
        raise ProviderError("native application execution requires a Store build artifact")
    try:
        manifest = json.loads(raw_manifest)
    except json.JSONDecodeError as error:
        raise ProviderError("Environment input-artifact manifest is invalid JSON") from error
    rows = manifest.get("artifacts") if isinstance(manifest, dict) else None
    if (not isinstance(manifest, dict) or manifest.get("schema_version") != INPUT_SCHEMA
            or not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict)):
        raise ProviderError("native application execution accepts one bounded Store input")
    row = rows[0]
    expected = {
        "name": "native-application-build",
        "capability": "mncs-compiler:native-application-build",
        "path": "native-application-build.json",
    }
    if any(row.get(key) != value for key, value in expected.items()):
        raise ProviderError("native application execution received an unexpected Compiler artifact")
    binding_id = row.get("binding_id")
    if (not isinstance(binding_id, str) or not binding_id.startswith("cap_")
            or len(binding_id) > 128):
        raise ProviderError("Compiler build binding identity is malformed")
    relative = Path(str(row.get("local_path", "")))
    if (relative.is_absolute() or not relative.parts
            or any(part in {"", ".", ".."} for part in relative.parts)):
        raise ProviderError("Environment input-artifact path is invalid")
    root_path = Path(raw_root)
    if root_path.is_symlink():
        raise ProviderError("Environment input-artifact directory is a symlink")
    try:
        root = root_path.resolve(strict=True)
        source = root / relative
        if source.is_symlink():
            raise ProviderError("Environment input artifact is a symlink")
        source = source.resolve(strict=True)
    except OSError as error:
        raise ProviderError("Environment input artifact is unavailable") from error
    if not source.is_relative_to(root) or not source.is_file():
        raise ProviderError("Environment input artifact escapes its private directory")
    try:
        with source.open("rb") as stream:
            data = stream.read(BUILD_INPUT_LIMIT_BYTES + 1)
    except OSError as error:
        raise ProviderError("Environment input artifact could not be read") from error
    if len(data) > BUILD_INPUT_LIMIT_BYTES:
        raise ProviderError("Compiler build artifact exceeds the 1 MiB input bound")
    digest = hashlib.sha256(data).hexdigest()
    if (len(data) != row.get("byte_count") or digest != row.get("sha256")
            or not isinstance(row.get("store_reference"), str)
            or not row["store_reference"]):
        raise ProviderError("Compiler build artifact bytes differ from canonical Store provenance")
    try:
        build = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ProviderError("Compiler build artifact is not valid UTF-8 JSON") from error
    if not isinstance(build, dict):
        raise ProviderError("Compiler build artifact must be a JSON object")
    validate_build(build)
    return build, {
        "capability": row["capability"],
        "binding_id": row.get("binding_id"),
        "path": row["path"],
        "store_reference": row["store_reference"],
        "byte_count": len(data),
        "sha256": digest,
    }


def validate_build(build: dict[str, Any]) -> None:
    if build.get("schema_version") != BUILD_SCHEMA or build.get("status") != "verified":
        raise ProviderError("Compiler did not provide a verified application-build/4 result")
    entry = build.get("entry")
    interface = entry.get("interface") if isinstance(entry, dict) else None
    artifact = build.get("artifact")
    value = artifact.get("value") if isinstance(artifact, dict) else None
    compatibility = build.get("execution_target_compatibility")
    dispatch = build.get("capability_dispatch")
    types = interface.get("types") if isinstance(interface, dict) else None
    if (not isinstance(entry, dict) or entry.get("status") != "verified"
            or not isinstance(interface, dict)
            or interface.get("schema_version") != "mncs.compiler.callable-interface/1"
            or interface.get("status") != "verified"
            or not isinstance(interface.get("signature"), dict)
            or not isinstance(types, dict)
            or types.get("status") != "verified"
            or types.get("unresolved_references") != []
            or not isinstance(artifact, dict)
            or artifact.get("schema_version") != VM_ARTIFACT_SCHEMA
            or not isinstance(value, dict)
            or value.get("schema_version") != VM_ARTIFACT_SCHEMA
            or artifact.get("identity") != value.get("artifact_id")
            or artifact.get("unresolved_obligations") != []
            or not isinstance(compatibility, dict)
            or compatibility.get("target") != "mncs-vm-0.1"
            or compatibility.get("status") != "verified"
            or compatibility.get("compatible") is not True
            or not isinstance(dispatch, dict)
            or dispatch.get("status") != "verified"
            or dispatch.get("unresolved_obligations") != []
            or build.get("unresolved_obligations") != []):
        raise ProviderError("Compiler artifact, callable, target, or capability closure is unresolved")
    source_snapshot = build.get("source_snapshot")
    dependency_graph = build.get("dependency_graph")
    if not isinstance(source_snapshot, dict) or not isinstance(dependency_graph, dict):
        raise ProviderError("Compiler source and dependency identities are unavailable")
    for label, field in (
        ("artifact identity", artifact.get("identity")),
        ("callable interface identity", interface.get("identity")),
        ("source snapshot identity", source_snapshot.get("identity")),
        ("dependency graph identity", dependency_graph.get("identity")),
    ):
        required_identity(field, label)
    if not isinstance(entry.get("module"), str) or not isinstance(entry.get("function"), str):
        raise ProviderError("Compiler entry target is malformed")
    signature = interface["signature"]
    if not isinstance(signature.get("inputs"), list) or len(signature["inputs"]) > 32:
        raise ProviderError("Compiler callable input interface is malformed or exceeds its bound")


def read_call(argv: list[str], build: dict[str, Any]) -> dict[str, Any]:
    if len(argv) != 2 or argv[0] != "--call-json":
        raise ProviderError("native application execution accepts one --call-json value")
    encoded = argv[1]
    raw = encoded.encode("utf-8")
    if len(raw) > CALL_LIMIT_BYTES:
        raise ProviderError("typed call exceeds the 32 KiB input bound")
    try:
        call = json.loads(encoded)
        canonical = json_bytes(call).decode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError, json.JSONDecodeError) as error:
        raise ProviderError("typed application call is invalid JSON") from error
    arguments = call.get("arguments") if isinstance(call, dict) else None
    expected = build["entry"]["interface"]["signature"]["inputs"]
    if (not isinstance(call, dict) or set(call) != {"arguments"}
            or not isinstance(arguments, list) or len(arguments) != len(expected)):
        raise ProviderError("typed application call does not match the selected callable arity")
    if canonical != encoded:
        raise ProviderError("typed application call JSON is not canonical")
    return call


def selected_runtime() -> dict[str, Any]:
    try:
        compiler_root = Path(os.environ["MNCS_COMPILER_CHECKOUT"]).resolve(strict=True)
        compiler_probe = Path(os.environ["MNCS_COMPILER_PROBE"]).resolve(strict=True)
        vm_root = Path(os.environ["MNCS_VM_CHECKOUT"]).resolve(strict=True)
        vm = Path(os.environ["MNCS_VM_BIN"]).resolve(strict=True)
    except (KeyError, OSError) as error:
        raise ProviderError("Compiler and canonical VM toolchain bindings are unavailable") from error
    for path, parent, label in (
        (compiler_probe, compiler_root, "selected Compiler probe"),
        (vm, vm_root, "selected canonical VM"),
    ):
        if not path.is_relative_to(parent) or not path.is_file() or not os.access(path, os.X_OK):
            raise ProviderError(f"{label} is missing or escapes its selected checkout")
    try:
        compiler_head = subprocess.run(
            ["git", "-C", str(compiler_root), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True, timeout=5,
        ).stdout.strip()
        vm_head = subprocess.run(
            ["git", "-C", str(vm_root), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True, timeout=5,
        ).stdout.strip()
        producer_doc = json.loads(subprocess.run(
            [str(compiler_probe), "--producer-info"], check=True,
            capture_output=True, text=True, timeout=10,
        ).stdout)
        runtime_doc = json.loads(subprocess.run(
            [str(vm), "describe"], check=True, capture_output=True,
            text=True, timeout=10,
        ).stdout)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        raise ProviderError("selected Compiler or canonical VM identity could not be observed") from error
    if runtime_doc.get("artifact_schema") != VM_ARTIFACT_SCHEMA:
        raise ProviderError("selected VM does not declare the canonical application artifact schema")
    required_identity(producer_doc.get("identity"), "selected Compiler producer identity")
    return {
        "compiler": {
            "checkout_revision": compiler_head,
            "probe_sha256": file_sha256(compiler_probe),
            "producer_identity": producer_doc.get("identity"),
        },
        "vm": {
            "checkout_revision": vm_head,
            "executable_sha256": file_sha256(vm),
            "runtime_identity": "sha256:" + canonical_sha256(runtime_doc),
            "runtime": runtime_doc,
        },
    }


def output_directory() -> Path:
    raw = os.environ.get("MNCS_ENV_SESSION_ARTIFACT_DIR", "")
    if not raw:
        raise ProviderError("Environment result artifact directory is unavailable")
    path = Path(raw)
    if path.is_symlink():
        raise ProviderError("Environment result artifact directory is a symlink")
    try:
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise ProviderError("Environment result artifact directory cannot be opened") from error
    if not resolved.is_dir():
        raise ProviderError("Environment result artifact directory is not a directory")
    return resolved


def clear_prior_outputs(directory: Path) -> None:
    for name in (*SUCCESS_FILES, FAILURE_FILE):
        path = directory / name
        try:
            info = path.lstat()
        except FileNotFoundError:
            continue
        if not path.is_file() or path.is_symlink():
            raise ProviderError("restored result artifact is not a regular file")
        if info.st_uid != os.getuid():
            raise ProviderError("restored result artifact has a different owner")
        path.unlink()


def write_output(directory: Path, name: str, value: object) -> None:
    data = json_bytes(value)
    if len(data) > 512 * 1024:
        raise ProviderError(f"Environment result artifact exceeds its size bound: {name}")
    destination = directory / name
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(destination, flags, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def receipt_from_forge(forge: Forge) -> tuple[dict[str, Any], dict[str, Any]]:
    records = forge.ledger.records("execution_receipt_binding")
    if not records:
        raise ProviderError("Forge did not persist an execution receipt binding")
    binding = records[-1].payload.to_object_dict()
    receipt = binding.get("mncs_receipt")
    if (binding.get("receipt_completeness") != "complete"
            or not isinstance(receipt, dict)
            or receipt.get("record_type") != "mncs-execution-receipt"
            or receipt.get("schema_version") != "0.1-experimental"):
        raise ProviderError("Forge did not produce a complete measured execution receipt")
    identity = receipt.get("receipt_identity")
    if (not isinstance(identity, str) or identity != canonical_sha256(
            {key: value for key, value in receipt.items() if key != "receipt_identity"})):
        raise ProviderError("Forge receipt identity failed canonical verification")
    extensions = receipt.get("extensions")
    local = extensions.get("forge:local-process") if isinstance(extensions, dict) else None
    resource = local.get("resource_observations") if isinstance(local, dict) else None
    observations = resource.get("resource_observations") if isinstance(resource, dict) else None
    envelope = local.get("resource_envelope") if isinstance(local, dict) else None
    if (not isinstance(observations, dict)
            or any(type(observations.get(key)) is not int for key in (
                "cgroup_memory_peak_bytes", "cpu_time_microseconds", "process_count_peak"))
            or not isinstance(envelope, dict)
            or envelope.get("memory_max_bytes") != RESOURCE_ENVELOPE["memory_max_bytes"]):
        raise ProviderError("Forge receipt lacks the required envelope or cgroup measurements")
    return binding, receipt


def main(argv: list[str] | None = None) -> int:
    output: Path | None = None
    runtime_root: Path | None = None
    forge: Forge | None = None
    try:
        output = output_directory()
        clear_prior_outputs(output)
        build, input_identity = input_build()
        call = read_call(list(sys.argv[1:] if argv is None else argv), build)
        runtime = selected_runtime()
        build_producer = build.get("producer")
        probe_details = build_producer.get("probe") if isinstance(build_producer, dict) else None
        build_probe_sha = probe_details.get("probe_executable_sha256") if isinstance(probe_details, dict) else None
        if build_probe_sha != "sha256:" + runtime["compiler"]["probe_sha256"]:
            raise ProviderError("selected Compiler probe differs from the verified build producer")
        entry = build["entry"]
        artifact = build["artifact"]
        application_identity = {
            "build_capability": input_identity["capability"],
            "build_binding_id": input_identity["binding_id"],
            "build_store_reference": input_identity["store_reference"],
            "build_response_sha256": input_identity["sha256"],
            "source_snapshot_identity": build["source_snapshot"]["identity"],
            "dependency_graph_identity": build["dependency_graph"]["identity"],
            "compiler_producer_identity": runtime["compiler"]["producer_identity"],
            "compiler_checkout_revision": runtime["compiler"]["checkout_revision"],
            "compiler_probe_sha256": runtime["compiler"]["probe_sha256"],
            "artifact_identity": artifact["identity"],
            "artifact_sha256": hashlib.sha256(json_bytes(artifact["value"])).hexdigest(),
            "interface_identity": build["entry"]["interface"]["identity"],
            "entry": {"module": entry["module"], "function": entry["function"]},
        }
        call_identity = "sha256:" + canonical_sha256({
            "application": application_identity,
            "arguments": call["arguments"],
        })
        subject_identity = "sha256:" + canonical_sha256({
            "schema_version": "mncs.forge.native-application-subject/1",
            "application": application_identity,
            "call_identity": call_identity,
            "vm_identity": runtime["vm"]["runtime_identity"],
        })
        cache_root = ROOT / ".mncs" / "cache" / "native-application-executions"
        cache_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        cache_state = cache_root.stat()
        if cache_state.st_uid != os.getuid() or cache_state.st_mode & 0o077:
            raise ProviderError("Forge native-application execution cache is not private")
        compiler_vm_cache = cache_root / "compiler-vm"
        compiler_vm_cache.mkdir(mode=0o700, exist_ok=True)
        compiler_vm_cache_state = compiler_vm_cache.stat()
        if (compiler_vm_cache_state.st_uid != os.getuid()
                or compiler_vm_cache_state.st_mode & 0o077):
            raise ProviderError("Forge canonical compiler/VM cache is not private")
        os.environ["MNCS_VM_ARTIFACT_CACHE"] = str(compiler_vm_cache)
        runtime_root = Path(tempfile.mkdtemp(prefix="execution-", dir=cache_root))
        config = load_config(ROOT / "mncs-forge.compiler-vm.toml")
        config.raw["project"]["identity"] = subject_identity
        config = dataclasses.replace(config, root=runtime_root / "forge-project")
        config.root.mkdir(mode=0o700, parents=True)
        runner = LocalProcessRunner(process_capability=None)
        local_store = LocalRecordStore(runtime_root / "forge-records")
        forge = Forge(config, mode="development", record_store=local_store, runner=runner)
        lock_path = cache_root / "verification.lock"
        envelope = SystemdCgroupEnvelope(
            RESOURCE_ENVELOPE, required=True, lock_path=lock_path,
            environment_dir=cache_root, resource_semantics=forge._resource_semantics,
        )
        if not envelope.available:
            raise ProviderError(
                "Forge cgroup-v2 resource envelope is unavailable: "
                + str(envelope.status().get("limitation") or envelope.status().get("state"))
            )
        runner.resource_envelope = envelope
        invocation_root = runtime_root / "invocation"
        invocation_root.mkdir(mode=0o700)
        artifact_path = invocation_root / "application.vm.json"
        arguments_path = invocation_root / "arguments.json"
        type_arguments_path = invocation_root / "type-arguments.json"
        artifact_path.write_bytes(json_bytes(artifact["value"]))
        arguments_path.write_bytes(json_bytes(call["arguments"]))
        type_arguments_path.write_bytes(b"[]")
        callable_name = f"{entry['module']}::{entry['function']}"
        workflow = config.workflows["compiler-vm-work"]
        child_environment = {key: os.environ[key] for key in ("PATH", "LANG", "LC_ALL") if key in os.environ}
        config.workflows["compiler-vm-work"] = dataclasses.replace(
            workflow,
            command=[
                sys.executable, str(ROOT / "scripts" / "native_application_vm_task.py"),
                "--vm", os.environ["MNCS_VM_BIN"], "--artifact", str(artifact_path),
                "--arguments", str(arguments_path), "--type-arguments", str(type_arguments_path),
                "--callable", callable_name,
                "--expected-artifact-identity", artifact["identity"],
            ],
            environment=child_environment,
        )
        run_result = forge.development_checks_run(["compiler-vm-work"])
        results = run_result.get("results")
        if (run_result.get("aggregate_status") != "PASS"
                or not isinstance(results, list) or len(results) != 1
                or not isinstance(results[0], dict)):
            summary = {
                "schema_version": "mncs.forge.native-application-failure/1",
                "capability": CAPABILITY,
                "status": str(run_result.get("aggregate_status", "UNKNOWN")),
                "application": application_identity,
                "call_identity": call_identity,
                "forge_results": results[:1] if isinstance(results, list) else [],
                "resource_envelope": RESOURCE_ENVELOPE,
            }
            write_output(output, FAILURE_FILE, summary)
            print(json.dumps(summary, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
            return 1
        witnesses = results[0].get("witnesses_or_counterexamples")
        if (not isinstance(witnesses, list) or len(witnesses) != 1
                or not isinstance(witnesses[0], dict)):
            raise ProviderError("Forge did not retain one canonical VM result witness")
        witness = witnesses[0]
        execution = witness.get("execution")
        if (witness.get("artifact_identity") != artifact["identity"]
                or witness.get("callable") != callable_name
                or not isinstance(execution, dict)
                or execution.get("record", {}).get("artifact_id") != artifact["identity"]):
            raise ProviderError("Forge witness does not match the selected app, call, and artifact")
        binding, receipt = receipt_from_forge(forge)
        receipt_subject = receipt.get("subject")
        if (not isinstance(receipt_subject, dict)
                or receipt_subject.get("canonical_sha256") != subject_identity.removeprefix("sha256:")):
            raise ProviderError("Forge execution receipt subject differs from the selected app call")
        local = receipt["extensions"]["forge:local-process"]
        result_document = {
            "schema_version": "mncs.forge.native-application-execution/1",
            "capability": CAPABILITY,
            "status": "completed",
            "call_identity": call_identity,
            "application": application_identity,
            "runtime": runtime["vm"],
            "typed_call": call,
            "execution": execution,
            "forge": {
                "action_identity": binding.get("action_identity"),
                "execution_receipt_identity": receipt["receipt_identity"],
                "receipt_completeness": binding["receipt_completeness"],
                "resource_envelope": local["resource_envelope"],
                "resource_observations": local["resource_observations"]["resource_observations"],
            },
            "limitations": results[0].get("limitations", []),
        }
        write_output(output, SUCCESS_FILES[0], result_document)
        write_output(output, SUCCESS_FILES[1], receipt)
        print(json.dumps({
            "schema_version": result_document["schema_version"],
            "status": "completed",
            "call_identity": call_identity,
            "application_artifact_identity": artifact["identity"],
            "forge_execution_receipt_identity": receipt["receipt_identity"],
            "resource_envelope": local["resource_envelope"],
            "resource_observations": local["resource_observations"]["resource_observations"],
        }, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        return 0
    except (OSError, ValueError, KeyError, ProviderError, subprocess.SubprocessError) as error:
        failure = {
            "schema_version": "mncs.forge.native-application-failure/1",
            "capability": CAPABILITY,
            "status": "failed",
            "reason": str(error)[:1200],
        }
        if output is not None:
            try:
                clear_prior_outputs(output)
                write_output(output, FAILURE_FILE, failure)
            except (OSError, ProviderError):
                pass
        print(json.dumps(failure, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        return 1
    finally:
        if forge is not None:
            forge.close()
        if runtime_root is not None:
            shutil.rmtree(runtime_root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
