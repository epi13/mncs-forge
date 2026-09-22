#!/usr/bin/env python3
"""Small Provider Protocol 0.1 fixture."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path


def main() -> int:
    mode = sys.argv[1]
    request = json.loads(sys.stdin.readline())
    if mode == "TIMEOUT":
        time.sleep(5)
        return 0
    if mode == "MALFORMED":
        print("not-json")
        return 0
    if mode == "OVERSIZE":
        print("x" * 100_000)
        return 0
    if mode == "MULTIPLE":
        print("{}")
        print("{}")
        return 0
    if mode == "NONZERO":
        print("provider failed", file=sys.stderr)
        return 7
    if mode == "STDERR":
        print("diagnostic-" * 10_000, file=sys.stderr)
        return 0
    if mode == "ZERO_UNKNOWN":
        return 0
    identity = "drifted-provider-identity" if mode == "IDENTITY_DRIFT" else f"fake-{mode.lower()}"
    security = mode.startswith("SECURITY_")
    capabilities = ["bounded-structural", "security-micro"] if security else ["bounded-structural"]
    provider = {
        "id": f"fake-{mode.lower()}",
        "name": f"fake-{mode.lower()}",
        "version": "1",
        "identity": identity,
    }
    if request["type"] == "capabilities":
        response = {
            "protocol_version": "0.1",
            "type": "capabilities",
            "provider": provider,
            "analyses": capabilities,
            "statuses": ["PASS", "FAIL", "UNKNOWN"],
            "cancellation": False,
            "health_checks": True,
            "extensions": {
                "supported_constructs": ["direct-calls"],
                "unsupported_constructs": ["dynamic-dispatch"],
                "limitations": ["fixture provider"],
            },
        }
        print(json.dumps(response, sort_keys=True, separators=(",", ":")))
        return 0
    status = (
        "UNKNOWN"
        if mode == "IDENTITY_DRIFT"
        else "PASS"
        if mode == "SECURITY_PASS"
        else mode
    )
    if mode == "PROTECTED_CHECK":
        status = "FAIL" if Path("protected/holdout.txt").exists() else "PASS"
    if mode == "WITNESS":
        status = "FAIL"
    dependency_paths = ["reference/reference.py"] if security else ["candidate/main.py"]
    response = {
        "protocol_version": "0.1",
        "type": "analysis_response",
        "request_id": request["request_id"],
        "provider": provider,
        "status": status,
        "summary": f"bounded security fixture {mode}" if security else f"fixture {mode}",
        "witnesses": (
            [{"location": "candidate/main.py:1", "detail": "x" * 48} for _ in range(20)]
            if mode == "WITNESS"
            else [{"location": "candidate/main.py:1"}]
            if mode == "FAIL"
            else []
        ),
        "limitations": ["fixture cannot resolve dynamic behavior"] if mode == "UNKNOWN" else [],
        "extensions": {
            "unsupported": ["dynamic-dispatch"] if mode == "UNKNOWN" else [],
            "mncs_forge": {
                "assumptions": ["fixture request is bounded"],
                "dependency_envelope": {
                    "paths": dependency_paths,
                    "identities": {},
                    "complete": True,
                },
            },
        },
    }
    print(json.dumps(response, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
