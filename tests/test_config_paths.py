from __future__ import annotations

import json
from pathlib import Path

import pytest

from mncs_forge.config import ForgeConfig, load_config, validate_config_data
from mncs_forge.engine import Forge
from mncs_forge.errors import ForgeError
from mncs_forge.execution import validate_argv
from mncs_forge.mncs_native import NativeForgeAdapter
from mncs_forge.paths import resolve_contained, validate_relative_path


def test_configuration_validates(config: object) -> None:
    assert config is not None


def test_configuration_defaults_remain_compatible(config: ForgeConfig) -> None:
    assert config.verifier_limits == {
        "max_batch": 8,
        "request_bytes": 65536,
        "batch_timeout_seconds": 2.0,
        "witness_bytes": 32768,
        "stderr_bytes": 4096,
        "result_bytes": 131072,
        "max_changed_paths": 64,
        "max_dependency_identities": 64,
        "max_question_parameters": 32,
        "workspace_bytes": 1073741824,
        "max_workspace_entries": 50000,
    }
    project_workflow = config.workflows["project-check"]
    assert project_workflow.disclosure == "compact"
    assert project_workflow.subject == "project"
    candidate_workflow = config.workflows["pass-check"]
    assert candidate_workflow.disclosure == "compact"
    assert candidate_workflow.subject == "candidate"
    provider = config.providers["provider-pass"]
    assert provider.transport == "stdio-jsonl"
    assert provider.required is False


def test_continuous_micro_verifier_declarations_have_an_event_capacity(
    config: ForgeConfig,
) -> None:
    raw = {
        **config.raw,
        "continuous": {
            "enabled": True,
            "triggers": [
                {
                    "id": "bounded-micro",
                    "action": "micro_verifier",
                    "maximum_cost": "low",
                    "verifier_ids": [f"verifier-{index}" for index in range(17)],
                }
            ],
        },
    }
    with pytest.raises(ForgeError, match="per-event capacity of 16"):
        validate_config_data(raw)


def test_nested_configuration_can_scope_an_ancestor_project(project: Path) -> None:
    nested = project / "integration" / "forge"
    nested.mkdir(parents=True)
    path = nested / "mncs-forge.toml"
    path.write_text(
        (project / "mncs-forge.toml")
        .read_text(encoding="utf-8")
        .replace('root = "."', 'root = "../.."', 1),
        encoding="utf-8",
    )

    config = load_config(path)

    assert config.root == project.resolve()
    assert config.paths("candidates") == [(project / "candidate").resolve()]


def test_native_execution_mode_is_explicit_and_environment_overridable(
    config: ForgeConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MNCS_FORGE_NATIVE_MODE", raising=False)
    assert config.native_execution_mode == "prefer"
    for mode in ("off", "prefer", "required"):
        monkeypatch.setenv("MNCS_FORGE_NATIVE_MODE", mode)
        assert config.native_execution_mode == mode
    monkeypatch.setenv("MNCS_FORGE_NATIVE_MODE", "invalid")
    with pytest.raises(ForgeError, match="native execution mode"):
        _ = config.native_execution_mode


def test_required_native_mode_fails_during_forge_startup(
    config: ForgeConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MNCS_FORGE_NATIVE_MODE", "required")

    def unavailable(_adapter: NativeForgeAdapter) -> None:
        raise ForgeError("NATIVE_UNAVAILABLE", "mncs-language checkout is unavailable")

    monkeypatch.setattr(NativeForgeAdapter, "ensure_available", unavailable)

    with pytest.raises(ForgeError, match="mncs-language checkout is unavailable"):
        Forge(config)


def test_runtime_state_override_is_outside_source_tree(
    config: ForgeConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime_root = tmp_path.parent / "forge-runtime-state"
    monkeypatch.setenv("MNCS_FORGE_STATE_DIR", str(runtime_root))
    assert config.state_dir == (runtime_root / config.project_identity).resolve()
    assert config.root not in config.state_dir.parents


def test_provider_defaults_are_applied_when_optional_fields_are_absent(project: Path) -> None:
    path = project / "mncs-forge.toml"
    text = path.read_text(encoding="utf-8")
    text = text.replace('transport = "stdio-jsonl"\n', "", 1)
    text = text.replace("required = false\n", "", 1)
    config = load_config(path)

    provider = config.providers["provider-pass"]
    assert provider.transport == "stdio-jsonl"
    assert provider.required is False


def test_schema_copies_are_identical() -> None:
    root = Path(__file__).parents[1]
    assert json.loads((root / "schemas/mncs-forge-config.schema.json").read_text()) == json.loads(
        (root / "src/mncs_forge/resources/mncs-forge-config.schema.json").read_text()
    )


@pytest.mark.parametrize(
    ("content", "code"),
    [
        ("version = [", "CONFIG_INVALID"),
        ("version = 999\n", "CONFIG_INVALID"),
        ("version = 1\n[unknown]\nvalue = true\n", "CONFIG_INVALID"),
    ],
)
def test_malformed_or_unsupported_configuration_has_stable_error(
    tmp_path: Path, content: str, code: str
) -> None:
    path = tmp_path / "mncs-forge.toml"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(ForgeError) as issue:
        load_config(path)
    assert issue.value.code == code


def test_unreadable_configuration_has_stable_error(tmp_path: Path) -> None:
    with pytest.raises(ForgeError) as issue:
        load_config(tmp_path / "missing.toml")
    assert issue.value.code == "CONFIG_READ"


@pytest.mark.parametrize("value", ["../secret", "candidate/../../secret", "/tmp/secret"])
def test_traversal_and_absolute_rejected(value: str) -> None:
    with pytest.raises(ForgeError):
        validate_relative_path(value)


def test_symlink_escape_rejected(project: Path, tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    (project / "candidate/link").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ForgeError, match="escapes"):
        resolve_contained(project, "candidate/link/file", must_exist=False)


def test_invalid_argument_types_rejected() -> None:
    with pytest.raises(ForgeError, match="argument"):
        validate_argv(["python", 7])


def test_protected_writable_overlap_rejected(project: Path) -> None:
    path = project / "mncs-forge.toml"
    text = path.read_text(encoding="utf-8").replace(
        'protected = ["protected"]', 'protected = ["candidate"]'
    )
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ForgeError, match="overlap"):
        load_config(path)


def test_undeclared_provider_command_rejected(project: Path) -> None:
    path = project / "mncs-forge.toml"
    text = path.read_text(encoding="utf-8")
    provider = text.index("[[providers]]")
    command = text.index("command = ", provider)
    command_end = text.index("\n", command)
    text = text[:command] + 'command = ["not-declared"]' + text[command_end:]
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ForgeError):
        load_config(path)


@pytest.mark.parametrize("reserved", ["shared", "by_verifier"])
def test_batch_parameter_envelope_keys_are_reserved(project: Path, reserved: str) -> None:
    path = project / "mncs-forge.toml"
    text = path.read_text(encoding="utf-8").replace(
        'parameter_keys = ["note"]',
        f'parameter_keys = ["{reserved}"]',
        1,
    )
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ForgeError) as issue:
        load_config(path)
    assert issue.value.code == "CONFIG_INVALID"


def test_relative_provider_symlink_escape_is_unavailable(project: Path, tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-provider-outside"
    outside.write_text("#!/bin/sh\n", encoding="utf-8")
    outside.chmod(0o755)
    (project / "candidate/provider-link").symlink_to(outside)
    path = project / "mncs-forge.toml"
    text = path.read_text(encoding="utf-8")
    marker = "[[providers]]\n"
    first = text.index(marker)
    command = text.index("command = ", first)
    end = text.index("\n", command)
    text = text[:command] + 'command = ["candidate/provider-link"]' + text[end:]
    workflow = text.index("[[workflows]]", end)
    workflow_command = text.index("command = ", workflow)
    workflow_end = text.index("\n", workflow_command)
    text = text[:workflow_command] + 'command = ["candidate/provider-link"]' + text[workflow_end:]
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ForgeError) as issue:
        load_config(path)
    assert issue.value.code == "SYMLINK_ESCAPE"


def test_family_module_root_resolves_sibling_and_rejects_escape(tmp_path: Path) -> None:
    from mncs_forge.paths import FAMILY_MODULE_ROOTS_MECHANISM, resolve_family_module_root

    workspace = tmp_path / "Projects"
    project = workspace / "mncs-forge-project"
    sibling = workspace / "machine-native-complexity-standard" / "src"
    sibling.mkdir(parents=True)
    (sibling / "mncs_validator").mkdir()
    (sibling / "mncs_validator" / "__init__.py").write_text("", encoding="utf-8")
    project.mkdir()
    resolved = resolve_family_module_root(project, "../machine-native-complexity-standard/src")
    assert resolved == sibling.resolve()
    with pytest.raises(ForgeError) as escaped:
        resolve_family_module_root(project, "../../outside")
    assert escaped.value.code == "FAMILY_MODULE_ROOT_ESCAPE"
    with pytest.raises(ForgeError) as absolute:
        resolve_family_module_root(project, str(sibling))
    assert absolute.value.code == "ABSOLUTE_PATH"
    assert FAMILY_MODULE_ROOTS_MECHANISM.startswith("mncs-forge.family-module-roots")
