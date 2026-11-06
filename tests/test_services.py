from __future__ import annotations

from dataclasses import dataclass

from mncs_forge.adapters import LocalCommandExecutor, LocalProjectObserver
from mncs_forge.application.candidates import CandidateService
from mncs_forge.application.lifecycle import LifecycleContext
from mncs_forge.application.providers import ProviderService
from mncs_forge.application.support import compatibility_execution_result_status
from mncs_forge.application.workflows import DevelopmentWorkflowService, WorkflowExecutor
from mncs_forge.config import ForgeConfig
from mncs_forge.ledger import Ledger
from mncs_forge.ports import CommandExecutor, ExecutionResult
from mncs_forge.record_store import LocalRecordStore


@dataclass
class RecordingExecutor:
    delegate: CommandExecutor
    calls: int = 0

    def execute(self, command: object, **kwargs: object) -> ExecutionResult:
        self.calls += 1
        return self.delegate.execute(command, **kwargs)  # type: ignore[arg-type]


def collaborators(config: ForgeConfig):  # type: ignore[no-untyped-def]
    ledger = Ledger(config.state_dir)
    store = LocalRecordStore(config.state_dir, ledger)
    observer = LocalProjectObserver(config)
    lifecycle = LifecycleContext(mode="development", records=ledger, observer=observer)
    executor = LocalCommandExecutor()
    workflow_executor = WorkflowExecutor(
        config=config,
        mode="development",
        executor=executor,
        observer=observer,
        execution_result_classifier=compatibility_execution_result_status,
    )
    development = DevelopmentWorkflowService(
        config=config,
        mode="development",
        records=ledger,
        record_store=store,
        lifecycle=lifecycle,
        workflows=workflow_executor,
    )
    return ledger, store, observer, lifecycle, executor, development


def test_candidate_service_operates_without_forge_facade(config: ForgeConfig) -> None:
    ledger, store, observer, lifecycle, _, development = collaborators(config)
    service = CandidateService(
        config=config,
        observer=observer,
        record_store=store,
        lifecycle=lifecycle,
        development=development,
    )
    epoch = service.begin_epoch(generator_identity="generator", evaluator_identity="evaluator")
    candidate = service.register(
        changed_files=["candidate/main.py"],
        hypothesis="compatibility characterization",
        generator_identity="generator",
        generator_config_identity="generator-config",
    )
    assert epoch["record_type"] == "epoch"
    assert candidate["record_type"] == "candidate"
    assert [entry.kind for entry in ledger.records()] == ["epoch", "candidate"]


def test_provider_service_uses_injected_execution_and_record_store(config: ForgeConfig) -> None:
    ledger, store, observer, _, executor, _ = collaborators(config)
    recording = RecordingExecutor(executor)
    service = ProviderService(
        config=config,
        mode="development",
        records=ledger,
        record_store=store,
        executor=recording,
        observer=observer,
    )
    result = service.probe("provider-pass")
    assert recording.calls == 1
    assert result["status"] == "PASS"
    assert ledger.records("provider_probe")[0].payload.to_object_dict() == result
