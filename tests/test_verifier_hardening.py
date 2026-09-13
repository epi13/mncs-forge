from __future__ import annotations

import subprocess
import sys
from collections.abc import Callable

import pytest
from conftest import with_native_latency_allowance

from mncs_forge.config import ForgeConfig
from mncs_forge.engine import Forge
from mncs_forge.errors import ForgeError
from mncs_forge.execution_windows import collect_windows_pipes
from mncs_forge.micro_verifiers import MicroVerifierService
from mncs_forge.records import derive_record_identity


@pytest.mark.parametrize(
    "imports",
    [
        "import mncs_forge.engine as engine; import mncs_forge",
        "import mncs_forge; import mncs_forge.engine as engine",
    ],
)
def test_import_orders_use_the_authoritative_verifier_service(imports: str) -> None:
    script = f"""
import sys
{imports}
from mncs_forge import micro_verifiers

service = micro_verifiers.MicroVerifierService
print(service.__module__)
print(service.__qualname__)
print(engine.MicroVerifierService is service)
print("mncs_forge.micro_verifiers_hardened" in sys.modules)
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=True,
        capture_output=True,
        text=True,
    )
    assert completed.stdout.splitlines() == [
        "mncs_forge.micro_verifiers",
        "MicroVerifierService",
        "True",
        "False",
    ]


def begin_and_register(forge: Forge) -> dict[str, object]:
    forge.epoch_begin(generator_identity="generator-v1", evaluator_identity="evaluator-v1")
    return forge.candidate_register(
        changed_files=["candidate/main.py"],
        hypothesis="hardened verifier fixture",
        generator_identity="generator-v1",
        generator_config_identity="generator-config-v1",
    )


def test_deleted_changed_path_uses_explicit_absent_identity(config: ForgeConfig) -> None:
    forge = Forge(config)
    candidate = begin_and_register(forge)
    deleted_path = "candidate/deleted.py"
    result = forge.verifier_run(
        "verify-pass",
        candidate_identity=str(candidate["candidate_id"]),
        changed_paths=[deleted_path],
        scope="file",
    )
    assert result["status"] == "PASS"
    identity = result["input_identities"]["changed_path_identities"][deleted_path]
    assert str(identity).startswith("forge-json-sha256-v1:")


def test_changed_path_rejects_existing_directory(config: ForgeConfig) -> None:
    forge = Forge(config)
    begin_and_register(forge)
    with pytest.raises(ForgeError, match="not a file"):
        forge.verifier_run(
            "verify-pass",
            changed_paths=["candidate"],
            scope="file",
        )


def test_windows_overflow_checked_after_reader_threads_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class InlineThread:
        def __init__(
            self,
            *,
            target: Callable[..., None],
            args: tuple[object, ...],
            daemon: bool,
        ) -> None:
            self.target = target
            self.args = args
            self.daemon = daemon

        def start(self) -> None:
            self.target(*self.args)

        def is_alive(self) -> bool:
            return False

        def join(self, timeout: float | None = None) -> None:
            del timeout

    monkeypatch.setattr("mncs_forge.execution_windows.threading.Thread", InlineThread)
    process = subprocess.Popen(
        [sys.executable, "-c", "import sys; sys.stdout.write('x' * 1024)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    with pytest.raises(ForgeError) as issue:
        collect_windows_pipes(
            process,
            timeout=5,
            stdout_cap=16,
            stderr_cap=16,
        )
    assert issue.value.code == "OUTPUT_LIMIT"


def test_started_action_receives_terminal_unknown_on_authority_drift(
    config: ForgeConfig,
    monkeypatch: object,
) -> None:
    forge = Forge(config)
    begin_and_register(forge)
    original = forge._observer.current_authority_identities
    calls = 0

    def drifting_authority() -> dict[str, str]:
        nonlocal calls
        calls += 1
        if calls == 1:
            return original()
        return {"drift": "sha256:" + "0" * 64}

    monkeypatch.setattr(forge._observer, "current_authority_identities", drifting_authority)
    result = forge.verifier_run(
        "verify-pass",
        changed_paths=["candidate/main.py"],
        scope="file",
    )
    assert result["status"] == "UNKNOWN"
    assert result["operational_error"]["code"] == "PROVIDER_MUTATION"
    records = forge.ledger.records()
    kinds = [entry["kind"] for entry in records]
    assert kinds[-2:] == ["verifier_action", "verifier_result"]
    action_id = records[-2]["payload"]["action_id"]
    terminal_results = [
        entry
        for entry in records
        if entry["kind"] == "verifier_result" and entry["payload"]["action_id"] == action_id
    ]
    assert len(terminal_results) == 1


def test_started_action_receives_terminal_unknown_on_unexpected_execution_error(
    config: ForgeConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    forge = Forge(config)
    begin_and_register(forge)

    def unexpected_failure(*args: object, **kwargs: object) -> dict[str, object]:
        del args, kwargs
        raise RuntimeError("internal detail must not escape")

    monkeypatch.setattr(MicroVerifierService, "_execute", unexpected_failure)
    result = forge.verifier_run(
        "verify-pass",
        changed_paths=["candidate/main.py"],
        scope="file",
    )

    assert result["status"] == "UNKNOWN"
    assert result["operational_error"] == {
        "code": "VERIFIER_INTERNAL",
        "message": "unexpected verifier execution failure",
    }
    assert "internal detail must not escape" not in str(result)
    records = forge.ledger.records()
    assert [entry["kind"] for entry in records[-2:]] == [
        "verifier_action",
        "verifier_result",
    ]
    assert records[-2]["payload"]["action_id"] == records[-1]["payload"]["action_id"]


def test_evaluator_terminal_unknown_is_redacted_before_recording(
    config: ForgeConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = with_native_latency_allowance(config)
    development = Forge(config)
    candidate = begin_and_register(development)
    development.development_checks_run(["pass-check"])
    development.candidate_disposition(
        str(candidate["candidate_id"]),
        disposition="selected",
        reason="fixture PASS",
    )
    development.candidate_freeze(
        str(candidate["candidate_id"]),
        environment_identity="environment-v1",
        required_evidence_plan="evaluator/policy.json",
    )

    def late_failure(*args: object, **kwargs: object) -> dict[str, object]:
        del args, kwargs
        raise ForgeError("SENSITIVE_LATE_FAILURE", "repair-enabling evaluator detail")

    monkeypatch.setattr(MicroVerifierService, "_execute", late_failure)
    evaluator = Forge(config, mode="evaluator")
    disclosed = evaluator.verifier_run(
        "evaluator.status-only",
        changed_paths=["candidate/main.py"],
        scope="file",
    )
    assert disclosed["status"] == "UNKNOWN"
    assert disclosed["repair_feedback_withheld"] is True
    assert "repair-enabling evaluator detail" not in str(disclosed)

    recorded = evaluator.ledger.records()[-1].payload
    assert recorded["assumptions"] == ()
    assert recorded["unsupported_constructs"] == ()
    assert recorded["operational_error"] is None
    assert recorded["returncode"] is None
    assert "repair-enabling evaluator detail" not in str(recorded)
    assert recorded["output_identity"] == derive_record_identity(
        recorded.record_type, recorded.to_json()
    )


def test_batch_supports_per_verifier_parameters(config: ForgeConfig) -> None:
    config = with_native_latency_allowance(config)
    forge = Forge(config)
    begin_and_register(forge)
    result = forge.verifier_batch(
        ["verify-pass", "verify-unknown"],
        changed_paths=["candidate/main.py"],
        scope="file",
        question_parameters={
            "shared": {"note": "shared"},
            "by_verifier": {
                "verify-pass": {"note": "pass-specific"},
                "verify-unknown": {"note": "unknown-specific"},
            },
        },
    )
    assert [item["status"] for item in result["results"]] == ["PASS", "UNKNOWN"]
    assert result["recorded_result_count"] == 2
    assert result["unrecorded_result_count"] == 0


def test_batch_retains_explicit_unknown_when_one_run_is_rejected(
    config: ForgeConfig,
) -> None:
    config = with_native_latency_allowance(config)
    forge = Forge(config)
    begin_and_register(forge)
    result = forge.verifier_batch(
        ["verify-pass", "verify-unknown"],
        changed_paths=["candidate/main.py"],
        scope="file",
        question_parameters={
            "by_verifier": {
                "verify-pass": {"not-declared": True},
                "verify-unknown": {"note": "still-runs"},
            }
        },
    )
    first, second = result["results"]
    assert first["status"] == "UNKNOWN"
    assert first["recorded"] is False
    assert first["operational_error"]["code"] == "VERIFIER_PARAMETER"
    assert second["status"] == "UNKNOWN"
    assert second["output_identity"]
    assert result["partial_execution_explicit"] is True


def test_batch_records_unknown_when_remaining_budget_prevents_start(
    config: ForgeConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    forge = Forge(config)
    begin_and_register(forge)
    after_budget = float(config.verifier_limits["batch_timeout_seconds"]) + 1.0
    clock = iter([0.0, after_budget, after_budget, after_budget])
    monkeypatch.setattr("mncs_forge.micro_verifiers.time.monotonic", lambda: next(clock))

    result = forge.verifier_batch(
        ["verify-pass", "verify-unknown"],
        changed_paths=["candidate/main.py"],
        scope="file",
    )

    assert result["aggregate_status"] == "UNKNOWN"
    assert result["recorded_result_count"] == 0
    assert result["unrecorded_result_count"] == 2
    assert all(item["recorded"] is False for item in result["results"])
    assert all(
        item["operational_error"]["code"] == "VERIFIER_BATCH_LIMIT" for item in result["results"]
    )
