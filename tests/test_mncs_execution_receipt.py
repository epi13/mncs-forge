from __future__ import annotations

import hashlib
import json
import os
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from mncs_forge.adapters import LocalProcessRunner
from mncs_forge.errors import ForgeError
from mncs_forge.mncs_execution_receipt import ReceiptContext, build_mncs_execution_receipt

SCHEMA_COMMIT = "6d6380016e174feaaa774c1cf0931095d24b5280"
SCHEMA_SHA256 = "f2e1860405052a40b100bead7c27dbe0cc3ac11d03dccca3fcb643b350ecab6e"
ROOT = Path(__file__).parents[1]
SCHEMA_PATH = ROOT / "tests" / "fixtures" / "mncs-execution-receipt-0.1.schema.json"
SIBLING_SCHEMA_PATH = (
    ROOT.parent / "machine-native-complexity-standard" / "schemas" / SCHEMA_PATH.name
)
SIBLING_SOURCE = ROOT.parent / "machine-native-complexity-standard" / "src"


def _schema() -> dict[str, object]:
    content = SCHEMA_PATH.read_bytes()
    assert hashlib.sha256(content).hexdigest() == SCHEMA_SHA256
    if SIBLING_SCHEMA_PATH.exists():
        assert hashlib.sha256(SIBLING_SCHEMA_PATH.read_bytes()).hexdigest() == SCHEMA_SHA256
    value = json.loads(content)
    assert isinstance(value, dict)
    return value


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _context(started: datetime, *, harness_status: str = "UNKNOWN") -> ReceiptContext:
    return ReceiptContext(
        record_id="receipt.task7b1.local",
        subject_family="MNCS",
        subject_kind="measurement",
        subject_record_id="subject.task7b1.local",
        subject_canonical_sha256="a" * 64,
        candidate_id="candidate.task7b1.local",
        test_bundle_identity="b" * 64,
        harness_identity="c" * 64,
        input_snapshot_identity=None,
        execution_policy_identity="d" * 64,
        placement_policy_identity=None,
        result_semantics="runner observation has no semantic harness result",
        challenge_nonce="task7b1-challenge-0123456789",
        challenge_issued_at=_timestamp(started - timedelta(seconds=1)),
        challenge_expires_at=_timestamp(started + timedelta(minutes=1)),
        observed_at=_timestamp(started),
        harness_status=harness_status,  # type: ignore[arg-type]
        command_binding="enforced",
        environment_binding="enforced",
    )


def _observe(runner: LocalProcessRunner, code: str, *, output_cap: int = 128):
    started = datetime.now(UTC)
    observation = runner.observe(
        [sys.executable, "-c", code],
        cwd=Path.cwd(),
        timeout=1,
        output_cap=output_cap,
        stderr_cap=output_cap,
        environment={"PATH": os.environ["PATH"], "MNCS_FORGE_RECEIPT_SECRET": "not-in-output"},
    )
    return observation, _context(started)


def test_pinned_mncs_schema_is_current_and_well_formed() -> None:
    schema = _schema()
    Draft202012Validator.check_schema(schema)
    assert (
        schema["$id"]
        == "https://mncs.dev/schema/experimental/0.1/mncs-execution-receipt.schema.json"
    )
    assert SCHEMA_COMMIT == "6d6380016e174feaaa774c1cf0931095d24b5280"


def test_local_runner_observation_has_complete_stream_facts() -> None:
    runner = LocalProcessRunner()
    observation, _ = _observe(
        runner, "import sys; sys.stdout.write('hello'); sys.stderr.write('err')"
    )

    assert observation.termination_category == "completed"
    assert observation.returncode == 0
    assert observation.stdout.total_bytes == 5
    assert observation.stdout.retained_bytes == 5
    assert observation.stdout.complete_sha256 == observation.stdout.retained_sha256
    assert observation.stderr.total_bytes == 3
    assert observation.aggregate_output.total_bytes == 8
    serialized = json.dumps(observation.to_dict(), sort_keys=True)
    assert "not-in-output" not in serialized


def test_local_runner_capabilities_do_not_claim_isolation() -> None:
    capabilities = LocalProcessRunner().inspect_capabilities()
    assert capabilities.sandbox_isolation == "not-provided"
    assert capabilities.network_isolation == "not-provided"
    assert capabilities.filesystem_isolation == "not-provided"


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("import sys; sys.exit(7)", "nonzero-exit"),
        ("import time; time.sleep(5)", "timeout"),
        ("print('x' * 100000)", "output-limit"),
    ],
)
def test_local_runner_observation_maps_termination_without_rewriting_execute(
    code: str, expected: str
) -> None:
    runner = LocalProcessRunner()
    observation, _ = _observe(runner, code, output_cap=32)

    assert observation.termination_category == expected
    if expected == "output-limit":
        assert observation.stdout.limit_hit is True
        assert observation.stdout.total_bytes is None
        assert observation.stdout.complete_sha256 is None
    if expected == "timeout":
        assert observation.error_code == "TIMEOUT"

    if expected == "nonzero-exit":
        result = runner.execute(
            [sys.executable, "-c", code],
            cwd=Path.cwd(),
            timeout=1,
            output_cap=32,
            environment={"PATH": os.environ["PATH"]},
        )
        assert result.returncode == 7
    else:
        with pytest.raises(ForgeError) as issue:
            runner.execute(
                [sys.executable, "-c", code],
                cwd=Path.cwd(),
                timeout=1 if expected != "timeout" else 0.05,
                output_cap=32,
                environment={"PATH": os.environ["PATH"]},
            )
        assert issue.value.code == ("OUTPUT_LIMIT" if expected == "output-limit" else "TIMEOUT")


def test_command_and_environment_identities_drift_on_material_changes() -> None:
    runner = LocalProcessRunner()
    first, _ = _observe(runner, "print('one')")
    second, _ = _observe(runner, "print('two')")
    assert first.command_identity != second.command_identity
    assert first.environment_identity == second.environment_identity

    changed = runner.observe(
        [sys.executable, "-c", "print('one')"],
        cwd=Path.cwd(),
        timeout=1,
        output_cap=128,
        environment={"PATH": os.environ["PATH"], "MNCS_FORGE_RECEIPT_SECRET": "changed"},
    )
    assert first.environment_identity != changed.environment_identity


def test_receipt_adapter_produces_schema_valid_non_authoritative_envelope() -> None:
    observation, context = _observe(LocalProcessRunner(), "print('hello')")
    receipt = build_mncs_execution_receipt(observation, context)
    errors = sorted(
        Draft202012Validator(_schema(), format_checker=FormatChecker()).iter_errors(receipt),
        key=str,
    )
    assert errors == []
    assert receipt["record_type"] == "mncs-execution-receipt"
    assert receipt["schema_version"] == "0.1-experimental"
    assert receipt["process"]["harness_status"] == "UNKNOWN"  # type: ignore[index]
    assert all(value == "not-asserted" for value in receipt["claim_boundary"].values())  # type: ignore[union-attr]
    assert receipt["placement"] == {"execution_placement_reference": None}
    assert receipt["enforcement"]["filesystem_restriction"] == "not-enforced"  # type: ignore[index]
    assert receipt["enforcement"]["network_restriction"] == "not-enforced"  # type: ignore[index]


def test_receipt_carries_cgroup_limits_and_aggregate_resource_observations() -> None:
    observation, context = _observe(LocalProcessRunner(), "print('contained')")
    envelope = {
        "identity": "e" * 64,
        "memory_high_bytes": 96 * 1024 * 1024,
        "memory_max_bytes": 128 * 1024 * 1024,
        "memory_swap_max_bytes": 0,
        "tasks_max": 8,
        "concurrency_max": 1,
        "runtime_max_seconds": 30,
    }
    observed = replace(
        observation,
        capabilities=replace(
            observation.capabilities,
            memory_limit="enforced",
            process_count_limit="enforced",
            aggregate_concurrency_limit="enforced",
        ),
        resource_envelope=envelope,
        resource_observations={
            "resource_observations": {
                "cgroup_memory_peak_bytes": 32 * 1024 * 1024,
                "process_count_peak": 3,
                "cpu_time_microseconds": 250_000,
            }
        },
    )
    receipt = build_mncs_execution_receipt(observed, context)
    errors = sorted(
        Draft202012Validator(_schema(), format_checker=FormatChecker()).iter_errors(receipt),
        key=str,
    )
    assert errors == []
    assert receipt["enforcement"]["resource_limits"] == "enforced"  # type: ignore[index]
    limits = receipt["policy"]["requested_limits"]  # type: ignore[index]
    assert {item["resource"] for item in limits} >= {
        "host-memory",
        "concurrency",
    }
    local = receipt["extensions"]["forge:local-process"]  # type: ignore[index]
    assert local["resource_envelope"]["tasks_max"] == 8  # type: ignore[index]
    metrics = {item["metric"]: item for item in receipt["resources"]}  # type: ignore[index]
    assert metrics["host-memory-peak"]["value"] == 32 * 1024 * 1024
    assert metrics["process-count"]["value"] == 3
    assert metrics["cpu-time"]["value"] == 0.25


def test_receipt_identity_changes_when_observed_command_changes() -> None:
    first_observation, context = _observe(LocalProcessRunner(), "print('one')")
    second_observation, _ = _observe(LocalProcessRunner(), "print('two')")
    first = build_mncs_execution_receipt(first_observation, context)
    second = build_mncs_execution_receipt(second_observation, context)
    assert first["receipt_identity"] != second["receipt_identity"]


def test_receipt_adapter_fails_closed_for_missing_bundle_or_incomplete_output() -> None:
    observation, context = _observe(LocalProcessRunner(), "print('hello')")
    with pytest.raises(ForgeError) as issue:
        build_mncs_execution_receipt(
            observation, replace(context, test_bundle_identity="missing-bundle")
        )
    assert issue.value.code == "RECEIPT_CONTEXT"

    limited, limited_context = _observe(LocalProcessRunner(), "print('x' * 100000)", output_cap=32)
    assert limited.termination_category == "output-limit"
    with pytest.raises(ForgeError) as issue:
        build_mncs_execution_receipt(limited, limited_context)
    assert issue.value.code == "RECEIPT_OBSERVATION"


def test_receipt_adapter_fails_closed_for_malformed_subject_context() -> None:
    observation, context = _observe(LocalProcessRunner(), "print('context')")
    with pytest.raises(ForgeError, match="subject_family"):
        build_mncs_execution_receipt(
            observation,
            replace(context, subject_family="OTHER"),  # type: ignore[arg-type]
        )


@pytest.mark.skipif(not SIBLING_SOURCE.exists(), reason="MNCS sibling checkout is unavailable")
def test_reference_receipt_validates_with_sibling_mncs_validator() -> None:
    sys.path.insert(0, str(SIBLING_SOURCE))
    from mncs_validator.execution_receipt import validate_execution_receipt_value

    observation, context = _observe(LocalProcessRunner(), "print('sibling-valid')")
    report = validate_execution_receipt_value(build_mncs_execution_receipt(observation, context))
    assert report.valid is True
    assert report.validation_status == "PASS"
    assert report.execution_status == "PASS"
    assert report.harness_status == "UNKNOWN"
