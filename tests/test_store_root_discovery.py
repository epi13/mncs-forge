from __future__ import annotations

from pathlib import Path

from mncs_forge.store_record_store import _discover_store_root


def test_nested_worktree_resolves_store_from_workspace_root(tmp_path: Path) -> None:
    workspace = tmp_path / "projects"
    store_root = workspace / "mncs-store"
    (store_root / "python" / "mncs_store").mkdir(parents=True)
    module_file = (
        workspace
        / "mncs-forge"
        / ".worktrees"
        / "campaign"
        / "src"
        / "mncs_forge"
        / "store_record_store.py"
    )

    assert _discover_store_root(module_file) == store_root
