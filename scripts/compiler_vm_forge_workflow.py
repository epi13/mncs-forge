#!/usr/bin/env python3
"""Admit a real selected Compiler task through Forge and retain its receipt."""

from __future__ import annotations

import argparse
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

SOURCE = ROOT / "examples/compiler-vm/steady_state.mncs"
CALLS = ROOT / "examples/compiler-vm/calls.json"
INTENT_READY = "compiler-vm-followup-v1"
RESOURCE_ENVELOPE = {
    "memory_max_bytes": 1_073_741_824,
    "memory_cap_bytes": 1_073_741_824,
    "pids_limit": 64,
    "concurrency_limit": 1,
    "runtime_max_seconds": 180,
}


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def tree_sha256(root: Path) -> str:
    files = {
        path.relative_to(root).as_posix(): file_sha256(path)
        for path in root.rglob("*.mncs")
        if path.is_file() and not path.is_symlink()
    }
    return canonical_sha256(files)


def git_head(root: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
    ).stdout.strip()


def selected_subject(intent_identity: str, decision_identity: str | None) -> dict[str, Any]:
    compiler_checkout = Path(os.environ["MNCS_COMPILER_CHECKOUT"]).resolve(strict=True)
    compiler_probe = Path(os.environ["MNCS_COMPILER_PROBE"]).resolve(strict=True)
    vm_checkout = Path(os.environ["MNCS_VM_CHECKOUT"]).resolve(strict=True)
    vm_bin = Path(os.environ["MNCS_VM_BIN"]).resolve(strict=True)
    language_checkout = Path(os.environ["MNCS_LANGUAGE_CHECKOUT"]).resolve(strict=True)
    reference_cli = Path(os.environ["MNCS_REFERENCE_BIN"]).resolve(strict=True)
    stdlib_checkout = Path(os.environ["MNCS_STDLIB_CHECKOUT"]).resolve(strict=True)
    library_root = (stdlib_checkout / "library").resolve(strict=True)
    if not reference_cli.is_file() or not (library_root / "core/logic.mncs").is_file():
        raise RuntimeError("selected Language CLI or standard library is unavailable")
    producer = json.loads(
        subprocess.run(
            [str(compiler_probe), "--producer-info"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout
    )
    return {
        "schema_version": "mncs.compiler-vm-subject/1",
        "intent_identity": intent_identity,
        "source_sha256": file_sha256(SOURCE),
        "calls_sha256": file_sha256(CALLS),
        "compiler_checkout_head": git_head(compiler_checkout),
        "compiler_producer_identity": producer["identity"],
        "compiler_executable_sha256": file_sha256(compiler_probe),
        "vm_checkout_head": git_head(vm_checkout),
        "vm_executable_sha256": file_sha256(vm_bin),
        "language_checkout_head": git_head(language_checkout),
        "language_cli_sha256": file_sha256(reference_cli),
        "stdlib_checkout_head": git_head(stdlib_checkout),
        "stdlib_library_identity": tree_sha256(library_root),
        "automation_decision_identity": decision_identity,
    }


def input_decision() -> tuple[dict[str, Any], str]:
    raw_manifest = os.environ.get("MNCS_ENV_INPUT_ARTIFACTS_JSON", "")
    raw_root = os.environ.get("MNCS_ENV_INPUT_ARTIFACT_DIR", "")
    if not raw_manifest or not raw_root:
        raise RuntimeError("scheduled Compiler work requires an Environment decision artifact")
    manifest = json.loads(raw_manifest)
    rows = manifest.get("artifacts") if isinstance(manifest, dict) else None
    if (
        not isinstance(rows, list)
        or len(rows) != 2
        or any(not isinstance(row, dict) for row in rows)
    ):
        raise RuntimeError(
            "scheduled Compiler work requires the decision and measured Forge receipt"
        )
    by_name = {row.get("name"): row for row in rows}
    decision_row = by_name.get("automation-decision")
    receipt_row = by_name.get("forge-receipt")
    if (
        not isinstance(decision_row, dict)
        or decision_row.get("capability")
        != "mncs-automation:automation-resource-headroom-evaluation"
        or decision_row.get("path") != "resource-headroom-decision.json"
        or not isinstance(receipt_row, dict)
        or receipt_row.get("capability") != "mncs-forge:provider-artifact-execution"
        or receipt_row.get("path") != "forge-execution-receipt.json"
    ):
        raise RuntimeError("scheduled Compiler work received unexpected source artifacts")
    root = Path(raw_root).resolve(strict=True)

    def read_staged(row: dict[str, Any], label: str) -> bytes:
        local = (root / str(row.get("local_path", ""))).resolve(strict=True)
        if not local.is_relative_to(root) or not local.is_file():
            raise RuntimeError(f"scheduled Compiler {label} escapes its staged input")
        data = local.read_bytes()
        if len(data) != row.get("byte_count") or hashlib.sha256(data).hexdigest() != row.get(
            "sha256"
        ):
            raise RuntimeError(f"scheduled Compiler {label} bytes do not match Store provenance")
        return data

    decision_bytes = read_staged(decision_row, "decision")
    receipt_bytes = read_staged(receipt_row, "Forge receipt")
    decision = json.loads(decision_bytes)
    receipt = json.loads(receipt_bytes)
    evidence = decision.get("evidence") if isinstance(decision, dict) else None
    provenance = decision.get("input_artifact") if isinstance(decision, dict) else None
    subject = receipt.get("subject") if isinstance(receipt, dict) else None
    extensions = receipt.get("extensions") if isinstance(receipt, dict) else None
    local_process = extensions.get("forge:local-process") if isinstance(extensions, dict) else None
    resource = (
        local_process.get("resource_observations") if isinstance(local_process, dict) else None
    )
    observations = resource.get("resource_observations") if isinstance(resource, dict) else None
    envelope = local_process.get("resource_envelope") if isinstance(local_process, dict) else None
    receipt_identity = receipt.get("receipt_identity") if isinstance(receipt, dict) else None
    if (
        not isinstance(receipt, dict)
        or receipt.get("record_type") != "mncs-execution-receipt"
        or receipt.get("schema_version") != "0.1-experimental"
        or not isinstance(receipt_identity, str)
        or canonical_sha256(
            {key: value for key, value in receipt.items() if key != "receipt_identity"}
        )
        != receipt_identity
        or not isinstance(subject, dict)
        or not isinstance(observations, dict)
        or not isinstance(envelope, dict)
    ):
        raise RuntimeError(
            "scheduled Compiler work received a malformed or unverified Forge receipt"
        )
    if (
        not isinstance(evidence, dict)
        or decision.get("condition") != "true"
        or decision.get("condition_value") != 1
        or evidence.get("status") != "ready"
        or evidence.get("intent_identity") != INTENT_READY
        or evidence.get("measurement_execution") != receipt_identity
        or evidence.get("measurement_subject_identity") != subject.get("canonical_sha256")
        or evidence.get("measured_peak_bytes") != observations.get("cgroup_memory_peak_bytes")
        or evidence.get("measurement_envelope_bytes") != envelope.get("memory_max_bytes")
        or not isinstance(provenance, dict)
        or provenance.get("capability") != "mncs-system-monitor:system-resource-sample"
    ):
        raise RuntimeError("Automation decision is not ready for this measured Compiler intent")
    required_memory = evidence.get("required_memory_bytes")
    if (
        type(required_memory) is not int
        or not 64 * 1024 * 1024 <= required_memory <= RESOURCE_ENVELOPE["memory_max_bytes"]
    ):
        raise RuntimeError("Automation decision exceeds Forge's bounded next-task memory envelope")
    return decision, str(decision_row["sha256"])


def write_output(name: str, value: object) -> None:
    raw_directory = os.environ.get("MNCS_ENV_SESSION_ARTIFACT_DIR", "")
    if not raw_directory:
        raise RuntimeError("Environment invocation artifact directory is unavailable")
    directory = Path(raw_directory).resolve(strict=True)
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    if len(encoded) > 512 * 1024:
        raise RuntimeError(f"Environment artifact exceeds its byte bound: {name}")
    destination = directory / name
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(destination, flags, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(encoded)
        stream.flush()
        os.fsync(stream.fileno())


def verification_lock_path() -> Path:
    directory = ROOT / ".mncs/cache/resource-runtime"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    state = directory.stat()
    if state.st_uid != os.getuid() or state.st_mode & 0o077:
        raise RuntimeError("Forge verification lock directory is not private to this user")
    return directory / "verification.lock"


def receipt_from_forge(forge: Forge) -> tuple[dict[str, Any], dict[str, Any]]:
    records = forge.ledger.records("execution_receipt_binding")
    if not records:
        raise RuntimeError("Forge did not persist an execution receipt binding")
    binding = records[-1].payload.to_object_dict()
    receipt = binding.get("mncs_receipt")
    if (
        binding.get("receipt_completeness") != "complete"
        or not isinstance(receipt, dict)
        or receipt.get("record_type") != "mncs-execution-receipt"
        or receipt.get("schema_version") != "0.1-experimental"
    ):
        raise RuntimeError("Forge did not produce a complete MNCS execution receipt")
    extensions = receipt.get("extensions")
    local = extensions.get("forge:local-process") if isinstance(extensions, dict) else None
    if not isinstance(local, dict):
        raise RuntimeError("Forge receipt omitted its local process observations")
    resource = local.get("resource_observations")
    observations = resource.get("resource_observations") if isinstance(resource, dict) else None
    envelope = local.get("resource_envelope")
    required_observations = (
        "cgroup_memory_peak_bytes",
        "cpu_time_microseconds",
        "process_count_peak",
    )
    if (
        not isinstance(observations, dict)
        or any(type(observations.get(key)) is not int for key in required_observations)
        or any(observations[key] < 0 for key in required_observations)
        or not isinstance(envelope, dict)
        or type(envelope.get("memory_max_bytes")) is not int
        or envelope.get("memory_max_bytes", 0) <= 0
    ):
        raise RuntimeError("Forge receipt lacks measured cgroup resources or a declared envelope")
    receipt_identity = receipt.get("receipt_identity")
    if not isinstance(receipt_identity, str) or len(receipt_identity) != 64:
        raise RuntimeError("Forge receipt identity is malformed")
    return binding, receipt


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scheduled", action="store_true")
    args = parser.parse_args(argv)

    decision: dict[str, Any] | None = None
    decision_identity: str | None = None
    intent_identity = "compiler-vm-steady-state-v1"
    if args.scheduled:
        try:
            decision, decision_identity = input_decision()
        except (OSError, ValueError, KeyError, RuntimeError) as error:
            print(
                json.dumps(
                    {
                        "schema_version": "mncs.forge.compiler-vm-admission/1",
                        "status": "DEFERRED",
                        "reason": str(error)[:256],
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
            return 0
        intent_identity = INTENT_READY

    memory_limit = RESOURCE_ENVELOPE["memory_max_bytes"]
    if decision is not None:
        memory_limit = decision["evidence"]["required_memory_bytes"]
    execution_envelope = {
        **RESOURCE_ENVELOPE,
        "memory_max_bytes": memory_limit,
        "memory_cap_bytes": memory_limit,
    }

    runtime_cache: Path | None = None
    try:
        subject_material = selected_subject(intent_identity, decision_identity)
        subject_identity = "sha256:" + canonical_sha256(subject_material)
        cache_root = ROOT / ".mncs/cache/compiler-vm-loop"
        cache_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        runtime_cache = Path(
            tempfile.mkdtemp(prefix="execution-", dir=cache_root)
        )
        policy_cache = runtime_cache / "forge-policy"
        native_cache = runtime_cache / "forge-native"
        task_cache = runtime_cache / "compiler-vm-task"
        os.environ["MNCS_VM_ARTIFACT_CACHE"] = str(policy_cache)
        os.environ["MNCS_NATIVE_APPLICATION_CACHE_DIR"] = str(native_cache)
        os.environ["MNCS_LANGUAGE_ROOT"] = str(
            Path(os.environ["MNCS_LANGUAGE_CHECKOUT"]).resolve(strict=True)
        )
        os.environ["MNCS_LIBRARY_ROOT"] = str(
            Path(os.environ["MNCS_STDLIB_CHECKOUT"]).resolve(strict=True) / "library"
        )
        os.environ["MNCS_CLI"] = str(Path(os.environ["MNCS_REFERENCE_BIN"]).resolve(strict=True))

        config = load_config(ROOT / "mncs-forge.compiler-vm.toml")
        config.raw["project"]["identity"] = subject_identity
        config = dataclasses.replace(config, root=runtime_cache / "forge-project")
        config.root.mkdir(mode=0o700, parents=True)
        runner = LocalProcessRunner(process_capability=None)
        local_store = LocalRecordStore(runtime_cache / "forge-records")
        forge: Forge | None = None
        try:
            forge = Forge(config, mode="development", record_store=local_store, runner=runner)
            lock_path = verification_lock_path()
            envelope = SystemdCgroupEnvelope(
                execution_envelope,
                required=True,
                lock_path=lock_path,
                environment_dir=lock_path.parent,
                resource_semantics=forge._resource_semantics,
            )
            if not envelope.available:
                raise RuntimeError(
                    "Forge cgroup-v2 resource envelope is unavailable: "
                    + str(envelope.status().get("limitation") or envelope.status().get("state"))
                )
            runner.resource_envelope = envelope

            workflow = config.workflows["compiler-vm-work"]
            task_command = [
                sys.executable,
                str(ROOT / "scripts/compiler_vm_task.py"),
                "--intent-identity",
                intent_identity,
                "--subject-identity",
                subject_identity,
                "--cache",
                str(task_cache),
            ]
            if decision_identity is not None:
                task_command.extend(("--decision-identity", decision_identity))
            child_environment = {
                key: os.environ[key]
                for key in (
                    "PATH",
                    "LANG",
                    "LC_ALL",
                    "MNCS_COMPILER_CHECKOUT",
                    "MNCS_COMPILER_PROBE",
                    "MNCS_VM_CHECKOUT",
                    "MNCS_VM_BIN",
                    "MNCS_LANGUAGE_CHECKOUT",
                    "MNCS_REFERENCE_BIN",
                    "MNCS_STDLIB_CHECKOUT",
                )
                if key in os.environ
            }
            config.workflows["compiler-vm-work"] = dataclasses.replace(
                workflow,
                command=task_command,
                environment=child_environment,
            )
            run = forge.development_checks_run(["compiler-vm-work"])
            results = run.get("results")
            if run.get("aggregate_status") != "PASS":
                summaries = [
                    {
                        key: result.get(key)
                        for key in (
                            "status",
                            "returncode",
                            "witnesses_or_counterexamples",
                            "limitations",
                            "stderr_diagnostic",
                        )
                    }
                    for result in results[:1]
                    if isinstance(result, dict)
                ] if isinstance(results, list) else []
                failure = {
                    "schema_version": "mncs.forge.compiler-vm-failure/1",
                    "aggregate_status": run.get("aggregate_status"),
                    "results": summaries,
                }
                write_output("forge-failure.json", failure)
                diagnostic = json.dumps(
                    failure, sort_keys=True, separators=(",", ":")
                )[:1536]
                raise RuntimeError("Forge workflow did not return PASS: " + diagnostic)
            if (
                not isinstance(results, list)
                or len(results) != 1
                or not isinstance(results[0], dict)
            ):
                raise RuntimeError("Forge workflow result record is unavailable")
            task_witnesses = results[0].get("witnesses_or_counterexamples")
            if not isinstance(task_witnesses, list) or len(task_witnesses) != 1:
                raise RuntimeError("Forge workflow did not retain the Compiler subject witness")
            task = task_witnesses[0]
            if (
                not isinstance(task, dict)
                or task.get("compiler_subject_identity") != subject_identity
            ):
                raise RuntimeError(
                    "Forge workflow witness does not match its admitted Compiler subject"
                )
            binding, receipt = receipt_from_forge(forge)
            receipt_subject = receipt.get("subject")
            if (
                not isinstance(receipt_subject, dict)
                or receipt_subject.get("canonical_sha256")
                != subject_identity.removeprefix("sha256:")
            ):
                raise RuntimeError(
                    "Forge receipt subject does not match the selected Compiler task"
                )
            extensions = receipt["extensions"]["forge:local-process"]
            observations = extensions["resource_observations"]["resource_observations"]
            if extensions["resource_envelope"].get("memory_max_bytes") != memory_limit:
                raise RuntimeError(
                    "Forge receipt does not match the Automation admitted memory envelope"
                )
            output = {
                "schema_version": "mncs.forge.compiler-vm-execution/1",
                "status": "PASS",
                "intent_identity": intent_identity,
                "compiler_subject_identity": subject_identity,
                "automation_decision_identity": decision_identity,
                "automation_measurement_execution": (
                    decision.get("evidence", {}).get("measurement_execution")
                    if decision is not None
                    else None
                ),
                "forge_action_identity": binding.get("action_identity"),
                "forge_execution_identity": receipt["receipt_identity"],
                "forge_receipt_completeness": binding.get("receipt_completeness"),
                "resource_envelope": extensions["resource_envelope"],
                "resource_observations": observations,
                "task": task,
                "decision": decision.get("evidence") if decision is not None else None,
                "limitations": results[0].get("limitations", []),
            }
            write_output("compiler-vm-task.json", task)
            write_output("forge-execution-receipt.json", receipt)
            print(json.dumps(output, sort_keys=True, separators=(",", ":")))
            return 0
        finally:
            if forge is not None:
                forge.close()
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError) as error:
        print(
            json.dumps(
                {
                    "schema_version": "mncs.forge.compiler-vm-execution/1",
                    "status": "FAIL",
                    "reason": str(error)[:512],
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return 1
    finally:
        if runtime_cache is not None:
            shutil.rmtree(runtime_cache, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
