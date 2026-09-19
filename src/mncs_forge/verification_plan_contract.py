"""Lazy boundary to the canonical MNCS-Commons verification-plan contract."""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from types import ModuleType
from typing import Any, cast


def _module() -> ModuleType:
    configured = os.environ.get("MNCS_COMMONS_ROOT")
    candidates = [Path(configured)] if configured else []
    candidates.append(Path(__file__).resolve().parents[3] / "MNCS-Commons")
    for root in candidates:
        source = root / "src"
        module_path = source / "mncs_commons" / "verification_plan.py"
        if module_path.is_file():
            name = "_mncs_commons_verification_plan_canonical"
            existing = sys.modules.get(name)
            if existing is not None:
                return existing
            spec = importlib.util.spec_from_file_location(name, module_path)
            if spec is None or spec.loader is None:
                raise RuntimeError(f"cannot load canonical verification-plan module: {module_path}")
            module = importlib.util.module_from_spec(spec)
            sys.modules[name] = module
            spec.loader.exec_module(module)
            return module
    raise RuntimeError(
        "canonical MNCS-Commons verification-plan contract is unavailable; "
        "set MNCS_COMMONS_ROOT to a checked-out Commons repository"
    )


def validate_plan(value: Any, **kwargs: Any) -> dict[str, Any]:
    return cast(dict[str, Any], _module().validate_plan(value, **kwargs))
