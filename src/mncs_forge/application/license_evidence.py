"""License-evidence discovery as a Forge analysis domain.

Forge contributes **analysis evidence** to MNCS Rights & Provenance: it
observes license declarations and metadata in a project tree and emits a
standalone, source-bound evidence record (v0.3). It does not verify
compliance, decide compatibility, or render legal conclusions.

Every finding carries explicit confidence:

- ``observed-declaration``: read directly from a declared metadata field.
- ``heuristic``: keyword match on a LICENSE/COPYING/NOTICE file; the file is
  hashed so downstream review can re-check the exact bytes.
- ``insufficient-evidence``: nothing found or a source could not be safely
  inspected. Saying unknown is the point.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tomllib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..config import ForgeConfig
from ..errors import ForgeError
from ..paths import resolve_contained
from ..serialization import canonical_bytes

EVIDENCE_SCHEMA_VERSION = "0.3.0"

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


def compute_content_digest(record: dict[str, Any]) -> str:
    # The shared canonical encoder rejects non-finite floats instead of
    # silently hashing their invalid JSON spelling.
    reduced = {key: item for key, item in record.items() if key != "content_digest"}
    return "sha256:" + hashlib.sha256(canonical_bytes(reduced)).hexdigest()


def _read_source(
    root: Path, name: str,
) -> tuple[bytes | None, dict[str, Any] | None, str | None]:
    """Read one expected root-level source without following an escape.

    The returned reference contains only project-relative paths and the hash
    of the exact bytes read. Metadata and hash therefore describe the same
    file snapshot, even if a source is replaced while the scan is running.
    """

    root_real = root.resolve(strict=True)
    candidate = root_real / name
    if not candidate.exists() and not candidate.is_symlink():
        return None, None, None
    try:
        resolved = resolve_contained(root_real, name, must_exist=True)
    except ForgeError as error:
        if error.code == "SYMLINK_ESCAPE":
            return None, None, "resolves outside the project root"
        return None, None, "could not be resolved"

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(resolved, flags)
        with os.fdopen(descriptor, "rb") as handle:
            facts = os.fstat(handle.fileno())
            if not stat.S_ISREG(facts.st_mode):
                return None, None, "is not a regular file"
            if facts.st_size > _MAX_FILE_BYTES:
                return None, None, "exceeds the bounded scan size"
            content = handle.read(_MAX_FILE_BYTES + 1)
    except OSError:
        return None, None, "could not be read safely"

    if len(content) > _MAX_FILE_BYTES:
        return None, None, "exceeds the bounded scan size"

    try:
        resolved_relative = resolved.relative_to(root_real).as_posix()
    except ValueError:
        return None, None, "resolves outside the project root"
    reference = {
        "path": name,
        "resolved_path": resolved_relative,
        "sha256": hashlib.sha256(content).hexdigest(),
        "byte_length": len(content),
    }
    return content, reference, None


def _metadata_summary(value: Any) -> dict[str, Any]:
    """Keep a bounded display value while the source hash binds the full value."""

    if isinstance(value, str):
        return {"raw": value[:200], "truncated": len(value) > 200}
    serialized = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    kind = (
        "null" if value is None
        else "boolean" if isinstance(value, bool)
        else "number" if isinstance(value, (int, float))
        else "object" if isinstance(value, dict)
        else "array" if isinstance(value, list)
        else "other"
    )
    return {
        "raw": serialized[:200],
        "value_type": kind,
        "truncated": len(serialized) > 200,
    }


def _add_observation(
    observations: list[dict[str, Any]],
    *,
    name: str,
    value: dict[str, Any],
    source_ref: dict[str, Any],
    selector: str,
    observed_at: str,
) -> None:
    observations.append(
        {
            "name": name,
            "value": value,
            "source_ref": {**source_ref, "selector": selector},
            "observed_at": observed_at,
        }
    )


def _add_metadata_declaration(
    *,
    source_name: str,
    selector: str,
    value: Any,
    source_ref: dict[str, Any],
    observed_at: str,
    observations: list[dict[str, Any]],
    claims: list[dict[str, Any]],
) -> None:
    observation_name = f"{source_name}_license_declaration"
    summary = _metadata_summary(value)
    _add_observation(
        observations,
        name=observation_name,
        value=summary,
        source_ref=source_ref,
        selector=selector,
        observed_at=observed_at,
    )
    claims.append(
        {
            "claim_type": "license-declaration",
            "statement": (
                f"{source_name} declares license metadata ({summary['raw'][:120]}). "
                "Forge records the declaration without validating its authority or legal effect."
            ),
            "confidence": "observed-declaration",
            "value": summary["raw"],
            "supporting_observations": [observation_name],
        }
    )


def _add_parse_error(
    *,
    source_name: str,
    source_ref: dict[str, Any],
    selector: str,
    parse_label: str,
    observed_at: str,
    observations: list[dict[str, Any]],
    claims: list[dict[str, Any]],
) -> None:
    observation_name = f"{source_name}_parse_status"
    _add_observation(
        observations,
        name=observation_name,
        value={"status": "invalid", "format": parse_label},
        source_ref=source_ref,
        selector=selector,
        observed_at=observed_at,
    )
    claims.append(
        {
            "claim_type": "declaration-parse-error",
            "statement": (
                f"{source_name} could not be parsed as {parse_label}; "
                "Forge does not infer a license value from it."
            ),
            "confidence": "insufficient-evidence",
            "supporting_observations": [observation_name],
        }
    )


def _classify_license_text(head_text: str) -> str | None:
    lowered = head_text.lower()
    for keywords, expression in _LICENSE_KEYWORDS:
        if all(keyword in lowered for keyword in keywords):
            return expression
    return None


def scan_license_evidence(config: ForgeConfig) -> dict[str, Any]:
    """Scan bounded root declarations and emit source-bound evidence."""

    root = resolve_contained(config.root, ".", must_exist=True)
    claims: list[dict[str, Any]] = []
    observations: list[dict[str, Any]] = []
    scan_inputs: list[dict[str, Any]] = []
    observed_at = _utc_now()

    for name, parser, selector in (
        ("pyproject.toml", "toml", "/project/license"),
        ("package.json", "json", "/license"),
    ):
        source, source_ref, problem = _read_source(root, name)
        if problem is not None:
            scan_inputs.append({"path": name, "status": "unavailable", "reason": problem})
            claims.append(
                {
                    "claim_type": "source-unavailable",
                    "statement": (
                        f"{name} was not inspected because it {problem}; "
                        "license evidence for that source remains unknown."
                    ),
                    "confidence": "insufficient-evidence",
                    "source_path": name,
                }
            )
            continue
        if source is None or source_ref is None:
            scan_inputs.append({"path": name, "status": "absent"})
            continue
        scan_inputs.append(
            {
                "path": name,
                "status": "observed",
                "resolved_path": source_ref["resolved_path"],
                "sha256": source_ref["sha256"],
                "byte_length": source_ref["byte_length"],
            }
        )
        try:
            decoded = source.decode("utf-8")
            if parser == "toml":
                payload = tomllib.loads(decoded)
                project = payload.get("project")
                license_value = project.get("license") if isinstance(project, dict) else None
            else:
                payload = json.loads(decoded)
                license_value = payload.get("license") if isinstance(payload, dict) else None
        except (UnicodeDecodeError, tomllib.TOMLDecodeError, json.JSONDecodeError):
            _add_parse_error(
                source_name=name,
                source_ref=source_ref,
                selector=selector,
                parse_label=parser.upper(),
                observed_at=observed_at,
                observations=observations,
                claims=claims,
            )
            continue
        if license_value is not None:
            _add_metadata_declaration(
                source_name=name,
                selector=selector,
                value=license_value,
                source_ref=source_ref,
                observed_at=observed_at,
                observations=observations,
                claims=claims,
            )

    for name in _LICENSE_FILENAMES:
        source, source_ref, problem = _read_source(root, name)
        if problem is not None:
            scan_inputs.append({"path": name, "status": "unavailable", "reason": problem})
            claims.append(
                {
                    "claim_type": "source-unavailable",
                    "statement": (
                        f"{name} was not inspected because it {problem}; "
                        "license evidence for that source remains unknown."
                    ),
                    "confidence": "insufficient-evidence",
                    "source_path": name,
                }
            )
            continue
        if source is None or source_ref is None:
            scan_inputs.append({"path": name, "status": "absent"})
            continue
        scan_inputs.append(
            {
                "path": name,
                "status": "observed",
                "resolved_path": source_ref["resolved_path"],
                "sha256": source_ref["sha256"],
                "byte_length": source_ref["byte_length"],
            }
        )

        observation_name = f"notice_file:{name}"
        source_hash = source_ref["sha256"]
        _add_observation(
            observations,
            name=observation_name,
            value={"sha256": source_hash},
            source_ref=source_ref,
            selector="/content",
            observed_at=observed_at,
        )
        identified = _classify_license_text(source[:8192].decode("utf-8", errors="replace"))
        if identified is None:
            claims.append(
                {
                    "claim_type": "unknown-license-state",
                    "statement": (
                        f"Notice file {name} is present but its terms were not recognized "
                        "by bounded keyword inspection."
                    ),
                    "confidence": "insufficient-evidence",
                    "supporting_observations": [observation_name],
                }
            )
        else:
            claims.append(
                {
                    "claim_type": "license-identification",
                    "statement": (
                        f"{name} matches {identified} by keyword heuristic; "
                        "the exact source hash is recorded for re-inspection."
                    ),
                    "confidence": "heuristic",
                    "spdx_expression": identified,
                    "supporting_observations": [observation_name],
                }
            )

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

    subject = {
        "artifact_refs": [
            {"id": f"project:{config.project_identity}", "role": "subject"}
        ]
    }
    # This stable identity excludes scan time and derived wording. It binds
    # the subject to the exact bytes and availability state of every bounded
    # input, including files with no license declaration.
    identity_material = {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "subject": subject,
        "scan_inputs": scan_inputs,
    }
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
        "subject": subject,
        "scan_inputs": scan_inputs,
        "observations": observations,
        "claims": claims,
        "context": {"timestamp": observed_at},
        "limitations": [
            "Keyword heuristics identify candidates, not verified license terms.",
            "Metadata declarations are evidence supplied to Forge, not independent legal review.",
            (
                "Forge does not validate SPDX expression syntax or determine "
                "copyrightability, ownership, or compatibility."
            ),
        ],
    }
    evidence["content_digest"] = compute_content_digest(evidence)
    return evidence


__all__ = ["EVIDENCE_SCHEMA_VERSION", "compute_content_digest", "scan_license_evidence"]
