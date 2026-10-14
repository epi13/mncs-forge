#!/usr/bin/env python3
"""Enter the Forge checkout's installed runtime for Environment capabilities.

Environment launches declared Python scripts with its own interpreter. Forge
has a separate project environment with Forge's declared dependencies, so this
small dependency-free trampoline replaces itself with that selected runtime.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYTHON = ROOT / ".venv/bin/python"
WORKFLOW = ROOT / "scripts/compiler_vm_forge_workflow.py"


def main() -> None:
    if not PYTHON.is_file() or not os.access(PYTHON, os.X_OK):
        raise SystemExit(f"Forge project runtime is unavailable: {PYTHON}")
    if not WORKFLOW.is_file():
        raise SystemExit(f"Forge workflow is unavailable: {WORKFLOW}")
    os.execv(str(PYTHON), [str(PYTHON), str(WORKFLOW), *sys.argv[1:]])


if __name__ == "__main__":
    main()
