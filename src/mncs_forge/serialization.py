"""Deterministic local serialization helpers.

Two-track encoding policy:

- Forge-persisted bytes (ledger lines, immutable records, transaction
  journals) and Forge-local content identities use :func:`canonical_bytes`.
  Its exact bytes are load-bearing for existing ledgers and snapshots: never
  change its output for previously encodable values.
- Identities over language-owned envelopes (MNCS execution receipts) use RFC
  8785 JCS through ``rfc8785`` at the verification site. That is the
  cross-implementation contract with the language-owned receipt producer, not
  a Forge-local choice.

New code must use one of these two tracks explicitly. Do not add a third
hand-rolled canonicalizer.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


def canonical_bytes(value: object) -> bytes:
    """Encode Forge-owned JSON deterministically.

    This is a local Forge encoding, not an MNCS canonical JSON identity.
    """

    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def pretty_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, indent=2) + "\n"


def local_json_identity(value: object) -> str:
    return "forge-json-sha256-v1:" + hashlib.sha256(canonical_bytes(value)).hexdigest()


def read_json(path: Path, *, byte_cap: int = 4_000_000) -> Any:
    data = path.read_bytes()
    if len(data) > byte_cap:
        raise ValueError(f"{path} exceeds the {byte_cap}-byte JSON read cap")
    return json.loads(data, object_pairs_hook=reject_duplicate_keys)


def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Keep duplicate JSON members from silently changing a record's meaning."""

    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key: {key}")
        result[key] = value
    return result
