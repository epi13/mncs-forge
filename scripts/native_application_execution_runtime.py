#!/usr/bin/env python3
"""Enter the selected Forge runtime for the typed native-app provider."""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYTHON = ROOT / ".venv" / "bin" / "python"
PROVIDER = ROOT / "scripts" / "native_application_execution_provider.py"


def main() -> None:
    if not PYTHON.is_file() or not os.access(PYTHON, os.X_OK):
        raise SystemExit(f"Forge project runtime is unavailable: {PYTHON}")
    if not PROVIDER.is_file():
        raise SystemExit(f"native application provider is unavailable: {PROVIDER}")
    os.execv(str(PYTHON), [str(PYTHON), str(PROVIDER), *sys.argv[1:]])


if __name__ == "__main__":
    main()
