from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from mncs_forge.adapters import LocalProcessRunner
from mncs_forge.application.mncs_development import MncsDevelopmentService
from mncs_forge.errors import ForgeError

STUB = """\
import json, sys
from pathlib import Path

args = sys.argv[1:]
assert args[0] == "record", args
output = Path(args[args.index("--output") + 1])
seen = Path(args[args.index("--seen") + 1]) if "--seen" in args else None
mode = sys.argv[0] + ".mode"
witness_mode = Path(mode).read_text().strip() if Path(mode).exists() else "ok"
if seen is not None:
    seen.write_text(json.dumps(args), encoding="utf-8")
if witness_mode == "missing":
    sys.exit(0)
if witness_mode == "malformed":
    output.write_text(json.dumps({"schema_version": "other/9"}), encoding="utf-8")
    sys.exit(0)
witness = {
    "schema_version": "mncs.debug-witness/1",
    "witness_id": "witness-1",
    "execution_identity": "execution-1",
    "outcome": {"failure_class": "success"},
    "runtime": {
        "observation_identity": "observation-1",
        "observation": {
            "schema_version": "mncs.execution-observation/1",
            "identity": "observation-1",
            "execution_identity": "execution-1",
            "policy": {"capture": "bounded"},
            "completeness": {"events": "complete"},
            "events": [],
            "values": [],
            "frames": [],
            "effects": [],
        },
    },
}
output.write_text(json.dumps(witness), encoding="utf-8")
"""


def _stub_provider(project: Path, *, witness_mode: str = "ok") -> list[str]:
    stub = project / "candidate" / "stub-debug.py"
    stub.write_text(STUB, encoding="utf-8")
    (project / "candidate" / "stub-debug.py.mode").write_text(witness_mode, encoding="utf-8")
    return [sys.executable, str(stub)]


def _inputs(project: Path) -> tuple[str, str]:
    program = project / "candidate" / "program.mncs"
    program.write_text("program\n", encoding="utf-8")
    request = project / "candidate" / "request.json"
    request.write_text('{"entry": "main"}\n', encoding="utf-8")
    return str(program.relative_to(project)), str(request.relative_to(project))


def test_observe_returns_witness_with_forge_execution(config, project: Path) -> None:
    service = MncsDevelopmentService(config=config, executor=LocalProcessRunner())
    program, request = _inputs(project)
    observed = service.observe(
        program=program,
        request=request,
        debug_command=_stub_provider(project),
        mncs_binary="/repo/mncs",
        library_paths=["/repo/lib"],
        output_file="output/observation.json",
    )
    assert observed["schema_version"] == "mncs.forge-observation/1"
    assert observed["operation"] == "development.mncs.observe"
    assert observed["mode"] == "observe"
    witness = observed["witness"]
    assert witness["witness_id"] == "witness-1"
    assert witness["execution_identity"] == "execution-1"
    assert witness["observation_identity"] == "observation-1"
    assert witness["failure_class"] == "success"
    assert witness["observation_present"] is True
    assert witness["ref"]["schema_revision"] == "mncs.debug-witness/1"
    assert observed["witness_document"]["witness_id"] == "witness-1"
    execution = observed["execution"]
    assert execution["label"] == "mncs-debug-observe"
    assert execution["execution_mode"] == "observe"
    assert execution["command"][2:4] == ["record", str(project / "candidate" / "program.mncs")]
    assert "--executor" in execution["command"]
    assert execution["command"][execution["command"].index("--executor") + 1] == "local"
    assert execution["termination_category"] == "completed"
    assert observed["provenance"]["capture_policy"] == "bounded"
    assert observed["provenance"]["program"]["sha256"]
    assert observed["provenance"]["request"]["sha256"]
    assert observed["output_identity"]
    assert (project / "output/observation.json").is_file()


def test_observe_work_request_shape(config, project: Path) -> None:
    service = MncsDevelopmentService(config=config, executor=object())
    program, request = _inputs(project)
    work = service._observe_work_request(
        ["mncs-debug"],
        program=project / program,
        request=project / request,
        witness=project / "witness.json",
        cwd=project,
        mncs_binary="/repo/mncs",
        library_paths=["/repo/lib"],
        core_path=None,
        capture_policy="selected",
        max_events=10,
        max_values=20,
        max_value_bytes=30,
        selected_operations=["op-1", "op-2"],
        timeout=7.5,
        test_result=project / "candidate" / "request.json",
    )
    assert work.mode == "observe"
    assert work.argv[:4] == [
        "mncs-debug",
        "record",
        str(project / "candidate" / "program.mncs"),
        str(project / "candidate" / "request.json"),
    ]
    assert work.argv[work.argv.index("--timeout") + 1] == "7.5"
    assert work.argv[work.argv.index("--capture") + 1] == "selected"
    assert work.argv.count("--operation") == 2
    assert work.argv.count("--library") == 1
    assert "--core" not in work.argv
    assert work.argv[work.argv.index("--test-result") + 1] == str(
        project / "candidate" / "request.json"
    )
    assert work.environment_overlay == {}


def test_observe_rejects_bad_capture(config, project: Path) -> None:
    service = MncsDevelopmentService(config=config, executor=object())
    program, request = _inputs(project)
    with pytest.raises(ForgeError) as error:
        service.observe(program=program, request=request, capture_policy="everything")
    assert error.value.code == "MNCS_DEBUG_INPUT"
    with pytest.raises(ForgeError) as error:
        service.observe(program=program, request=request, capture_policy="selected")
    assert error.value.code == "MNCS_DEBUG_INPUT"


def test_observe_rejects_missing_witness(config, project: Path) -> None:
    service = MncsDevelopmentService(config=config, executor=LocalProcessRunner())
    program, request = _inputs(project)
    with pytest.raises(ForgeError) as error:
        service.observe(
            program=program,
            request=request,
            debug_command=_stub_provider(project, witness_mode="missing"),
        )
    assert error.value.code == "PROVIDER_CONTRACT_INVALID"


def test_observe_rejects_malformed_witness(config, project: Path) -> None:
    service = MncsDevelopmentService(config=config, executor=LocalProcessRunner())
    program, request = _inputs(project)
    with pytest.raises(ForgeError) as error:
        service.observe(
            program=program,
            request=request,
            debug_command=_stub_provider(project, witness_mode="malformed"),
        )
    assert error.value.code == "PROVIDER_CONTRACT_INVALID"


def test_observe_accepts_absolute_inputs_outside_project(
    config, project: Path, tmp_path: Path
) -> None:
    external = tmp_path.parent / f"{tmp_path.name}-external"
    external.mkdir(exist_ok=True)
    program = external / "program.mncs"
    program.write_text("program\n", encoding="utf-8")
    request = external / "request.json"
    request.write_text("{}\n", encoding="utf-8")
    service = MncsDevelopmentService(config=config, executor=LocalProcessRunner())
    observed = service.observe(
        program=str(program),
        request=str(request),
        debug_command=_stub_provider(project),
    )
    assert observed["witness"]["witness_id"] == "witness-1"
    assert observed["provenance"]["program"]["path"] == str(program)


def test_observe_cli_decodes_into_registered_input(project: Path) -> None:
    from mncs_forge.cli import _cli_payload, _common_parser
    from mncs_forge.operations import MncsObserveInput

    prefix = [sys.executable, str(project / "stub-debug.py")]
    args = _common_parser().parse_args(
        [
            "--config",
            str(project / "mncs-forge.toml"),
            "mncs",
            "observe",
            "candidate/program.mncs",
            "candidate/request.json",
            "--debug-command",
            json.dumps(prefix),
            "--mncs-binary",
            "/repo/mncs",
            "--library-paths",
            json.dumps(["/repo/lib"]),
            "--capture-policy",
            "failure-only",
            "--selected-operation",
            "op-1",
            "--timeout-seconds",
            "7.5",
        ]
    )
    assert args.operation_id == "development.mncs.observe"
    decoded = MncsObserveInput(**_cli_payload(args))
    assert decoded.program == "candidate/program.mncs"
    assert decoded.request == "candidate/request.json"
    assert decoded.debug_command == prefix
    assert decoded.mncs_binary == "/repo/mncs"
    assert decoded.library_paths == ["/repo/lib"]
    assert decoded.capture_policy == "failure-only"
    assert decoded.selected_operations == ["op-1"]
    assert decoded.timeout_seconds == 7.5
    assert decoded.max_events == 512
    assert decoded.witness_file == ".mncs-forge/mncs-observe-witness.json"
