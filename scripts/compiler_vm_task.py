#!/usr/bin/env python3
"""Run one bounded selected Compiler-to-VM task for the Forge workflow."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mncs_forge.compiler_vm import CanonicalVmApplication  # noqa: E402

SOURCE = ROOT / "examples/compiler-vm/steady_state.mncs"
CALLS = ROOT / "examples/compiler-vm/calls.json"


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def tree_sha256(root: Path) -> str:
    files = {
        path.relative_to(root).as_posix(): file_sha256(path)
        for path in root.rglob("*.mncs")
        if path.is_file() and not path.is_symlink()
    }
    return canonical_identity(files)


def git_head(root: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
    ).stdout.strip()


def canonical_identity(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--intent-identity", required=True)
    parser.add_argument("--subject-identity", required=True)
    parser.add_argument("--cache", required=True)
    parser.add_argument("--decision-identity")
    args = parser.parse_args(argv)

    app: CanonicalVmApplication | None = None
    report: dict[str, object] = {
        "schema_version": "mncs.forge.compiler-vm-task/1",
        "status": "FAIL",
        "witnesses": [],
        "limitations": [],
    }
    try:
        compiler_checkout = Path(os.environ["MNCS_COMPILER_CHECKOUT"]).resolve(strict=True)
        compiler_probe = Path(os.environ["MNCS_COMPILER_PROBE"]).resolve(strict=True)
        vm_checkout = Path(os.environ["MNCS_VM_CHECKOUT"]).resolve(strict=True)
        vm_bin = Path(os.environ["MNCS_VM_BIN"]).resolve(strict=True)
        language_checkout = Path(os.environ["MNCS_LANGUAGE_CHECKOUT"]).resolve(strict=True)
        reference_cli = Path(os.environ["MNCS_REFERENCE_BIN"]).resolve(strict=True)
        stdlib_checkout = Path(os.environ["MNCS_STDLIB_CHECKOUT"]).resolve(strict=True)
        library_root = (stdlib_checkout / "library").resolve(strict=True)
        if not all(path.is_file() for path in (compiler_probe, vm_bin, SOURCE, CALLS)):
            raise RuntimeError("selected toolchain or bounded Compiler fixture is unavailable")

        producer = json.loads(
            subprocess.run(
                [str(compiler_probe), "--producer-info"],
                check=True,
                capture_output=True,
                text=True,
                timeout=10,
            ).stdout
        )
        if producer.get("identity") != "sha256:" + canonical_identity(producer.get("receipt")):
            raise RuntimeError("selected Compiler producer identity does not match its receipt")

        call_requests = json.loads(CALLS.read_text(encoding="utf-8"))
        if not isinstance(call_requests, list) or len(call_requests) != 1:
            raise RuntimeError("Compiler fixture must contain exactly one bounded call")

        subject_material = {
            "schema_version": "mncs.compiler-vm-subject/1",
            "intent_identity": args.intent_identity,
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
            "automation_decision_identity": args.decision_identity,
        }
        subject_identity = "sha256:" + canonical_identity(subject_material)
        if subject_identity != args.subject_identity:
            raise RuntimeError("Compiler task inputs changed after Forge admission")

        app = CanonicalVmApplication(
            compiler_checkout=compiler_checkout,
            compiler_executable=compiler_probe,
            vm_checkout=vm_checkout,
            executable=vm_bin,
            cache=Path(args.cache),
            timeout=120,
        )
        provider_state = app.producer.inspect()
        if provider_state.get("state") != "ready":
            raise RuntimeError("selected Stage-0 Compiler producer is not ready")

        result = app.call(SOURCE, call_requests[0])
        execution = result.get("execution")
        returned = execution.get("returned") if isinstance(execution, dict) else None
        if not isinstance(returned, list) or len(returned) != 1:
            raise RuntimeError("selected Compiler and VM returned an unexpected result shape")
        integer = returned[0].get("integer") if isinstance(returned[0], dict) else None
        value = integer.get("value") if isinstance(integer, dict) else None
        if value != 7:
            raise RuntimeError(f"Compiler result differed from the fixture expectation: {value!r}")

        product = app.product
        if not isinstance(product, dict):
            raise RuntimeError("Compiler provider did not retain its build product")
        artifact = product.get("artifact")
        build_receipt = product.get("build_receipt")
        provenance = result.get("execution_provenance")
        if not all(isinstance(item, dict) for item in (artifact, build_receipt, provenance)):
            raise RuntimeError("Compiler or VM artifact provenance is incomplete")

        task = {
            **subject_material,
            "compiler_subject_identity": subject_identity,
            "compiler_build_receipt_identity": build_receipt.get("identity"),
            "compiler_artifact_identity": artifact.get("identity"),
            "compiler_artifact_sha256": artifact.get("sha256"),
            "vm_execution_identity": provenance.get("identity"),
            "vm_artifact_identity": provenance.get("core", {}).get("artifact_identity"),
            "vm_executor_sha256": provenance.get("core", {}).get("executor_sha256"),
            "intent_identity": args.intent_identity,
            "automation_decision_identity": args.decision_identity,
            "result": {
                "module": "proof.integration.v1",
                "function": "value",
                "integer": value,
            },
        }
        report = {
            "schema_version": "mncs.forge.compiler-vm-task/1",
            "status": "PASS",
            "witnesses": [task],
            "limitations": [
                "One pure function and one integer result were exercised; "
                "this is a bounded integration proof."
            ],
        }
        print(json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        return 0
    except (OSError, KeyError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        report["limitations"] = [str(error)[:512]]
        print(json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        return 1
    finally:
        if app is not None:
            app.close()


if __name__ == "__main__":
    raise SystemExit(main())
