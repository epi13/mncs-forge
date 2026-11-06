from __future__ import annotations

import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from mncs_forge.adapters import LocalProcessRunner
from mncs_forge.application.execution_receipts import persist_workflow_execution
from mncs_forge.application.support import compatibility_execution_result_status
from mncs_forge.config import ForgeConfig
from mncs_forge.errors import ForgeError
from mncs_forge.fabric_execution import FabricExecutionAdapter, ScriptedRunner
from mncs_forge.mncs_execution_receipt import ReceiptContext, build_mncs_execution_receipt


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def test_fabric_adapter_maps_record_without_importing_fabric() -> None:
    started = datetime.now(UTC)
    adapter = FabricExecutionAdapter()
    facts = adapter.facts_from_record(
        {
            "schema_version": "mncs-fabric.execution-record.v0.1",
            "record_id": "sha256:" + "a" * 64,
            "job_identity": "sha256:" + "b" * 64,
            "node": {
                "worker_identity": "worker.alpha",
                "host_identity": "host.alpha",
                "os_family": "linux",
                "architecture": "x86_64",
            },
            "argv": [sys.executable, "-c", "print('fabric')"],
            "stdout": {"captured_utf8": "fabric\n", "bytes": 7, "truncated": False},
            "stderr": {"captured_utf8": "", "bytes": 0, "truncated": False},
            "returncode": 0,
            "termination_reason": "completed",
            "started_at": _timestamp(started),
            "ended_at": _timestamp(started + timedelta(milliseconds=5)),
            "duration_seconds": 0.005,
            "timeout_seconds": 1,
            "stdout_limit": 128,
            "stderr_limit": 128,
            "containment": "not-provided",
            "network_isolation": "not-provided",
            "filesystem_isolation": "not-provided",
            "same_operator": True,
        }
    )
    session = adapter.session_from_facts(facts, stdout=b"fabric\n")
    observation = session.observation
    assert observation.capabilities.runner_kind == "fabric-backed"
    assert observation.capabilities.execution_scope == "remote"
    assert observation.worker_identity == "worker.alpha"
    assert observation.host_identity == "host.alpha"
    assert observation.same_operator is True
    assert observation.capabilities.sandbox_isolation == "not-provided"
    assert observation.termination_category == "completed"


def test_scripted_fabric_runner_is_consumed_like_local_runner(config: ForgeConfig) -> None:
    local = LocalProcessRunner()
    local_session = local.run(
        [sys.executable, "-c", "print('local')"],
        cwd=Path.cwd(),
        timeout=1,
        output_cap=128,
        environment={"PATH": os.environ["PATH"]},
    )
    adapter = FabricExecutionAdapter()
    fabric_session = adapter.session_from_facts(
        adapter.facts_from_record(
            {
                "schema_version": "mncs-fabric.execution-record.v0.1",
                "record_id": "sha256:" + "c" * 64,
                "job_identity": "sha256:" + "d" * 64,
                "node": {
                    "worker_identity": "worker.beta",
                    "os_family": "linux",
                    "architecture": "x86_64",
                },
                "argv": [sys.executable, "-c", "print('local')"],
                "stdout": {"captured_utf8": "local\n", "bytes": 6, "truncated": False},
                "stderr": {"captured_utf8": "", "bytes": 0, "truncated": False},
                "returncode": 0,
                "termination_reason": "completed",
                "started_at": local_session.observation.started_at,
                "ended_at": local_session.observation.ended_at,
                "duration_seconds": local_session.observation.duration_seconds or 0.001,
                "timeout_seconds": 1,
                "stdout_limit": 128,
                "stderr_limit": 128,
                "containment": "unknown",
                "network_isolation": "unknown",
                "filesystem_isolation": "unknown",
                "same_operator": True,
            }
        ),
        stdout=b"local\n",
    )
    runner = ScriptedRunner([fabric_session])
    session = runner.run(
        [sys.executable, "-c", "print('local')"],
        cwd=Path.cwd(),
        timeout=1,
        output_cap=128,
        environment={"PATH": os.environ["PATH"]},
    )
    assert session.observation.worker_identity == "worker.beta"
    assert (
        session.observation.environment_identity != local_session.observation.environment_identity
    )
    assert runner.inspect_capabilities().runner_kind == "fabric-backed"


def test_same_output_different_environment_changes_execution_identity() -> None:
    runner = LocalProcessRunner()
    first = runner.observe(
        [sys.executable, "-c", "print('same')"],
        cwd=Path.cwd(),
        timeout=1,
        output_cap=128,
        environment={"PATH": os.environ["PATH"], "MNCS_FORGE_ENV": "one"},
    )
    second = runner.observe(
        [sys.executable, "-c", "print('same')"],
        cwd=Path.cwd(),
        timeout=1,
        output_cap=128,
        environment={"PATH": os.environ["PATH"], "MNCS_FORGE_ENV": "two"},
    )
    assert first.stdout.complete_sha256 == second.stdout.complete_sha256
    assert first.environment_identity != second.environment_identity
    started = datetime.now(UTC)
    context = ReceiptContext(
        record_id="receipt.env-compare",
        subject_family="MNCS",
        subject_kind="development-record",
        subject_record_id="subject.env-compare",
        subject_canonical_sha256="a" * 64,
        candidate_id="candidate.env-compare",
        test_bundle_identity="b" * 64,
        harness_identity="c" * 64,
        input_snapshot_identity=None,
        execution_policy_identity="d" * 64,
        placement_policy_identity=None,
        result_semantics="environment identity must participate",
        challenge_nonce="env-compare-challenge-0123456789",
        challenge_issued_at=_timestamp(started - timedelta(seconds=1)),
        challenge_expires_at=_timestamp(started + timedelta(minutes=1)),
        observed_at=_timestamp(started),
        command_binding="enforced",
        environment_binding="enforced",
    )
    first_receipt = build_mncs_execution_receipt(first, context)
    second_receipt = build_mncs_execution_receipt(second, context)
    assert first_receipt["receipt_identity"] != second_receipt["receipt_identity"]


def test_binding_rejects_duplicate_action(config: ForgeConfig, tmp_path: Path) -> None:
    from mncs_forge.adapters import LocalProcessRunner, LocalProjectObserver
    from mncs_forge.application.lifecycle import LifecycleContext
    from mncs_forge.application.workflows import WorkflowExecutor
    from mncs_forge.ledger import Ledger
    from mncs_forge.record_store import LocalRecordStore

    ledger = Ledger(config.state_dir)
    store = LocalRecordStore(config.state_dir, ledger)
    observer = LocalProjectObserver(config)
    lifecycle = LifecycleContext(mode="development", records=ledger, observer=observer)
    workflows = WorkflowExecutor(
        config=config,
        mode="development",
        executor=LocalProcessRunner(),
        observer=observer,
        execution_result_classifier=compatibility_execution_result_status,
    )
    from mncs_forge.application.candidates import CandidateService
    from mncs_forge.application.workflows import DevelopmentWorkflowService

    development = DevelopmentWorkflowService(
        config=config,
        mode="development",
        records=ledger,
        record_store=store,
        lifecycle=lifecycle,
        workflows=workflows,
    )
    CandidateService(
        config=config,
        observer=observer,
        record_store=store,
        lifecycle=lifecycle,
        development=development,
    ).begin_epoch(generator_identity="g", evaluator_identity="e")
    candidate = CandidateService(
        config=config,
        observer=observer,
        record_store=store,
        lifecycle=lifecycle,
        development=development,
    ).register(
        changed_files=["candidate/main.py"],
        hypothesis="duplicate receipt",
        generator_identity="g",
        generator_config_identity="c",
    )
    execution = workflows.execute(
        workflows.workflow("pass-check", "development"),
        candidate,
        evaluator=False,
    )
    persist_workflow_execution(
        config=config, records=ledger, record_store=store, execution=execution
    )
    with pytest.raises(ForgeError) as issue:
        persist_workflow_execution(
            config=config, records=ledger, record_store=store, execution=execution
        )
    assert issue.value.code == "RECEIPT_DUPLICATE"
    _ = tmp_path


def test_fabric_adapter_rejects_unsupported_schema() -> None:
    with pytest.raises(ForgeError) as issue:
        FabricExecutionAdapter().facts_from_record({"schema_version": "future.v9"})
    assert issue.value.code == "FABRIC_ADAPTER"


def test_unsupported_image_tag_is_not_treated_as_immutable_identity() -> None:
    adapter = FabricExecutionAdapter()
    facts = adapter.facts_from_record(
        {
            "schema_version": "mncs-fabric.execution-record.v0.1",
            "record_id": "sha256:" + "e" * 64,
            "node": {"os_family": "linux", "architecture": "x86_64", "argv": ["echo"]},
            "argv": ["echo"],
            "stdout": {"bytes": 0, "truncated": False},
            "stderr": {"bytes": 0, "truncated": False},
            "returncode": 0,
            "termination_reason": "completed",
            "started_at": _timestamp(datetime.now(UTC)),
            "ended_at": _timestamp(datetime.now(UTC)),
            "duration_seconds": 0.001,
            "image_identity": None,
        }
    )
    observation = adapter.observation_from_facts(facts)
    assert observation.image_identity is None
    assert observation.capabilities.sandbox_isolation == "unknown"
