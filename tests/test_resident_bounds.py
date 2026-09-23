from __future__ import annotations

import gc
import json
import os
import subprocess
import time
from collections import deque
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

from mncs_forge import continuous
from mncs_forge.adapters import LocalProjectObserver
from mncs_forge.continuous import (
    CONTINUOUS_ATTENTION_CAPACITY,
    CONTINUOUS_PENDING_CAPACITY,
    CONTINUOUS_RECENT_RESULTS,
    CONTINUOUS_REPAIR_CAPACITY,
    CONTINUOUS_SELECTED_TEST_CAPACITY,
    ContinuousSupervisor,
    _compact_event_result,
)
from mncs_forge.errors import ForgeError
from mncs_forge.execution import run_bounded
from mncs_forge.mncs_native import BoundedNativeCache, NativeForgeAdapter
from mncs_forge.ports import ExecutionResult
from mncs_forge.resource_envelope import SystemdCgroupEnvelope
from mncs_forge.resource_process import bind_current_cgroup_parent

MIB = 1024 * 1024


def test_native_projection_cache_churn_is_bounded_and_exact_keyed() -> None:
    cache = BoundedNativeCache()
    for index in range(5000):
        identity = f"semantic-{index // 8}"
        cache.put(("lifecycle", identity, index), {"value": index})
        assert cache.get(("lifecycle", identity, index)) == {"value": index}
    stats = cache.stats()
    assert stats["entries"] <= stats["entry_capacity"] == 64
    assert stats["retained_bytes_estimate"] <= stats["byte_capacity"] == 2 * MIB
    assert stats["evictions"] > 0
    assert cache.get(("lifecycle", "semantic-0", 0)) is None

    cache.put(("lifecycle", "current", "same-input"), {"status": "PASS"})
    cache.retain_semantic_identity("next")
    assert cache.stats()["entries"] == 0
    assert cache.stats()["identity_invalidations"] >= 1


def test_native_identity_change_evicts_previous_generation_projection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "core.mncs"
    source.write_text("module forge.core;\n", encoding="utf-8")
    adapter = NativeForgeAdapter(tmp_path)
    monkeypatch.setattr(adapter, "_command", lambda: ["mncs"])
    monkeypatch.setattr(
        adapter,
        "_identity_material",
        lambda _command: ([source], [], []),
    )
    cache = adapter._native_caches["lifecycle"]

    first_identity = adapter.semantic_input_identity()
    key = ("lifecycle-contract", first_identity, "same-request")
    cache.put(key, {"status": "PASS"})
    assert cache.get(key) == {"status": "PASS"}

    source.write_text("module forge.core;\n// changed\n", encoding="utf-8")
    second_identity = adapter.semantic_input_identity()

    assert second_identity != first_identity
    assert cache.get(key) is None
    assert cache.stats()["identity_invalidations"] == 1
    adapter.close()


def test_continuous_resident_structures_plateau_under_generation_churn(tmp_path: Path) -> None:
    config = SimpleNamespace(continuous_settings={}, output_cap=1024, state_dir=tmp_path)

    class Runner:
        @staticmethod
        def resource_status() -> dict[str, object]:
            return {"state": "unavailable", "deferred_jobs": 0}

    class Native:
        @staticmethod
        def cache_status() -> dict[str, object]:
            return {"abi": {"entries": 0, "entry_capacity": 64}}

    class Store:
        @staticmethod
        def resident_status() -> dict[str, object]:
            return {
                "forge_record_projection_entries": 4,
                "forge_record_projection_entry_capacity": 1024,
                "embedded_store": {"verified_object_projection_entries": 4},
            }

    supervisor = ContinuousSupervisor(
        SimpleNamespace(config=config, _executor=Runner(), _native=Native(), record_store=Store())
    )
    gc.collect()
    rss_before = _current_rss_bytes()
    for index in range(10000):
        supervisor._record_status(("PASS", "FAIL", "UNKNOWN")[index % 3])
        supervisor._attention(
            {
                "current_generation": index,
                "cursor": index,
                "current": {"identity": f"source-{index}"},
            },
            f"synthetic bounded event {index}",
        )
        supervisor._remember_pending(
            f"obligation-{index}",
            {"verifier_id": "synthetic", "status": "UNKNOWN", "reason": "x" * 10000},
        )
        supervisor._remember_repair(
            {
                "original_source_identity": f"source-{index}",
                "resulting_source_identity": f"source-{index + 1}",
                "doctor_fix_identities": ["fix" * 1000] * 100,
                "migration_rule_identities": ["migration" * 1000] * 100,
                "repair_rounds": index,
                "validation_result": {"status": "PASS", "reason": "x" * 10000},
                "failure_conflict_reason": None,
            }
        )
    supervisor._set_selected_test_ids([f"test-{index}" for index in range(5000)])
    result_history: deque[dict[str, object]] = deque(maxlen=CONTINUOUS_RECENT_RESULTS)
    for index in range(300):
        result_history.append(
            _compact_event_result(
                {
                    "generation": index,
                    "status": "UNKNOWN",
                    "actions": [
                        {
                            "action": "micro_verifier",
                            "result": {
                                "status": "UNKNOWN",
                                "results": [
                                    {"verifier_id": "v", "status": "UNKNOWN", "reason": "y" * 10000}
                                    for _ in range(20)
                                ],
                            },
                        }
                        for _ in range(12)
                    ],
                }
            )
        )

    assert len(supervisor.status_counts) == 3
    assert len(supervisor.attention) == CONTINUOUS_ATTENTION_CAPACITY
    assert len(supervisor.pending) == CONTINUOUS_PENDING_CAPACITY
    assert len(supervisor.repairs) == CONTINUOUS_REPAIR_CAPACITY
    assert len(supervisor.selected_test_ids) == CONTINUOUS_SELECTED_TEST_CAPACITY
    assert supervisor.selected_test_count == 5000
    assert supervisor.pending_overflow_count == 10000 - CONTINUOUS_PENDING_CAPACITY
    assert len(result_history) == CONTINUOUS_RECENT_RESULTS
    status = supervisor.status()
    assert status["queue"]["capacity"] == 1
    assert status["pending_check_overflow_count"] == 10000 - CONTINUOUS_PENDING_CAPACITY
    assert isinstance(status["pending_check_overflow_identity"], str)
    assert status["resources"]["native_projection_caches"]["abi"]["entry_capacity"] == 64
    assert status["resources"]["store_projection"]["forge_record_projection_entry_capacity"] == 1024
    gc.collect()
    rss_after = _current_rss_bytes()
    if rss_before is not None and rss_after is not None:
        if os.environ.get("MNCS_RESIDENT_RSS_TRACE") == "1":
            print(
                "resident_cycle_rss "
                + json.dumps(
                    {
                        "before_bytes": rss_before,
                        "after_bytes": rss_after,
                        "delta_bytes": rss_after - rss_before,
                        "generations": 10000,
                    },
                    sort_keys=True,
                )
            )
        assert rss_after - rss_before <= 32 * MIB


def test_continuous_status_keeps_active_generation_elapsed_and_cancel_state(
    tmp_path: Path,
) -> None:
    config = SimpleNamespace(continuous_settings={}, output_cap=1024, state_dir=tmp_path)

    class Runner:
        @staticmethod
        def resource_status() -> dict[str, object]:
            return {"state": "protected", "aggregate_process_count": 1}

    class Native:
        @staticmethod
        def cache_status() -> dict[str, object]:
            return {}

    class Store:
        @staticmethod
        def resident_status() -> dict[str, object]:
            return {}

    supervisor = ContinuousSupervisor(
        SimpleNamespace(config=config, _executor=Runner(), _native=Native(), record_store=Store())
    )
    supervisor.active_job = {
        "generation": 12,
        "source_identity": "source-12",
        "started_monotonic": time.monotonic() - 2,
    }
    status = supervisor.status()
    assert status["active_job"]["generation"] == 12
    assert status["active_job"]["elapsed_seconds"] >= 2
    assert status["cancellation_requested"] is False


def test_stop_request_persists_cancellation_state_for_external_status(
    tmp_path: Path,
) -> None:
    config = SimpleNamespace(continuous_settings={}, output_cap=1024, state_dir=tmp_path)

    class Runner:
        @staticmethod
        def resource_status() -> dict[str, object]:
            return {"state": "unavailable"}

    class Native:
        @staticmethod
        def cache_status() -> dict[str, object]:
            return {}

    class Store:
        @staticmethod
        def resident_status() -> dict[str, object]:
            return {}

    supervisor = ContinuousSupervisor(
        SimpleNamespace(config=config, _executor=Runner(), _native=Native(), record_store=Store())
    )
    supervisor.active_job = {
        "identity": "job-stop",
        "started_monotonic": time.monotonic(),
    }
    supervisor._write_status(supervisor.status())

    supervisor.request_stop()

    retained = supervisor.read_status()
    assert retained["cancellation_requested"] is True
    assert retained["active_job"]["cancellation_requested"] is True


def test_lifecycle_status_refreshes_active_job_resource_facts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = SimpleNamespace(
        state_dir=tmp_path,
        root=tmp_path,
        continuous_settings={"enabled": True},
    )
    continuous._write_json_path(
        tmp_path / "continuous" / "status.json",
        {
            "workspace_generation": 23,
            "active_job": {
                "generation": 23,
                "identity": "job-23",
                "started_monotonic": time.monotonic() - 2,
            },
        },
    )
    monkeypatch.setattr(continuous, "_supervisor_status", lambda _config: {"state": "running"})
    monkeypatch.setattr(
        continuous,
        "_language_service_status",
        lambda _config: {"state": "running", "status": {"generation": 23}},
    )

    class Envelope:
        def __init__(self, _settings, *, required: bool) -> None:
            assert required is True

        @staticmethod
        def status() -> dict[str, object]:
            return {
                "state": "protected",
                "resource_envelope_identity": "envelope-23",
                "aggregate_memory_current_bytes": 4 * MIB,
                "aggregate_memory_peak_bytes": 8 * MIB,
                "aggregate_process_count": 2,
            }

    monkeypatch.setattr("mncs_forge.resource_envelope.SystemdCgroupEnvelope", Envelope)
    result = continuous.continuous_lifecycle(config, "status")
    job = result["continuous"]["active_job"]
    assert job["generation"] == 23
    assert job["elapsed_seconds"] >= 2
    assert job["resource_protection_state"] == "protected"
    assert job["resource_envelope_identity"] == "envelope-23"
    assert job["execution_process_count_current"] == 2
    assert result["continuous"]["resources"]["aggregate_memory_peak_bytes"] == 8 * MIB
    assert result["continuous"]["resources"]["active_jobs"][0]["identity"] == "job-23"


def _current_rss_bytes() -> int | None:
    try:
        for line in Path("/proc/self/status").read_text(encoding="ascii").splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        return None
    return None


def test_resource_exhaustion_stays_unknown_and_keeps_job_identity(tmp_path: Path) -> None:
    config = SimpleNamespace(
        continuous_settings={"candidate_identity": "candidate-current"},
        output_cap=1024,
        state_dir=tmp_path,
        root=tmp_path,
    )

    class Forge:
        mode = "development"
        calls = 0

        @classmethod
        def verifier_run(cls, *_args, **_kwargs):
            cls.calls += 1
            raise ForgeError(
                "RESOURCE_LIMIT",
                "test envelope memory max reached",
                details={
                    "resource_evidence": {
                        "resource_envelope_identity": "e" * 64,
                        "resource_exhausted": True,
                        "resource_metric": "host-memory-peak",
                        "resource_bound": 128 * MIB,
                        "resource_observed": 128 * MIB,
                        "cleanup_succeeded": True,
                        "job_context": {
                            "generation": 7,
                            "source_identity": "source-current",
                            "candidate_identity": "candidate-current",
                        },
                    }
                },
            )

    Forge.config = config
    supervisor = ContinuousSupervisor(Forge())
    supervisor._trigger_cost_allowed = lambda *_args: True  # type: ignore[method-assign]
    supervisor._reusable_result = lambda *_args: None  # type: ignore[method-assign]
    result = supervisor._micro(
        {
            "current_generation": 7,
            "current": {
                "uri": f"file://{tmp_path / 'source.mncs'}",
                "identity": "source-current",
            },
        },
        {
            "id": "memory-test",
            "action": "micro_verifier",
            "maximum_cost": "low",
            "verifier_ids": ["memory-verifier", "follow-up-verifier"],
        },
    )
    assert result["status"] == "UNKNOWN"
    failure = result["results"][0]
    assert failure["error_code"] == "RESOURCE_LIMIT"
    assert failure["status"] == "UNKNOWN"
    assert failure["resource_evidence"]["resource_metric"] == "host-memory-peak"
    assert failure["resource_evidence"]["job_context"]["generation"] == 7
    assert Forge.calls == 1
    assert supervisor.deferred_jobs == 2
    pending = list(supervisor.pending.values())
    assert {item.get("verifier_id") for item in pending} == {
        "memory-verifier",
        "follow-up-verifier",
    }
    current = next(item for item in pending if item.get("verifier_id") == "memory-verifier")
    assert current["resource_evidence"]["resource_metric"] == "host-memory-peak"
    assert current["source_identity"] == "source-current"
    restored = ContinuousSupervisor(Forge())
    restored._restore_pending({"pending_checks": pending})
    assert len(restored.pending) == 2
    assert {item.get("verifier_id") for item in restored.pending.values()} == {
        "memory-verifier",
        "follow-up-verifier",
    }


def test_completed_current_generation_micro_verifier_resolves_pending_item(
    tmp_path: Path,
) -> None:
    config = SimpleNamespace(
        continuous_settings={"candidate_identity": "candidate-current"},
        output_cap=1024,
        state_dir=tmp_path,
        root=tmp_path,
        verifiers={"small": SimpleNamespace(cost="low")},
    )

    class Forge:
        mode = "development"

        @staticmethod
        def verifier_run(*_args, **_kwargs):
            return {"status": "PASS", "dependency_envelope": {"complete": True}}

    Forge.config = config
    supervisor = ContinuousSupervisor(Forge())
    supervisor._trigger_cost_allowed = lambda *_args: True  # type: ignore[method-assign]
    supervisor._reusable_result = lambda *_args: None  # type: ignore[method-assign]
    event = {
        "current_generation": 7,
        "current": {
            "uri": f"file://{tmp_path / 'source.mncs'}",
            "identity": "source-current",
        },
    }
    trigger = {
        "id": "small-trigger",
        "action": "micro_verifier",
        "maximum_cost": "low",
        "verifier_ids": ["small"],
    }
    supervisor._remember_micro_pending(event, trigger, "candidate-current", "small", "pressure")
    identity = supervisor._pending_identity(7, "candidate-current", "small")
    assert identity in supervisor.pending

    result = supervisor._micro(event, trigger)

    assert result["status"] == "PASS"
    assert identity not in supervisor.pending


def _fake_systemd_manager(
    tmp_path: Path,
    *,
    memory_events: str = "high 0\nmax 0\noom 0\noom_kill 0\n",
    process_events: str = "max 0\n",
    result: str = "success",
    main_code: int = 1,
    main_status: int = 0,
    settings: dict[str, object] | None = None,
) -> tuple[SystemdCgroupEnvelope, list[list[str]], Path]:
    runtime = tmp_path / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    runtime.chmod(0o700)
    cgroup_root = tmp_path / "cgroup"
    cgroup_root.mkdir(parents=True, exist_ok=True)
    (cgroup_root / "cgroup.controllers").write_text("memory pids cpu\n", encoding="ascii")
    group = "mncs-forge-verification.slice"
    job_group = cgroup_root / group
    job_group.mkdir(parents=True)
    (job_group / "memory.high").write_text(str(64 * MIB), encoding="ascii")
    (job_group / "memory.max").write_text(str(128 * MIB), encoding="ascii")
    (job_group / "memory.swap.max").write_text("0", encoding="ascii")
    (job_group / "pids.max").write_text("8", encoding="ascii")
    (job_group / "memory.current").write_text(str(96 * MIB), encoding="ascii")
    (job_group / "memory.peak").write_text(str(128 * MIB), encoding="ascii")
    (job_group / "pids.current").write_text("0", encoding="ascii")
    (job_group / "pids.peak").write_text("8", encoding="ascii")
    (job_group / "memory.events").write_text(memory_events, encoding="ascii")
    (job_group / "pids.events").write_text(process_events, encoding="ascii")
    (job_group / "cpu.stat").write_text("usage_usec 5000\n", encoding="ascii")
    commands: list[list[str]] = []

    def control(command, **_kwargs):
        argv = [str(item) for item in command]
        commands.append(argv)
        if "show" in argv:
            output = (
                f"ControlGroup=/{group}\nResult={result}\nActiveState=inactive\n"
                "SubState=dead\nMemoryPeak=134217728\nCPUUsageNSec=5000000\n"
                f"ExecMainCode={main_code}\nExecMainStatus={main_status}\nTasksCurrent=0\n"
                "LoadState=loaded\nMemoryAccounting=yes\nTasksAccounting=yes\n"
                "CPUAccounting=yes\n"
            ).encode()
        else:
            output = b""
        return subprocess.CompletedProcess(argv, 0, stdout=output, stderr=b"")

    manager = SystemdCgroupEnvelope(
        settings
        or {"memory_max_bytes": 128 * MIB, "memory_cap_bytes": 128 * MIB, "pids_limit": 8},
        required=True,
        control_runner=control,
        host_memory_reader=lambda: (2 * 1024 * MIB, 1536 * MIB),
        cgroup_root=cgroup_root,
        systemd_run="systemd-run",
        systemctl="systemctl",
        runtime_dir=runtime,
        resource_semantics=NativeForgeAdapter(Path(__file__).resolve().parents[1]),
    )
    manager._lock_path = runtime / "test.lock"
    assert manager.available
    return manager, commands, job_group


def test_small_cgroup_budget_and_memory_pid_timeout_evidence_use_fake_kernel_state(
    tmp_path: Path,
) -> None:
    manager, commands, _group = _fake_systemd_manager(
        tmp_path,
        memory_events="high 0\nmax 1\noom 0\noom_kill 0\n",
        settings={
            "memory_max_bytes": 128 * MIB,
            "memory_cap_bytes": 128 * MIB,
            "pids_limit": 8,
            "runtime_max_seconds": 4,
        },
    )
    tiny = manager.budget
    assert tiny is not None
    assert tiny.memory_max_bytes == 128 * MIB
    assert tiny.memory_high_bytes < tiny.memory_max_bytes
    assert tiny.memory_swap_max_bytes == 0
    assert tiny.tasks_max == 8
    assert tiny.runtime_max_seconds == 4

    prepared = manager.prepare_execution(
        ["/usr/bin/true"],
        cwd=tmp_path,
        environment={"PATH": "/usr/bin"},
        timeout=3,
    )
    assert prepared is not None
    assert "--property=MemoryMax=134217728" in prepared.argv
    assert "--property=MemorySwapMax=0" in prepared.argv
    assert "--property=TasksMax=8" in prepared.argv
    assert "--property=KillMode=control-group" in prepared.argv
    assert not any("CollectMode" in argument for argument in prepared.argv)
    memory = manager.finish_execution(prepared)
    assert memory["resource_exhausted"] is True
    assert memory["resource_metric"] == "host-memory-peak"
    assert memory["resource_bound"] == 128 * MIB
    assert memory["resource_observed"] == 128 * MIB
    assert memory["command_returncode"] == 0
    assert memory["resource_observations"]["cpu_time_microseconds"] == 5_000
    assert memory["cleanup_succeeded"] is True
    assert [command[2] for command in commands if len(command) > 2][-4:] == [
        "kill",
        "stop",
        "show",
        "reset-failed",
    ]

    manager, _commands, _group = _fake_systemd_manager(
        tmp_path / "nonzero", main_status=7
    )
    prepared = manager.prepare_execution(
        ["/usr/bin/false"], cwd=tmp_path, environment={"PATH": "/usr/bin"}, timeout=3
    )
    assert prepared is not None
    assert manager.finish_execution(prepared)["command_returncode"] == 7

    manager, _commands, _group = _fake_systemd_manager(
        tmp_path / "signal", main_code=2, main_status=9
    )
    prepared = manager.prepare_execution(
        ["/usr/bin/sleep"], cwd=tmp_path, environment={"PATH": "/usr/bin"}, timeout=3
    )
    assert prepared is not None
    assert manager.finish_execution(prepared)["command_returncode"] == -9

    manager, _commands, _group = _fake_systemd_manager(tmp_path / "pids", process_events="max 1\n")
    prepared = manager.prepare_execution(
        ["/usr/bin/true"],
        cwd=tmp_path,
        environment={"PATH": "/usr/bin"},
        timeout=3,
    )
    assert prepared is not None
    pids = manager.finish_execution(prepared)
    assert pids["resource_exhausted"] is True
    assert pids["resource_metric"] == "process-count"
    assert pids["resource_bound"] == 8
    assert pids["cleanup_succeeded"] is True

    manager, _commands, _group = _fake_systemd_manager(tmp_path / "timeout")
    prepared = manager.prepare_execution(
        ["/usr/bin/true"],
        cwd=tmp_path,
        environment={"PATH": "/usr/bin"},
        timeout=3,
    )
    assert prepared is not None
    timed_out = manager.finish_execution(prepared, timed_out=True)
    assert timed_out["resource_exhausted"] is True
    assert timed_out["resource_metric"] == "wall-duration"
    assert timed_out["resource_bound"] == 3
    assert timed_out["cleanup_succeeded"] is True

    manager, _commands, _group = _fake_systemd_manager(
        tmp_path / "oom", memory_events="high 0\nmax 0\noom 1\noom_kill 0\n"
    )
    prepared = manager.prepare_execution(
        ["/usr/bin/true"],
        cwd=tmp_path,
        environment={"PATH": "/usr/bin"},
        timeout=3,
    )
    assert prepared is not None
    oom = manager.finish_execution(prepared)
    assert oom["resource_exhausted"] is True
    assert oom["resource_metric"] == "host-memory-peak"
    assert oom["resource_observations"]["memory_oom_events"] == 1


def test_unit_command_does_not_expose_provider_environment_values(
    tmp_path: Path,
) -> None:
    manager, _commands, _group = _fake_systemd_manager(tmp_path)
    prepared = manager.prepare_execution(
        ["/usr/bin/true"],
        cwd=tmp_path,
        environment={"PATH": "/usr/bin", "MNCS_TEST_SECRET": "secret-not-in-unit"},
        timeout=3,
    )
    assert prepared is not None
    encoded_argv = "\x00".join(prepared.argv)
    assert "secret-not-in-unit" not in encoded_argv
    assert "MNCS_TEST_SECRET=secret-not-in-unit" not in encoded_argv
    assert prepared.environment_file_path.stat().st_mode & 0o777 == 0o600
    staged = json.loads(prepared.environment_file_path.read_text(encoding="utf-8"))
    assert staged == {"MNCS_TEST_SECRET": "secret-not-in-unit", "PATH": "/usr/bin"}
    facts = manager.finish_execution(prepared)
    assert facts["cleanup_succeeded"] is True
    assert facts["environment_file_cleanup_succeeded"] is True
    assert not prepared.environment_file_path.exists()


def test_launcher_environment_adds_only_user_manager_transport(tmp_path: Path) -> None:
    manager, _commands, _group = _fake_systemd_manager(tmp_path)
    result = manager.launcher_environment({"PATH": "/verifier/bin"})
    assert result["PATH"] == "/verifier/bin"
    assert result["XDG_RUNTIME_DIR"] == str(tmp_path / "runtime")
    assert "HOME" not in result
    assert "MNCS_TEST_SECRET" not in result


def test_container_cgroup_parent_binds_to_the_current_service_only() -> None:
    command = [
        "podman",
        "run",
        "--cgroup-parent=@mncs.current-cgroup@",
        "image",
        "true",
    ]
    bound = bind_current_cgroup_parent(command, "0::/user.slice/user@1000.service/job.service\n")
    assert bound == [
        "podman",
        "run",
        "--cgroup-parent=/user.slice/user@1000.service/job.service",
        "image",
        "true",
    ]
    assert bind_current_cgroup_parent(command, "1:memory:/not-unified\n") is None


def test_cleanup_accepts_inactive_dead_service_after_systemd_removes_cgroup(
    tmp_path: Path,
) -> None:
    manager, commands, _group = _fake_systemd_manager(tmp_path)
    manager._unit_properties = lambda _unit: {  # type: ignore[method-assign]
        "LoadState": "not-found",
        "ActiveState": "inactive",
        "SubState": "dead",
        "ControlGroup": "",
    }
    assert manager._cleanup_unit("mncs-forge-job-test.service", "") is True
    assert not any("reset-failed" in command for command in commands)


def test_cleanup_resets_failed_timeout_after_systemd_killed_tree_and_removed_cgroup(
    tmp_path: Path,
) -> None:
    manager, commands, _group = _fake_systemd_manager(tmp_path)
    manager._unit_properties = lambda _unit: {  # type: ignore[method-assign]
        "LoadState": "loaded",
        "ActiveState": "failed",
        "SubState": "failed",
        "Result": "timeout",
        "ControlGroup": "",
    }
    assert manager._cleanup_unit("mncs-forge-job-timeout.service", "") is True
    assert any("reset-failed" in command for command in commands)


def test_runner_defers_when_a_previous_owned_tree_is_still_active(tmp_path: Path) -> None:
    manager, _commands, group = _fake_systemd_manager(tmp_path)
    (group / "pids.current").write_text("2", encoding="ascii")
    with pytest.raises(ForgeError) as issue, manager.execution_lock():
        pytest.fail("must not admit a second job while the cgroup slice is occupied")
    assert issue.value.code == "RESOURCE_CONCURRENCY_LIMIT"
    assert issue.value.details["resource_evidence"]["resource_observed"] == 2
    assert issue.value.details["resource_evidence"]["deferred"] is True


def test_runner_uses_native_accepted_exit_status_when_systemd_reaps_unit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    prepared = SimpleNamespace(
        argv=("systemd-run", "--", "/usr/bin/true"),
        unit_name="mncs-forge-job-test.service",
        timeout_seconds=2,
    )

    class Envelope:
        budget = SimpleNamespace(to_dict=lambda: {"identity": "envelope-test"})

        @staticmethod
        def execution_lock():
            return nullcontext()

        @staticmethod
        def prepare_execution(*_args, **_kwargs):
            return prepared

        @staticmethod
        def observe_execution(_prepared):
            return None

        @staticmethod
        def launcher_environment(environment):
            return dict(environment)

        @staticmethod
        def finish_execution(_prepared, **_kwargs):
            return {
                "resource_exhausted": False,
                "cleanup_succeeded": True,
                "resource_outcome": "Pass",
                "command_returncode": None,
                "systemd_wrapper_returncode": 0,
                "native_execution_returncode_available": True,
                "native_execution_returncode": 0,
            }

    monkeypatch.setattr(
        "mncs_forge.execution._run_bounded_process",
        lambda *_args, **_kwargs: ExecutionResult(
            argv=["systemd-run"],
            returncode=0,
            stdout=b"",
            stderr=b"",
            duration_seconds=0.001,
        ),
    )
    result = run_bounded(
        ["/usr/bin/true"],
        cwd=tmp_path,
        timeout=2,
        output_cap=1024,
        environment={"PATH": "/usr/bin"},
        resource_envelope=Envelope(),
    )
    assert result.returncode == 0
    assert result.resource_observations["native_execution_returncode"] == 0


def test_runner_cleans_owned_unit_when_python_cancellation_escapes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    prepared = SimpleNamespace(
        argv=("systemd-run", "--", "/usr/bin/true"),
        unit_name="mncs-forge-job-cancelled.service",
        timeout_seconds=2,
    )
    cleanup: list[str] = []

    class Envelope:
        budget = SimpleNamespace(to_dict=lambda: {"identity": "envelope-test"})

        @staticmethod
        def execution_lock():
            return nullcontext()

        @staticmethod
        def prepare_execution(*_args, **_kwargs):
            return prepared

        @staticmethod
        def observe_execution(_prepared):
            return None

        @staticmethod
        def launcher_environment(environment):
            return dict(environment)

        @staticmethod
        def finish_execution(_prepared, **_kwargs):
            cleanup.append("finished")
            return {"cleanup_succeeded": True}

    def interrupted(*_args, **_kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr("mncs_forge.execution._run_bounded_process", interrupted)
    with pytest.raises(KeyboardInterrupt):
        run_bounded(
            ["/usr/bin/true"],
            cwd=tmp_path,
            timeout=2,
            output_cap=1024,
            environment={"PATH": "/usr/bin"},
            resource_envelope=Envelope(),
        )
    assert cleanup == ["finished"]


def test_unavailable_tree_envelope_fails_closed_before_spawning(tmp_path: Path) -> None:
    manager = SystemdCgroupEnvelope(
        {},
        required=True,
        host_memory_reader=lambda: (1024 * MIB, 900 * MIB),
        cgroup_root=tmp_path,
        systemd_run="systemd-run",
        systemctl="systemctl",
        resource_semantics=NativeForgeAdapter(Path(__file__).resolve().parents[1]),
    )
    assert manager.available is False
    runner = __import__("mncs_forge.adapters", fromlist=["LocalProcessRunner"]).LocalProcessRunner(
        manager
    )
    marker = tmp_path / "must-not-exist"
    session = runner.run(
        ["/usr/bin/touch", str(marker)],
        cwd=tmp_path,
        timeout=1,
        output_cap=128,
        environment=dict(os.environ),
    )
    assert session.result is None
    assert session.error_code == "RESOURCE_ENVELOPE_UNAVAILABLE"
    assert session.observation.termination_category == "policy-rejected"
    assert session.observation.resource_observations["deferred"] is True
    assert marker.exists() is False


def test_provider_workspace_preflight_bounds_copy_bytes(config, project) -> None:
    large = project / "candidate" / "resident-bounds-large.bin"
    large.write_bytes(b"x" * 4096)
    observer = LocalProjectObserver(config)
    with pytest.raises(ForgeError) as issue:
        observer._check_workspace_bound([project / "candidate"], byte_limit=1024, entry_limit=100)
    assert issue.value.code == "WORKSPACE_LIMIT"
    evidence = issue.value.details["resource_evidence"]
    assert evidence["resource_metric"] == "workspace-bytes"
    assert evidence["resource_bound"] == 1024
