"""MNCS Rights & Provenance assessment as a bounded Forge evidence domain.

Forge consumes the versioned MNCS Rights & Provenance manifest contract.  It
validates and contextualizes evidence; it does not decide copyrightability,
ownership, non-infringement, or other legal conclusions.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from importlib.resources import files
from pathlib import Path
from typing import Any, cast

from jsonschema import Draft202012Validator

from ..config import ForgeConfig
from ..errors import ForgeError
from ..paths import resolve_contained
from ..serialization import local_json_identity, read_json
from .execution_receipts import list_bindings
from .lifecycle import LifecycleContext
from .support import aggregate_status
from .workflows import DevelopmentWorkflowService

RIGHTS_CONTRACT = "mncs-rights-provenance/manifest@0.2.0"
RIGHTS_SCHEMA_VERSION = "0.2.0"
SUPPORTED_MANIFEST_VERSIONS = ("0.2.0", "0.1.0")
RIGHTS_SPEC_REPOSITORY = "https://github.com/epi13/mncs-rights-provenance"
RIGHTS_MODES = frozenset({"observe", "advisory", "enforced"})

LEGAL_LIMITATIONS = [
    "Forge does not determine copyrightability or authorship as a matter of law.",
    "Forge does not establish ownership, non-infringement, or legal clearance.",
    "Origin classifications describe provenance and do not imply a copyright conclusion.",
    "Declared source-license status is evidence supplied to Forge, not independent legal review.",
]


def _schema(schema_version: str = RIGHTS_SCHEMA_VERSION) -> dict[str, Any]:
    if schema_version == "0.2.0":
        resource_name = "mncs-rights-manifest-0.2.schema.json"
    elif schema_version == "0.1.0":
        resource_name = "mncs-rights-manifest-0.1.schema.json"
    else:
        raise ForgeError(
            "RIGHTS_MANIFEST_UNSUPPORTED",
            f"unsupported rights/provenance manifest schema_version: {schema_version}",
        )
    path = files("mncs_forge.resources").joinpath(resource_name)
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ForgeError("INTERNAL_SCHEMA", "packaged rights/provenance schema is invalid")
    return value


def detect_manifest_schema_version(manifest: Mapping[str, object]) -> str:
    declared = manifest.get("schema_version")
    if isinstance(declared, str) and declared in SUPPORTED_MANIFEST_VERSIONS:
        return declared
    raise ForgeError(
        "RIGHTS_MANIFEST_UNSUPPORTED",
        f"unsupported rights/provenance manifest schema_version: {declared!r}",
    )


def _policy(config: ForgeConfig) -> tuple[str, Path | None]:
    configured = config.raw.get("rights_provenance", {})
    if not isinstance(configured, dict):
        raise ForgeError("CONFIG_INVALID", "rights_provenance must be a TOML table")
    mode = str(configured.get("mode", "observe"))
    if mode not in RIGHTS_MODES:
        raise ForgeError("CONFIG_INVALID", f"unsupported rights_provenance mode: {mode}")
    declared = configured.get("manifest")
    if declared is None:
        return mode, None
    return mode, resolve_contained(config.root, str(declared), must_exist=False)


def _candidate_identity(lifecycle: LifecycleContext, candidate_identity: str | None) -> str | None:
    if candidate_identity is not None:
        return candidate_identity
    projection = lifecycle.machine(
        observe_epoch_authority=False,
        observe_freeze_bindings=False,
        observe_policy=False,
    ).projection
    current = projection.current_candidate
    if current is None:
        return None
    value = current.get("candidate_id")
    return str(value) if value is not None else None


def _technical_status(
    lifecycle: LifecycleContext,
    development: DevelopmentWorkflowService,
    candidate_identity: str | None,
) -> str:
    if candidate_identity is None:
        return "UNKNOWN"
    return aggregate_status(
        (str(record["status"]) for record in development.result_records(candidate_identity)),
        native_status_aggregate=lifecycle.native_status_aggregate,
    )


def _receipt_evidence(
    lifecycle: LifecycleContext, candidate_identity: str | None
) -> list[dict[str, str]]:
    if candidate_identity is None:
        return []
    result = list_bindings(lifecycle.records, candidate_identity=candidate_identity)
    raw = result.get("execution_receipts", [])
    if not isinstance(raw, list):
        return []
    evidence: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        reference = item.get("receipt_identity") or item.get("binding_id")
        if not isinstance(reference, str) or not reference or reference in seen:
            continue
        seen.add(reference)
        evidence.append({"kind": "fabric-receipt", "reference": reference})
    return evidence


def draft_manifest(
    *,
    config: ForgeConfig,
    lifecycle: LifecycleContext,
    development: DevelopmentWorkflowService,
    candidate_identity: str | None = None,
) -> dict[str, object]:
    """Build a conservative, schema-valid draft from evidence Forge actually knows."""

    candidate = _candidate_identity(lifecycle, candidate_identity)
    participants: list[dict[str, str]] = []
    changed_paths: list[str] = []
    if candidate is not None:
        try:
            record = lifecycle.record_by_id("candidate", candidate, "candidate_id")
        except ForgeError:
            record = None
        if record is not None:
            generator = record.get("generator_identity")
            if isinstance(generator, str) and generator:
                participants.append({"type": "agent", "role": "generator", "name": generator})
            raw_paths = record.get("changed_files")
            if isinstance(raw_paths, tuple):
                changed_paths = [str(item) for item in raw_paths]

    technical = _technical_status(lifecycle, development, candidate)
    declared_technical = "not-run"
    if technical == "PASS":
        declared_technical = "passed"
    elif technical == "FAIL":
        declared_technical = "failed"

    return {
        "schema_version": RIGHTS_SCHEMA_VERSION,
        "spec_profile": "development",
        "artifact": {
            "id": candidate or f"project:{config.project_identity}",
            "class": "source-code",
            "paths": changed_paths,
        },
        "provenance": {
            "origin_classification": "origin-uncertain",
            "participants": participants,
            "process_evidence": _receipt_evidence(lifecycle, candidate),
            "notes": (
                "Forge-derived draft: unknown fields remain explicit until supplied evidence "
                "resolves them."
            ),
        },
        "rights": {
            "distribution_license": "Apache-2.0",
            "copyright_status": "unresolved",
            "rights_basis": "unknown-needs-review",
            "third_party_material": "unknown",
            "sources": [],
            "notes": "No legal conclusion is inferred from candidate or execution provenance.",
        },
        "review": {
            "technical_validation": declared_technical,
            "provenance_validation": "not-run",
            "human_acceptance": "not-reviewed",
        },
    }


def _load_configured_manifest(config: ForgeConfig, path: Path) -> dict[str, object]:
    if not path.is_file():
        raise ForgeError(
            "RIGHTS_MANIFEST_MISSING",
            f"configured rights/provenance manifest does not exist: {path}",
        )
    try:
        value = read_json(path, byte_cap=config.output_cap)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise ForgeError("RIGHTS_MANIFEST_INVALID", f"cannot read rights manifest: {exc}") from exc
    if not isinstance(value, dict):
        raise ForgeError("RIGHTS_MANIFEST_INVALID", "rights manifest must be a JSON object")
    return {str(key): cast(object, item) for key, item in value.items()}


def _validation_errors(manifest: Mapping[str, object]) -> list[str]:
    try:
        schema_version = detect_manifest_schema_version(manifest)
    except ForgeError as exc:
        return [f"schema_version: {exc.message}"]
    errors = sorted(
        Draft202012Validator(_schema(schema_version)).iter_errors(dict(manifest)),
        key=lambda item: [str(part) for part in item.absolute_path],
    )
    rendered: list[str] = []
    for issue in errors[:20]:
        location = ".".join(str(part) for part in issue.absolute_path) or "<root>"
        rendered.append(f"{location}: {issue.message}")
    return rendered


def _review_status(value: object, *, passed: set[str], failed: set[str]) -> str:
    if value in passed:
        return "PASS"
    if value in failed:
        return "FAIL"
    return "UNKNOWN"


def _rights_basis_status(
    rights: Mapping[str, object], lifecycle: LifecycleContext
) -> tuple[str, list[str]]:
    blockers: list[str] = []
    statuses = ["PASS"]
    if rights.get("rights_basis") == "unknown-needs-review":
        statuses.append("UNKNOWN")
        blockers.append("rights basis is explicitly unknown and needs review")

    third_party = rights.get("third_party_material")
    if third_party in {"possible", "unknown"}:
        statuses.append("UNKNOWN")
        blockers.append(f"third-party material status is {third_party}")

    sources = rights.get("sources")
    if isinstance(sources, list):
        for source in sources:
            if not isinstance(source, Mapping):
                continue
            license_status = source.get("license_status")
            reference = source.get("reference", "source")
            if license_status == "incompatible":
                statuses.append("FAIL")
                blockers.append(f"declared incompatible source license: {reference}")
            elif license_status == "unknown":
                statuses.append("UNKNOWN")
                blockers.append(f"source license status is unknown: {reference}")

    return (
        aggregate_status(statuses, native_status_aggregate=lifecycle.native_status_aggregate),
        blockers,
    )


def _policy_projection(mode: str, evidence_status: str, blockers: list[str]) -> dict[str, object]:
    if mode == "observe":
        return {
            "blocking": False,
            "disposition": "OBSERVED",
            "review_required": evidence_status != "PASS",
        }
    if mode == "advisory":
        return {
            "blocking": False,
            "disposition": "REVIEW_REQUIRED" if evidence_status != "PASS" else "NO_ADVISORY",
            "review_required": evidence_status != "PASS",
        }
    blocked = evidence_status != "PASS"
    return {
        "blocking": blocked,
        "disposition": (
            "BLOCKED_BY_RIGHTS_PROVENANCE_POLICY"
            if blocked
            else "RIGHTS_PROVENANCE_EVIDENCE_COMPLETE"
        ),
        "review_required": blocked,
        "blocker_count": len(blockers),
    }


def assess_manifest(
    manifest: Mapping[str, object],
    *,
    config: ForgeConfig,
    lifecycle: LifecycleContext,
    development: DevelopmentWorkflowService,
    candidate_identity: str | None = None,
    policy_mode: str | None = None,
    source: str = "supplied-manifest",
) -> dict[str, object]:
    """Assess evidence completeness without promoting the result into legal authority."""

    configured_mode, _ = _policy(config)
    mode = policy_mode or configured_mode
    if mode not in RIGHTS_MODES:
        raise ForgeError("RIGHTS_POLICY_MODE", f"unsupported rights/provenance mode: {mode}")

    candidate = _candidate_identity(lifecycle, candidate_identity)
    errors = _validation_errors(manifest)
    contract_status = "PASS" if not errors else "FAIL"
    blockers = list(errors)
    provenance_status = "UNKNOWN"
    rights_status = "UNKNOWN"
    human_review_status = "UNKNOWN"

    if not errors:
        artifact = manifest.get("artifact")
        if candidate is not None and isinstance(artifact, Mapping):
            artifact_id = artifact.get("id")
            if artifact_id != candidate:
                contract_status = "FAIL"
                blockers.append(
                    f"manifest artifact id {artifact_id!r} does not match candidate {candidate!r}"
                )

        review = manifest.get("review")
        if isinstance(review, Mapping):
            provenance_status = _review_status(
                review.get("provenance_validation"),
                passed={"passed"},
                failed={"failed"},
            )
            human_review_status = _review_status(
                review.get("human_acceptance"),
                passed={"accepted", "not-required"},
                failed={"rejected"},
            )
            if provenance_status == "UNKNOWN":
                blockers.append("provenance validation is incomplete or has not run")
            elif provenance_status == "FAIL":
                blockers.append("provenance validation failed")
            if human_review_status == "UNKNOWN":
                blockers.append("human acceptance/review is unresolved")
            elif human_review_status == "FAIL":
                blockers.append("human review rejected the artifact")

        rights_value = manifest.get("rights")
        if isinstance(rights_value, Mapping):
            rights_status, rights_blockers = _rights_basis_status(rights_value, lifecycle)
            blockers.extend(rights_blockers)

    evidence_status = aggregate_status(
        [contract_status, provenance_status, rights_status, human_review_status],
        native_status_aggregate=lifecycle.native_status_aggregate,
    )
    policy = _policy_projection(mode, evidence_status, blockers)
    technical = _technical_status(lifecycle, development, candidate)

    rights_value = manifest.get("rights")
    rights: Mapping[str, object] = (
        cast(Mapping[str, object], rights_value) if isinstance(rights_value, Mapping) else {}
    )
    provenance_value = manifest.get("provenance")
    provenance: Mapping[str, object] = (
        cast(Mapping[str, object], provenance_value)
        if isinstance(provenance_value, Mapping)
        else {}
    )
    return {
        "contract": RIGHTS_CONTRACT,
        "contract_repository": RIGHTS_SPEC_REPOSITORY,
        "contract_schema_version": RIGHTS_SCHEMA_VERSION,
        "candidate_identity": candidate,
        "manifest_source": source,
        "manifest_identity": local_json_identity(dict(manifest)),
        "policy_mode": mode,
        "evidence_status": evidence_status,
        "contract_validation": contract_status,
        "provenance_validation": provenance_status,
        "rights_basis_status": rights_status,
        "human_review_status": human_review_status,
        "technical_status": technical,
        "origin_classification": provenance.get("origin_classification"),
        "copyright_status": rights.get("copyright_status"),
        "rights_basis": rights.get("rights_basis"),
        "distribution_license": rights.get("distribution_license"),
        "third_party_material": rights.get("third_party_material"),
        "blockers": sorted(set(blockers)),
        "policy": policy,
        "legal_conclusion": "NOT_MADE",
        "limitations": list(LEGAL_LIMITATIONS),
        "dominance": "FAIL > UNKNOWN > PASS",
        "note": (
            "Technical status and rights/provenance evidence status are independent domains; "
            "neither silently promotes the other."
        ),
    }


def rights_provenance_status(
    *,
    config: ForgeConfig,
    lifecycle: LifecycleContext,
    development: DevelopmentWorkflowService,
    candidate_identity: str | None = None,
) -> dict[str, object]:
    mode, manifest_path = _policy(config)
    candidate = _candidate_identity(lifecycle, candidate_identity)
    if manifest_path is None:
        manifest = draft_manifest(
            config=config,
            lifecycle=lifecycle,
            development=development,
            candidate_identity=candidate,
        )
        result = assess_manifest(
            manifest,
            config=config,
            lifecycle=lifecycle,
            development=development,
            candidate_identity=candidate,
            policy_mode=mode,
            source="forge-derived-draft",
        )
        result["configured_manifest"] = None
        result["draft_manifest"] = manifest
        return result

    try:
        manifest = _load_configured_manifest(config, manifest_path)
    except ForgeError as exc:
        draft = draft_manifest(
            config=config,
            lifecycle=lifecycle,
            development=development,
            candidate_identity=candidate,
        )
        result = assess_manifest(
            draft,
            config=config,
            lifecycle=lifecycle,
            development=development,
            candidate_identity=candidate,
            policy_mode=mode,
            source="configured-manifest-unavailable",
        )
        result["configured_manifest"] = str(manifest_path.relative_to(config.root))
        result["manifest_error"] = exc.as_dict()
        result["draft_manifest"] = draft
        return result

    result = assess_manifest(
        manifest,
        config=config,
        lifecycle=lifecycle,
        development=development,
        candidate_identity=candidate,
        policy_mode=mode,
        source="configured-manifest",
    )
    result["configured_manifest"] = str(manifest_path.relative_to(config.root))
    return result


def enforce_rights_provenance_selection(
    *,
    config: ForgeConfig,
    lifecycle: LifecycleContext,
    development: DevelopmentWorkflowService,
    candidate_identity: str,
) -> dict[str, object]:
    """Apply only an explicitly configured enforced policy to candidate selection."""

    result = rights_provenance_status(
        config=config,
        lifecycle=lifecycle,
        development=development,
        candidate_identity=candidate_identity,
    )
    policy = result.get("policy")
    blocking = isinstance(policy, Mapping) and policy.get("blocking") is True
    if blocking:
        blockers = result.get("blockers")
        detail = "; ".join(str(item) for item in blockers) if isinstance(blockers, list) else ""
        raise ForgeError(
            "RIGHTS_PROVENANCE_BLOCKED",
            "candidate selection blocked by explicit rights/provenance policy"
            + (f": {detail}" if detail else ""),
        )
    return result
