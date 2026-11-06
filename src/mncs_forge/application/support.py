"""Small shared application-level presentation and status helpers."""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from datetime import UTC, datetime

from ..errors import ForgeError
from ..execution import STATUSES

STATUS_ORDER = {"PASS": 0, "UNKNOWN": 1, "FAIL": 2}
CLAIM_CLASSES = (
    "mncs_implementation_result",
    "mncds_development_process_result",
    "local_reproduction",
    "operator_controlled_reproduction",
    "independent_evaluation",
    "protected_holdout",
    "witnessed_evidence",
    "operational_evidence",
    "governance_approval",
)
PUBLIC_LIMITATIONS = [
    "experimental non-normative reference implementation",
    "not required for MNCS conformance and not an accredited certification system",
    "cannot create independent evaluation, protected custody, witnessing, or governance approval",
    "local results do not promote MNCS, MNCDS, an RFC, or a case study",
    "REVIEW_REQUIRED is a workflow disposition, not an MNCS result",
    "missing or unsupported evidence remains UNKNOWN",
    "configured subprocesses are trusted providers; Forge is not an OS or network sandbox",
]
SECRET_PATTERN = re.compile(
    r"(?i)(token|secret|password|authorization|api[_-]?key)([\"'=:\s]+)([^\s,\"']+)"
)


def aggregate_status(
    statuses: Iterable[str],
    *,
    native_status_aggregate: Callable[[list[str]], str] | None = None,
) -> str:
    """Aggregate recognized statuses, delegating the fold when native is selected."""

    values = [status for status in statuses if status in STATUSES]
    if not values:
        return "UNKNOWN"
    if native_status_aggregate is not None:
        result = native_status_aggregate(values)
        if result not in STATUSES:
            raise ForgeError("NATIVE_STATUS_UNKNOWN", "native status aggregate is not recognized")
        return result
    return max(values, key=STATUS_ORDER.__getitem__)


def redact(text: str, limit: int = 4096) -> str:
    return SECRET_PATTERN.sub(r"\1\2<redacted>", text[:limit])


def now() -> str:
    return datetime.now(UTC).isoformat()
