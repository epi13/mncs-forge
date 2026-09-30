#!/usr/bin/env python3
"""Checkout-owned entrypoint for the resident provider contract."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mncs_forge.resident import main

if __name__ == "__main__":
    raise SystemExit(main())
