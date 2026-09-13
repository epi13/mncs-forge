from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def test_micro_verifier_benchmark_smoke() -> None:
    root = Path(__file__).parents[1]
    # Hang guard only: this benchmark asserts structural properties, never
    # absolute timings, so it must tolerate slow native toolchains.
    completed = subprocess.run(
        [
            sys.executable,
            str(root / "scripts/benchmark-micro-verifiers.py"),
            "--iterations",
            "2",
        ],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )
    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    assert result["evidence_class"] == "operator-controlled-development-benchmark"
    assert result["normative"] is False
    assert result["ledger_entries"] >= 6
    assert set(result["metrics"]) == {
        "verifier_list",
        "verifier_match",
        "verifier_run",
        "ledger_verify",
        "state_inspect",
        "verifier_explain",
    }
    for metric in result["metrics"].values():
        assert metric["iterations"] == 2.0
        assert 0 <= metric["p50_ms"] <= metric["max_ms"]
        assert 0 <= metric["p95_ms"] <= metric["max_ms"]
        assert 0 <= metric["p99_ms"] <= metric["max_ms"]
