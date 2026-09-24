from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from mncs_forge import cli
from mncs_forge.errors import ForgeError
from mncs_forge.workspace_binding import resolve_workspace_config


EXAMPLE_CONFIG = Path(__file__).parents[1] / "examples" / "minimal" / "mncs-forge.toml"


def _workspace(path: Path, *, nested: bool = False) -> Path:
    path.mkdir(parents=True)
    config_dir = path / "integration" / "forge" if nested else path
    config_dir.mkdir(parents=True, exist_ok=True)
    text = EXAMPLE_CONFIG.read_text(encoding="utf-8")
    if nested:
        text = text.replace('root = "."', 'root = "../.."', 1)
    (config_dir / "mncs-forge.toml").write_text(text, encoding="utf-8")
    return path.resolve()


def test_explicit_workspace_resolution_is_independent_of_current_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = _workspace(tmp_path / "workspace-a", nested=True)
    neutral = tmp_path / "neutral"
    neutral.mkdir()
    monkeypatch.chdir(neutral)

    config = resolve_workspace_config(workspace)

    assert config.root == workspace
    assert config.config_path == workspace / "integration" / "forge" / "mncs-forge.toml"


def test_cli_resolves_workspace_before_loading_current_directory_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = _workspace(tmp_path / "workspace-a")
    neutral = tmp_path / "neutral"
    neutral.mkdir()
    monkeypatch.chdir(neutral)
    observed: list[Path] = []

    def enter(config, *, mode: str) -> dict[str, object]:
        assert mode == "development"
        observed.append(config.root)
        return {"workspace_root": str(config.root)}

    monkeypatch.setattr(cli, "environment_enter", enter)
    code, value = cli.run(["continuous", "enter", str(workspace)])

    assert code == 0
    assert value == {"workspace_root": str(workspace)}
    assert observed == [workspace]


def test_workspace_binding_fails_closed_for_mismatch_and_ambiguity(
    tmp_path: Path,
) -> None:
    workspace_a = _workspace(tmp_path / "workspace-a")
    workspace_b = _workspace(tmp_path / "workspace-b")
    with pytest.raises(ForgeError, match="not requested workspace") as mismatch:
        resolve_workspace_config(workspace_a, explicit_config=workspace_b / "mncs-forge.toml")
    assert mismatch.value.code == "WORKSPACE_BINDING_MISMATCH"

    duplicate = workspace_a / "integration" / "forge"
    duplicate.mkdir(parents=True)
    shutil.copyfile(workspace_a / "mncs-forge.toml", duplicate / "mncs-forge.toml")
    duplicate_text = (duplicate / "mncs-forge.toml").read_text(encoding="utf-8")
    duplicate_text = duplicate_text.replace('root = "."', 'root = "../.."', 1)
    (duplicate / "mncs-forge.toml").write_text(duplicate_text, encoding="utf-8")
    with pytest.raises(ForgeError, match="multiple Forge configurations") as ambiguous:
        resolve_workspace_config(workspace_a)
    assert ambiguous.value.code == "WORKSPACE_BINDING_AMBIGUOUS"
