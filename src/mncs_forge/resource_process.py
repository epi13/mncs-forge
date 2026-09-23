"""Apply a private allowlisted environment, then exec one declared command.

This tiny transport helper is launched inside Forge's systemd cgroup. Values
travel in a mode-0600 runtime file rather than in the transient unit's
ExecStart argv or environment-property text.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

MAX_ENVIRONMENT_BYTES = 1024 * 1024
CURRENT_CGROUP_PARENT_PLACEHOLDER = "@mncs.current-cgroup@"


def cgroup_v2_parent(membership: str) -> str | None:
    """Return the current process's absolute unified-cgroup path, if present."""

    for line in membership.splitlines():
        hierarchy, separator, path = line.partition("::")
        if separator and hierarchy == "0" and path.startswith("/"):
            return path
    return None


def bind_current_cgroup_parent(command: list[str], membership: str) -> list[str] | None:
    """Bind a Podman cgroup-parent placeholder to this delegated service."""

    marker = f"--cgroup-parent={CURRENT_CGROUP_PARENT_PLACEHOLDER}"
    if marker not in command:
        return command
    parent = cgroup_v2_parent(membership)
    if parent is None:
        return None
    return [f"--cgroup-parent={parent}" if argument == marker else argument for argument in command]


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if len(arguments) < 2:
        return 126
    environment_path = Path(arguments[0])
    command = arguments[1:]
    try:
        with environment_path.open("rb") as stream:
            encoded = stream.read(MAX_ENVIRONMENT_BYTES + 1)
        environment_path.unlink(missing_ok=True)
        if len(encoded) > MAX_ENVIRONMENT_BYTES:
            return 126
        environment = json.loads(encoded)
    except (OSError, UnicodeError, ValueError, TypeError):
        return 126
    if not isinstance(environment, dict) or any(
        not isinstance(key, str)
        or not isinstance(value, str)
        or not key
        or "=" in key
        or "\x00" in key
        or "\x00" in value
        for key, value in environment.items()
    ):
        return 126
    if any(
        argument == f"--cgroup-parent={CURRENT_CGROUP_PARENT_PLACEHOLDER}" for argument in command
    ):
        try:
            membership = Path("/proc/self/cgroup").read_text(encoding="ascii")
        except OSError:
            return 126
        bound_command = bind_current_cgroup_parent(command, membership)
    else:
        bound_command = command
    if bound_command is None:
        return 126
    try:
        os.execvpe(bound_command[0], bound_command, environment)
    except OSError:
        return 127
    return 127  # pragma: no cover - execvpe replaces this process on success


if __name__ == "__main__":  # pragma: no cover - launched by systemd
    raise SystemExit(main())
