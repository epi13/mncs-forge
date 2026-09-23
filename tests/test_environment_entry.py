from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from mncs_forge import continuous
from mncs_forge.cli import _common_parser
from mncs_forge.config import load_config


def test_continuous_enter_composes_bounded_resident_capsule(
    project: Path, monkeypatch
) -> None:
    config = load_config(project / "mncs-forge.toml")
    config = replace(
        config,
        raw={
            **config.raw,
            "continuous": {
                "enabled": True,
                "language_service_socket": ".mncs/mnls-language-service.sock",
            },
        },
    )
    calls: list[str] = []

    def lifecycle(_config, action: str, *, mode: str) -> dict[str, object]:
        calls.append(action)
        if action == "start":
            return {"state": "started", "supervisor": {"state": "running", "pid": 42}}
        return {
            "schema_version": "mncs.continuous-lifecycle/1",
            "language_service": {
                "state": "running",
                "pid": 41,
                "status": {
                    "workspace_root": str(project),
                    "stream_identity": "stream-1",
                    "generation": 7,
                    "event_cursor": 3,
                },
            },
            "supervisor": {"state": "running", "pid": 42},
            "continuous": {
                "workspace_generation": 7,
                "counts": {"PASS": 2, "FAIL": 0, "UNKNOWN": 0},
                "blocking_attention_events": [],
                "resources": {
                    "state": "protected",
                    "resource_envelope_identity": "envelope-1",
                    "aggregate_process_count": 0,
                },
                "active_job": {
                    "generation": 7,
                    "elapsed_seconds": 1.25,
                    "resource_protection_state": "protected",
                },
                "queue": {"depth": 0, "capacity": 1},
            },
        }

    class Socket:
        def __init__(self, _path: Path, *, timeout: float) -> None:
            assert timeout == 8.0

        def request(self, method: str, params: dict[str, object]) -> dict[str, object]:
            assert method == "family_agent_context"
            assert params == {"max_items": 16}
            return {
                "schema_version": "mncs.family-agent-context/2",
                "status": "complete",
                "repository": {"manifest_identity": "manifest-1"},
                "language": {"content_identity": "language-1"},
                "architecture": {"content_identity": "architecture-1"},
                "verification": {"state": "current"},
                "completeness": {"state": "complete"},
                "pressures": [],
                "negative_knowledge": [{"identity": "negative-1"}],
            }

    monkeypatch.setattr(continuous, "continuous_lifecycle", lifecycle)
    monkeypatch.setattr(continuous, "LanguageServiceSocket", Socket)

    capsule = continuous.environment_enter(config)

    assert calls == ["start", "status"]
    assert capsule["schema_version"] == "mncs.environment-entry/1"
    assert capsule["workspace_generation"] == 7
    assert capsule["task_environment"]["continuous_status"]["resources"]["state"] == (
        "protected"
    )
    assert capsule["task_environment"]["continuous_status"]["active_job"]["generation"] == 7
    assert capsule["task_environment"]["continuous_status"]["queue"]["capacity"] == 1
    assert capsule["language_identity"] == "language-1"
    assert capsule["architecture_identity"] == "architecture-1"
    assert capsule["resident_services"] == {
        "language_service": {
            "state": "running",
            "pid": 41,
            "stream_identity": "stream-1",
        },
        "forge_supervisor": {"state": "running", "pid": 42, "resources": {}},
    }
    assert capsule["negative_knowledge"] == [{"identity": "negative-1"}]
    assert "family_agent_context" in capsule["query_handles"]


def test_continuous_enter_is_a_bounded_cli_command() -> None:
    arguments = _common_parser().parse_args(["continuous", "enter"])
    assert arguments.command == "continuous"
    assert arguments.continuous_command == "enter"
