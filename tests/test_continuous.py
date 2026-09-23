from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
from collections import OrderedDict
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from conftest import with_native_latency_allowance

from mncs_forge.adapters import LocalProcessRunner
from mncs_forge.continuous import ContinuousSupervisor, LanguageServiceSocket
from mncs_forge.errors import ForgeError
from mncs_forge.engine import Forge
from mncs_forge.execution_observations import ExecutionObservationBuilder
from mncs_forge.mncs_native import NativeForgeAdapter
from mncs_forge.ports import ExecutionResult
from mncs_forge.record_store import LocalRecordStore


def _supervisor() -> ContinuousSupervisor:
    supervisor = object.__new__(ContinuousSupervisor)
    supervisor._resource_semantics = NativeForgeAdapter(Path(__file__).parents[1])
    supervisor.pending = OrderedDict()
    supervisor.pending_overflow_count = 0
    supervisor.pending_overflow_identity = None
    supervisor.cancelled_jobs = 0
    return supervisor


def _event() -> dict[str, object]:
    return {
        "current_generation": 7,
        "current": {"uri": "file:///workspace/src/main.mncs", "identity": "mncs:source:7"},
        "semantic_subjects": [{"identity": "mncs:fn:main", "change": "changed"}],
        "diagnostics": {"added": ["E-MODULE-OLD"], "resolved": []},
        "obligations": {"added": [], "resolved": [], "status_changed": []},
        "impact_complete": True,
        "impact": {
            "complete": True,
            "change_kinds": ["public_contract"],
            "guarantee_domains": ["compatibility"],
            "risk_flags": ["effect_semantics"],
            "nodes": [{"kind": "function"}],
        },
    }


def test_trigger_matching_is_explicit_and_compact() -> None:
    supervisor = _supervisor()
    event = _event()
    assert supervisor._matches(
        {
            "id": "contract-security",
            "action": "security_micro_verifier",
            "maximum_cost": "low",
            "event_kinds": ["public_contract_changed"],
            "change_kinds": ["public_contract"],
            "guarantee_domains": ["compatibility"],
            "subject_kinds": ["function"],
            "diagnostics": ["E-MODULE-*"],
            "risk_flags": ["effect_semantics"],
            "security": True,
        },
        event,
    )
    assert not supervisor._matches(
        {
            "id": "unrelated",
            "action": "micro_verifier",
            "maximum_cost": "low",
            "event_kinds": ["obligation_changed"],
        },
        event,
    )


def test_event_kinds_preserve_security_and_attention_classifications() -> None:
    supervisor = _supervisor()
    kinds = supervisor._event_kinds(_event())
    assert {
        "source_changed",
        "diagnostic_added",
        "semantic_subject_changed",
        "security_boundary_changed",
        "public_contract_changed",
    }.issubset(kinds)


def test_trigger_can_bind_contract_and_obligation_identities() -> None:
    supervisor = _supervisor()
    event = _event()
    event["obligations"] = {
        "added": ["mncs:obligation:authority-boundary"],
        "resolved": [],
        "status_changed": [],
    }
    event["impact"]["nodes"] = [
        {"kind": "contract", "identity": "mncs:contract:public-api"},
        {"kind": "function", "identity": "mncs:fn:main"},
    ]
    assert supervisor._matches(
        {
            "id": "declared-boundary",
            "action": "security_micro_verifier",
            "maximum_cost": "low",
            "contract_identities": ["mncs:contract:public-*"],
            "obligation_identities": ["mncs:obligation:authority-*"],
        },
        event,
    )
    assert not supervisor._matches(
        {
            "id": "other-boundary",
            "action": "security_micro_verifier",
            "maximum_cost": "low",
            "obligation_identities": ["mncs:obligation:unrelated"],
        },
        event,
    )


def test_debounce_policy_is_bounded_and_trigger_declared() -> None:
    supervisor = _supervisor()
    supervisor.settings = {
        "debounce_ms": 120,
        "triggers": [{"id": "burst", "debounce_ms": 9000}],
    }
    assert supervisor._debounce_ms() == 5000


def test_escalation_policy_controls_attention_without_changing_verdict() -> None:
    supervisor = _supervisor()
    event = _event()
    supervisor.attention = []
    supervisor._escalate(event, {"escalation": "silent"}, "hidden", status="FAIL")
    assert supervisor.attention == []
    supervisor._escalate(event, {"escalation": "unknown"}, "fail-hidden", status="FAIL")
    assert supervisor.attention == []
    supervisor._escalate(event, {"escalation": "unknown"}, "unknown-visible")
    assert len(supervisor.attention) == 1


def test_unknown_action_keeps_reconciled_event_unknown() -> None:
    supervisor = _supervisor()
    supervisor.settings = {
        "triggers": [
            {
                "id": "reconcile",
                "action": "verification_plan",
                "maximum_cost": "low",
                "event_kinds": ["workspace_reconciled"],
            }
        ]
    }
    supervisor.current_generation = 0
    supervisor.current_source_identity = None
    supervisor.current_cursor = 0
    supervisor.stale_jobs = 0
    supervisor.repairs = []
    supervisor.attention = []
    supervisor.statuses = []
    supervisor.selected_test_ids = []
    supervisor.active_tier = "edit-time"
    supervisor._selected_verification = lambda _client, _event, _trigger: {
        "status": "UNKNOWN",
        "reason": "incomplete compiler impact",
    }
    event = {**_event(), "reconciled": True, "impact_complete": False, "impact": None}
    result = supervisor._process_event(SimpleNamespace(request=lambda *_args: {"generation": 7}), event)
    assert result["status"] == "UNKNOWN"


class _GenerationStatusServer:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.generation = 7
        self.published_monotonic: float | None = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._socket.bind(str(path))
        self._socket.listen(4)
        self._socket.settimeout(0.05)
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                client, _address = self._socket.accept()
            except TimeoutError:
                continue
            with client:
                while b"\n" not in (data := client.recv(4096)):
                    if not data:
                        break
                with self._lock:
                    generation = self.generation
                client.sendall(
                    (
                        json.dumps(
                            {"id": 1, "ok": True, "result": {"generation": generation}}
                        )
                        + "\n"
                    ).encode()
                )

    def publish(self, generation: int) -> None:
        with self._lock:
            self.generation = generation
            self.published_monotonic = time.monotonic()

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=0.5)
        self._socket.close()
        self.path.unlink(missing_ok=True)


def _linux_process_state(pid: int) -> str | None:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
    except OSError:
        return None
    return stat[stat.rfind(")") + 2 : stat.rfind(")") + 3]


def test_superseding_generation_cancels_owned_process_group(tmp_path: Path) -> None:
    supervisor = _supervisor()
    supervisor.current_generation = 7
    supervisor.settings = {"candidate_identity": "candidate:test"}
    supervisor.config = SimpleNamespace(state_dir=None)
    supervisor._native_resource_transition(
        event=_event(),
        candidate="candidate:test",
        verifier_id="warm-session",
        outcome="Unknown",
        evidence_status="NotRun",
        has_outcome=False,
        queue_remaining=0,
    )
    server = _GenerationStatusServer(tmp_path / "generation.sock")
    pid_path = tmp_path / "tree-pids"
    work = _event()
    trigger = {"id": "cancel-on-edit", "verifier_ids": ["verifier:test"]}
    published = threading.Thread(
        target=lambda: (
            time.sleep(0.15),
            server.publish(8),
        ),
        daemon=True,
    )
    program = (
        "import os, subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
        f"open({str(pid_path)!r}, 'w').write(f'{{os.getpid()}} {{child.pid}}')\n"
        "time.sleep(30)\n"
    )
    runner = LocalProcessRunner()
    failure: ForgeError | None = None
    try:
        with supervisor._job_scope(
            work,
            trigger,
            "micro_verifier",
            client=LanguageServiceSocket(server.path, timeout=0.5),
        ):
            published.start()
            try:
                runner.execute(
                    [sys.executable, "-c", program],
                    cwd=tmp_path,
                    timeout=15,
                    output_cap=1024,
                    environment=dict(os.environ),
                )
            except ForgeError as error:
                failure = error
    finally:
        published.join(timeout=1)
        server.close()

    assert failure is not None and failure.code == "EXECUTION_CANCELLED"
    cancellation = failure.details["execution_cancellation"]
    assert cancellation["cancellation_requested"] is True
    assert cancellation["superseded"] is True
    assert isinstance(cancellation["request_to_termination_request_seconds"], float)
    assert server.published_monotonic is not None
    assert time.monotonic() - server.published_monotonic < 1.5
    assert supervisor.current_generation == 8
    pids = [int(value) for value in pid_path.read_text(encoding="ascii").split()]
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline:
        states = [_linux_process_state(pid) for pid in pids]
        if all(state in {None, "Z"} for state in states):
            break
        time.sleep(0.01)
    assert all(state in {None, "Z"} for state in states)

    native = supervisor._resource_semantics
    outcome = native.resource_outcome(
        {
            "systemd_result": "unknown",
            "cleanup_known": True,
            "cleanup_succeeded": True,
            "cancellation_requested": cancellation["cancellation_requested"],
            "superseded": cancellation["superseded"],
        }
    )
    assert outcome.status == "Stale"
    transition = supervisor._native_resource_transition(
        event={**work, "current_generation": 8},
        candidate="candidate:test",
        verifier_id="verifier:test",
        outcome=outcome.status,
        evidence_status="Unknown",
        has_outcome=True,
        queue_remaining=0,
        work_generation=7,
        current_generation=8,
    )
    assert transition.disposition == "DiscardStale"
    assert transition.evidence_status == "Unknown"

    supervisor.settings = {"candidate_identity": "candidate:test", "triggers": []}
    supervisor.current_source_identity = None
    supervisor.current_cursor = 0
    next_generation = supervisor._process_event(
        SimpleNamespace(request=lambda *_args: {"generation": 8}),
        {**work, "current_generation": 8},
    )
    assert next_generation["status"] == "PASS"


def test_reconciled_security_trigger_runs_bounded_verifier_and_reuses_evidence(
    config, project
) -> None:
    configured = with_native_latency_allowance(
        replace(
            config,
            raw={
                **config.raw,
                "native": {"mode": "off"},
                "continuous": {
                    "enabled": True,
                    "triggers": [
                        {
                            "id": "offline-security",
                            "action": "security_micro_verifier",
                            "maximum_cost": "low",
                            "security": True,
                            "event_kinds": ["security_boundary_changed"],
                            "verifier_ids": ["verify-security-pass"],
                            "complete_dependency_envelope": True,
                        }
                    ],
                },
            },
        )
    )
    # The coordinator's trigger/evidence behavior is exercised with a fixture
    # Runner. Resource-envelope enforcement has separate fake-cgroup tests, so
    # this test never starts a host systemd scope.
    class FixtureRunner(LocalProcessRunner):
        def run(
            self,
            command,
            *,
            cwd,
            timeout,
            output_cap,
            stderr_cap=None,
            environment,
            stdin=b"",
        ):
            request = json.loads(stdin)
            response = {
                "protocol_version": "0.1",
                "type": "analysis_response",
                "request_id": request["request_id"],
                "provider": {
                    "id": "fake-security_pass",
                    "name": "fake-security_pass",
                    "version": "1",
                    "identity": "fake-security_pass",
                },
                "status": "PASS",
                "summary": "bounded fake security verifier",
                "witnesses": [],
                "limitations": [],
                "extensions": {
                    "mncs_forge": {
                        "assumptions": [],
                        "dependency_envelope": {
                            "paths": ["reference/reference.py"],
                            "identities": {},
                            "complete": True,
                        },
                    }
                },
            }
            stdout = json.dumps(response, sort_keys=True, separators=(",", ":")).encode()
            result = ExecutionResult(
                argv=list(command),
                returncode=0,
                stdout=stdout,
                stderr=b"",
                duration_seconds=0.001,
            )
            builder = ExecutionObservationBuilder(
                argv=list(command),
                cwd=cwd,
                timeout=timeout,
                stdout_limit=output_cap,
                stderr_limit=stderr_cap or output_cap,
                environment=environment,
                stdin=stdin,
                capabilities=self.inspect_capabilities(),
                runner_identity=self.runner_identity,
                runner_version="1",
                executable_identity=None,
            )
            builder.process_started()
            builder.feed("stdout", stdout)
            builder.completed(result)
            return builder.session(result, None)

    forge = Forge(
        configured,
        record_store=LocalRecordStore(project / ".local-state"),
        runner=FixtureRunner(),  # type: ignore[arg-type]
    )
    forge.epoch_begin(generator_identity="generator-v1", evaluator_identity="evaluator-v1")
    candidate = forge.candidate_register(
        changed_files=["candidate/main.py"],
        hypothesis="bounded security trigger fixture",
        generator_identity="generator-v1",
        generator_config_identity="generator-config-v1",
    )
    supervisor = ContinuousSupervisor(forge)
    supervisor.settings["candidate_identity"] = str(candidate["candidate_id"])
    event = {
        "current_generation": 2,
        "cursor": 1,
        "reconciled": True,
        "current": {
            "uri": f"file://{project / 'candidate/main.py'}",
            "identity": "mncs:source:offline-edit",
        },
        "semantic_subjects": [],
        "diagnostics": {"added": [], "resolved": []},
        "obligations": {"added": [], "resolved": [], "status_changed": []},
        "impact_complete": False,
        "impact": {"change_kinds": [], "risk_flags": ["effect_semantics"], "nodes": []},
    }
    trigger = supervisor.settings["triggers"][0]
    assert supervisor._matches(trigger, event)  # type: ignore[arg-type]

    client = SimpleNamespace(request=lambda method, _params: {"generation": 2})
    cold = supervisor._process_event(client, event)
    warm = supervisor._process_event(client, {**event, "cursor": 2})
    assert cold["status"] == "PASS"
    assert cold["actions"][0]["result"]["results"][0]["reused"] is False  # type: ignore[index]
    assert warm["status"] == "PASS"
    assert warm["actions"][0]["result"]["results"][0]["reused"] is True  # type: ignore[index]
    assert supervisor.recomputed_evidence == 1
    assert supervisor.reused_evidence == 1

    unrelated = {
        **event,
        "cursor": 2,
        "current": {
            "uri": f"file://{project / 'contract/contract.md'}",
            "identity": "mncs:source:unrelated-edit",
        },
    }
    unrelated_result = supervisor._micro(unrelated, trigger)  # type: ignore[arg-type]
    assert unrelated_result["results"][0]["reused"] is True  # type: ignore[index]
    assert supervisor.reused_evidence == 2

    (project / "reference/reference.py").write_text("VALUE = 2\n", encoding="utf-8")
    dependency_change = {
        **event,
        "cursor": 3,
        "current": {
            "uri": f"file://{project / 'reference/reference.py'}",
            "identity": "mncs:source:dependency-edit",
        },
    }
    invalidated = supervisor._micro(dependency_change, trigger)  # type: ignore[arg-type]
    assert invalidated["results"][0]["reused"] is False  # type: ignore[index]
    assert supervisor.recomputed_evidence == 2


def test_ravel_plan_passes_existing_commons_family_overlay(tmp_path: Path) -> None:
    output_commands: list[list[str]] = []

    class DevelopmentService:
        @staticmethod
        def _validate_verification_plan(_plan: dict[str, object]) -> None:
            return None

    class Config:
        root = tmp_path
        continuous_settings = {
            "library_paths": [],
            "cross_repository": True,
            "family_graph_file": "family/semantic-edges-v1.json",
            "commons_root": "../MNCS-Commons",
            "repository": "mncs-forge",
            "obligation_inventory": "family/obligations.json",
            "current_evidence": "family/evidence.json",
            "obligation_output": "family/verification-plan.json",
        }

        @staticmethod
        def public_commands() -> dict[str, list[str]]:
            return {"ravel_impact": ["ravel"], "mncs": ["mncs"]}

    supervisor = object.__new__(ContinuousSupervisor)
    supervisor.config = Config()
    supervisor.settings = Config.continuous_settings
    supervisor.forge = SimpleNamespace(
        _mncs_development_service=DevelopmentService(),
    )

    def run(command: list[str], *, cwd: Path, timeout: float) -> dict[str, object]:
        del cwd, timeout
        output_commands.append(command)
        output = Path(command[command.index("--output") + 1])
        output.write_text(json.dumps({"selection": {"selected_test_identities": []}}))
        return {"returncode": 0, "stderr": ""}

    supervisor._run_command = run  # type: ignore[method-assign]
    (tmp_path / "run").mkdir()
    plan = supervisor._ravel_plan(
        {
            "impact": {
                "roots": ["mncs:fn:producer"],
                "change_kinds": ["cross_repository_contract"],
            }
        },
        tmp_path / "producer.mncs",
        tmp_path / "run",
    )
    assert plan["selection"]["selected_test_identities"] == []  # type: ignore[index]
    command = output_commands[0]
    assert "--cross-repository" in command
    assert command[command.index("--family-graph") + 1].endswith(
        "family/semantic-edges-v1.json"
    )
    assert command[command.index("--commons-root") + 1].endswith("/MNCS-Commons")
    assert command[command.index("--repository") + 1] == "mncs-forge"
