"""License-evidence discovery as a Forge analysis domain.

Forge contributes **analysis evidence** to MNCS Rights & Provenance: it
observes license declarations and metadata in a project tree and emits a
standalone, content-addressed evidence record (v0.2). It does not verify
compliance, decide compatibility, or render legal conclusions.

Every finding carries explicit confidence:

- ``observed-declaration``: read directly from a declared metadata field.
- ``heuristic``: keyword match on a LICENSE/COPYING/NOTICE file; the file is
  hashed so downstream review can re-check the exact bytes.
- ``insufficient-evidence``: nothing found. Saying unknown is the point.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..config import ForgeConfig
from ..paths import resolve_contained
from ..serialization import canonical_bytes

EVIDENCE_SCHEMA_VERSION = "0.2.0"

_LICENSE_FILENAMES = (
    "LICENSE",
    "LICENSE.md",
    "LICENSE.txt",
    "COPYING",
    "COPYING.txt",
    "NOTICE",
)

_LICENSE_KEYWORDS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("apache license", "version 2.0"), "Apache-2.0"),
    (("permission is hereby granted, free of charge",), "MIT"),
    (("redistribution and use in source and binary forms", "neither the name"), "BSD-3-Clause"),
    (("redistribution and use in source and binary forms",), "BSD-2-Clause"),
    (("gnu general public license", "version 3"), "GPL-3.0-only"),
    (("gnu general public license", "version 2"), "GPL-2.0-only"),
    (("mozilla public license", "2.0"), "MPL-2.0"),
    (("creative commons zero",), "CC0-1.0"),
    (("boost software license",), "BSL-1.0"),
)

_MAX_FILE_BYTES = 512 * 1024


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def compute_content_digest(record: dict[str, Any]) -> str:
    # Forge-local content identity on the shared canonical track. The retired
    # hand-rolled encoder produced byte-identical output for this value
    # domain (ASCII keys, no non-finite floats); the shared encoder additionally
    # rejects non-finite floats instead of hashing their invalid spelling.
    reduced = {key: item for key, item in record.items() if key != "content_digest"}
    return "sha256:" + hashlib.sha256(canonical_bytes(reduced)).hexdigest()


def _classify_license_text(head_text: str) -> str | None:
    lowered = head_text.lower()
    for keywords, expression in _LICENSE_KEYWORDS:
        if all(keyword in lowered for keyword in keywords):
            return expression
    return None


def scan_license_evidence(config: ForgeConfig) -> dict[str, Any]:
    """Scan the project root for license declarations; emit an evidence record."""

    root = resolve_contained(config.root, ".", must_exist=True)
    claims: list[dict[str, Any]] = []
    observations: list[dict[str, Any]] = []

    pyproject = root / "pyproject.toml"
    if pyproject.is_file() and pyproject.stat().st_size <= _MAX_FILE_BYTES:
        text = pyproject.read_text(encoding="utf-8", errors="replace")
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("license") and "=" in stripped:
                value = stripped.split("=", 1)[1].strip().strip('"').strip("'")
                if value and not value.startswith("{"):
                    observations.append(
                        {
                            "name": "pyproject_license_declaration",
                            "value": {"raw": value[:200]},
                            "observed_at": _utc_now(),
                        }
                    )
                    claims.append(
                        {
                            "claim_type": "license-identification",
                            "statement": f"pyproject.toml declares license metadata: {value[:120]}",
                            "confidence": "observed-declaration",
                            "value": value[:200],
                        }
                    )
                break

    package_json = root / "package.json"
    if package_json.is_file() and package_json.stat().st_size <= _MAX_FILE_BYTES:
        try:
            payload = json.loads(package_json.read_text(encoding="utf-8", errors="replace"))
            license_value = payload.get("license")
            if isinstance(license_value, str) and license_value:
                observations.append(
                    {
                        "name": "package_json_license_declaration",
                        "value": {"raw": license_value[:200]},
                        "observed_at": _utc_now(),
                    }
                )
                claims.append(
                    {
                        "claim_type": "license-identification",
                        "statement": f"package.json declares license: {license_value[:120]}",
                        "confidence": "observed-declaration",
                        "value": license_value[:200],
                    }
                )
        except json.JSONDecodeError:
            claims.append(
                {
                    "claim_type": "contradictory-declaration",
                    "statement": "package.json exists but is not valid JSON.",
                    "confidence": "low",
                }
            )

    for name in _LICENSE_FILENAMES:
        candidate = root / name
        if not candidate.is_file() or candidate.stat().st_size > _MAX_FILE_BYTES:
            continue
        try:
            head = candidate.read_text(encoding="utf-8", errors="replace")[:8192]
        except OSError:
            continue
        sha256_hex = _sha256_file(candidate)
        observations.append(
            {
                "name": f"notice_file:{name}",
                "value": {"sha256": sha256_hex},
                "observed_at": _utc_now(),
            }
        )
        identified = _classify_license_text(head)
        if identified is None:
            claims.append(
                {
                    "claim_type": "unknown-license-state",
                    "statement": (
                        f"Notice file {name} present but its terms were not recognized "
                        "by bounded keyword inspection."
                    ),
                    "confidence": "insufficient-evidence",
                    "supporting_observations": [f"notice_file:{name}"],
                }
            )
        else:
            claims.append(
                {
                    "claim_type": "license-identification",
                    "statement": (
                        f"{name} matches {identified} by keyword heuristic; "
                        "hash recorded for exact re-inspection."
                    ),
                    "confidence": "heuristic",
                    "spdx_expression": identified,
                    "supporting_observations": [f"notice_file:{name}"],
                }
            )
        break

    if not claims:
        claims.append(
            {
                "claim_type": "unknown-license-state",
                "statement": (
                    "No license declaration or recognizable notice file found at the "
                    "project root. License status is unknown; do not infer one."
                ),
                "confidence": "insufficient-evidence",
            }
        )

    identity_material = {"root": str(root), "claims": claims}
    digest = hashlib.sha256(canonical_bytes(identity_material)).hexdigest()
    evidence_id = f"mncs-forge://license-evidence/{digest[:32]}"
    evidence: dict[str, Any] = {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "evidence_id": evidence_id,
        "kind": "forge-analysis",
        "producer": {
            "producer": "mncs-forge",
            "recordKind": "LicenseEvidence",
            "schemaVersion": EVIDENCE_SCHEMA_VERSION,
            "stableId": evidence_id,
        },
        "subject": {
            "artifact_refs": [{"id": f"project:{config.project_identity}", "role": "subject"}]
        },
        "observations": observations,
        "claims": claims,
        "context": {"timestamp": _utc_now()},
        "limitations": [
            "Keyword heuristics identify candidates, not verified license terms.",
            "Declarations are evidence supplied to Forge, not independent legal review.",
            "Forge does not determine copyrightability, ownership, or compatibility.",
        ],
    }
    evidence["content_digest"] = compute_content_digest(evidence)
    return evidence


__all__ = ["EVIDENCE_SCHEMA_VERSION", "compute_content_digest", "scan_license_evidence"]
