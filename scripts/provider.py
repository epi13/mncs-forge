#!/usr/bin/env python3
"""Invoke Forge's existing CLI against the explicitly selected provider closure."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mncs_forge.config import load_config
from mncs_forge.resident import selected_environment, selected_identity


def main() -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--config", required=True, type=Path)
    selected, _ = parser.parse_known_args()
    identity = selected_identity(load_config(selected.config))
    environment = selected_environment(identity)
    os.environ.clear()
    os.environ.update(environment)
    sys.path.insert(0, str(Path(identity["runtime"]["MNCS_STORE_ROOT"]) / "python"))
    from mncs_forge.cli import main as forge_main

    return forge_main()


if __name__ == "__main__":
    raise SystemExit(main())
