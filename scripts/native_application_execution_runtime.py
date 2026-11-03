#!/usr/bin/env python3
"""Enter the selected Forge runtime for the typed native-app provider."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROVIDER = ROOT / "scripts" / "native_application_execution_provider.py"


def runtime_python(root: Path) -> Path:
    """Reuse the repository runtime while executing this exact worktree source."""
    python = root / ".venv/bin/python"
    if python.is_file() and os.access(python, os.X_OK):
        return python
    # A linked checkout shares its repository's installed dependencies, not
    # another checkout's provider script or mutable source selection.
    if (root / ".git").is_file():
        result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--path-format=absolute", "--git-common-dir"],
            capture_output=True, text=True, timeout=5, check=True,
        )
        python = Path(result.stdout.strip()).resolve().parent / ".venv/bin/python"
    if not python.is_file() or not os.access(python, os.X_OK):
        raise SystemExit(f"Forge project runtime is unavailable: {python}")
    return python


def main() -> None:
    python = runtime_python(ROOT)
    if not PROVIDER.is_file():
        raise SystemExit(f"native application provider is unavailable: {PROVIDER}")
    os.execv(str(python), [str(python), str(PROVIDER), *sys.argv[1:]])


if __name__ == "__main__":
    main()
