"""Canonical workspace-to-Forge configuration resolution."""

from __future__ import annotations

import os
from pathlib import Path

from .config import ForgeConfig, load_config
from .errors import ForgeError

_SKIP_DIRECTORIES = {
    ".git",
    ".mncs",
    ".mncs-forge",
    ".venv",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    "node_modules",
    "target",
    "vendor",
}
_MAX_SEARCHED_DIRECTORIES = 4096
_MAX_SEARCH_DEPTH = 8
_MAX_CONFIG_CANDIDATES = 64


def _candidate_configs(workspace: Path) -> list[Path]:
    """Perform bounded bootstrap discovery for nested Forge project metadata.

    The mature environment-entry path should resolve workspace identity from
    authoritative project/family metadata. Until that mapping is available,
    this no-follow walk supports existing nested integration configs while
    failing closed on broad, deep, or unreadable workspaces.
    """

    found: list[Path] = []
    visited = 0

    def fail_on_walk_error(error: OSError) -> None:
        location = error.filename or workspace
        raise ForgeError(
            "WORKSPACE_SCAN_UNKNOWN",
            f"cannot establish workspace configuration candidates below {location}: {error.strerror or error}",
            details={"workspace": str(workspace), "path": str(location)},
        ) from error

    for current, directories, filenames in os.walk(
        workspace,
        followlinks=False,
        onerror=fail_on_walk_error,
    ):
        visited += 1
        if visited > _MAX_SEARCHED_DIRECTORIES:
            raise ForgeError(
                "WORKSPACE_RESOLUTION_LIMIT",
                f"workspace scan exceeds {_MAX_SEARCHED_DIRECTORIES} directories",
                details={"workspace": str(workspace), "maximum_directories": _MAX_SEARCHED_DIRECTORIES},
            )
        current_path = Path(current)
        directories[:] = sorted(
            name
            for name in directories
            if name not in _SKIP_DIRECTORIES
            and not (current_path / name).is_symlink()
        )
        depth = len(current_path.relative_to(workspace).parts)
        if depth >= _MAX_SEARCH_DEPTH and directories:
            raise ForgeError(
                "WORKSPACE_RESOLUTION_LIMIT",
                f"workspace scan exceeds maximum depth {_MAX_SEARCH_DEPTH}",
                details={"workspace": str(workspace), "maximum_depth": _MAX_SEARCH_DEPTH},
            )
        if "mncs-forge.toml" in filenames:
            candidate = current_path / "mncs-forge.toml"
            if candidate.is_symlink():
                raise ForgeError(
                    "WORKSPACE_CONFIG_INVALID",
                    f"workspace Forge configuration must not be a symlink: {candidate}",
                    details={"config_path": str(candidate)},
                )
            found.append(candidate)
            if len(found) > _MAX_CONFIG_CANDIDATES:
                raise ForgeError(
                    "WORKSPACE_RESOLUTION_LIMIT",
                    f"workspace contains more than {_MAX_CONFIG_CANDIDATES} Forge configurations",
                    details={"workspace": str(workspace), "maximum_configs": _MAX_CONFIG_CANDIDATES},
                )
    return found


def resolve_workspace_config(
    workspace: Path | str,
    *,
    explicit_config: Path | str | None = None,
) -> ForgeConfig:
    """Resolve one authoritative Forge config that names ``workspace`` as root.

    A nested integration configuration is supported because its existing
    ``project.root`` metadata is authoritative. Current working directory is
    never consulted once an explicit workspace is provided.
    """

    try:
        root = Path(workspace).expanduser().resolve(strict=True)
    except OSError as error:
        raise ForgeError("WORKSPACE_NOT_FOUND", f"workspace cannot be resolved: {workspace}") from error
    if not root.is_dir():
        raise ForgeError("WORKSPACE_NOT_DIRECTORY", f"workspace is not a directory: {root}")
    if explicit_config is not None:
        config = load_config(explicit_config)
        if config.root != root:
            raise ForgeError(
                "WORKSPACE_BINDING_MISMATCH",
                f"Forge config {config.config_path} resolves to {config.root}, not requested workspace {root}",
                details={
                    "requested_workspace": str(root),
                    "configured_workspace": str(config.root),
                    "config_path": str(config.config_path),
                },
            )
        return config

    candidates = _candidate_configs(root)
    if not candidates:
        raise ForgeError(
            "WORKSPACE_CONFIG_NOT_FOUND",
            f"no Forge project configuration is bound to workspace {root}",
            details={"requested_workspace": str(root)},
        )
    matches: list[ForgeConfig] = []
    mismatches: list[tuple[Path, Path]] = []
    invalid: list[tuple[Path, ForgeError]] = []
    for candidate in candidates:
        try:
            config = load_config(candidate)
        except ForgeError as error:
            invalid.append((candidate, error))
            continue
        if config.root == root:
            matches.append(config)
        elif candidate.parent == root:
            mismatches.append((candidate, config.root))
    if invalid:
        candidate, error = invalid[0]
        raise ForgeError(
            "WORKSPACE_CONFIG_INVALID",
            f"cannot establish unique workspace binding because {candidate} is invalid: {error.message}",
            details={"config_path": str(candidate), "cause": error.code},
        ) from error
    if len(matches) > 1:
        raise ForgeError(
            "WORKSPACE_BINDING_AMBIGUOUS",
            f"multiple Forge configurations claim workspace {root}",
            details={"config_paths": [str(item.config_path) for item in matches]},
        )
    if matches:
        return matches[0]
    if mismatches:
        candidate, configured_root = mismatches[0]
        raise ForgeError(
            "WORKSPACE_BINDING_MISMATCH",
            f"Forge config {candidate} resolves to {configured_root}, not requested workspace {root}",
            details={
                "requested_workspace": str(root),
                "configured_workspace": str(configured_root),
                "config_path": str(candidate),
            },
        )
    raise ForgeError(
        "WORKSPACE_CONFIG_NOT_FOUND",
        f"no Forge project configuration is bound to workspace {root}",
        details={
            "requested_workspace": str(root),
            "candidate_configs": [str(item) for item in candidates[:32]],
        },
    )


def resolve_continuous_config(
    *,
    workspace: Path | str | None,
    explicit_config: Path | str | None,
) -> ForgeConfig:
    """One config-selection path for continuous enter/start/status/stop."""

    if workspace is not None:
        return resolve_workspace_config(workspace, explicit_config=explicit_config)
    return load_config(explicit_config or Path("mncs-forge.toml"))
