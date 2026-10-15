#!/usr/bin/env python3
"""Run one identity-bound application call through the selected canonical VM."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any


def canonical_identity(value: object) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def run(argv: list[str], *, timeout: float) -> tuple[dict[str, Any], str]:
    completed = subprocess.run(
        argv, capture_output=True, text=True, timeout=timeout, check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"canonical VM command failed ({completed.returncode}): "
            + (completed.stderr or completed.stdout)[-1800:]
        )
    try:
        document = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError("canonical VM returned invalid JSON") from error
    if not isinstance(document, dict):
        raise RuntimeError("canonical VM returned a non-object result")
    return document, completed.stderr[-1024:]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vm", required=True)
    parser.add_argument("--artifact", required=True)
    parser.add_argument("--arguments", required=True)
    parser.add_argument("--type-arguments", required=True)
    parser.add_argument("--callable", required=True)
    parser.add_argument("--expected-artifact-identity", required=True)
    args = parser.parse_args(argv)
    try:
        vm = Path(args.vm).resolve(strict=True)
        artifact = Path(args.artifact).resolve(strict=True)
        arguments = Path(args.arguments).resolve(strict=True)
        type_arguments = Path(args.type_arguments).resolve(strict=True)
        if (not vm.is_file() or not artifact.is_file() or not arguments.is_file()
                or not type_arguments.is_file()):
            raise RuntimeError("selected VM, artifact, or typed arguments are unavailable")
        admitted, _ = run([str(vm), "admit", "--artifact", str(artifact)], timeout=10)
        if (admitted.get("status") != "admitted"
                or admitted.get("artifact_id") != args.expected_artifact_identity):
            raise RuntimeError("canonical VM refused or changed the Compiler artifact identity")
        document, stderr = run([
            str(vm), "run", "--artifact", str(artifact), "--callable", args.callable,
            "--args", str(arguments), "--type-args", str(type_arguments),
        ], timeout=40)
        outcome = document.get("outcome")
        record = document.get("record")
        returned = record.get("returned") if isinstance(record, dict) else None
        if outcome != {"kind": "completed"}:
            raise RuntimeError(
                "native application did not complete: "
                + json.dumps(outcome, ensure_ascii=False, sort_keys=True)[:1200]
            )
        if (not isinstance(record, dict)
                or record.get("artifact_id") != args.expected_artifact_identity
                or not isinstance(returned, list) or len(returned) != 1
                or type(record.get("usage", {}).get("steps")) is not int):
            raise RuntimeError("VM result did not preserve the selected artifact and one typed return")
        witness = {
            "schema_version": "mncs.forge.native-application-vm-witness/1",
            "callable": args.callable,
            "artifact_identity": args.expected_artifact_identity,
            "call_result_identity": canonical_identity(document),
            "execution": document,
        }
        report = {
            "schema_version": "mncs.forge.native-application-task/1",
            "status": "PASS",
            "provider": {"id": "mncs-forge.native-application.vm", "kind": "canonical-runtime"},
            "witnesses": [witness],
            "limitations": [],
        }
        print(json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        return 0
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError) as error:
        report = {
            "schema_version": "mncs.forge.native-application-task/1",
            "status": "FAIL",
            "provider": {"id": "mncs-forge.native-application.vm", "kind": "canonical-runtime"},
            "witnesses": [],
            "limitations": [str(error)[:1024]],
        }
        print(json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
