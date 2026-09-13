"""Identity-bearing Forge evaluations for Concept Reconstruction Experiments."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping
from typing import Any

from .errors import ForgeError
from .serialization import canonical_bytes

CONCEPT_EVALUATION_SCHEMA = "mncs-forge.concept-evaluation.v0.1"
CONCEPT_EVALUATION_INTERPRETATION = "bounded_forge_evaluation_not_mncs_conformance"


def _text(value: object, field: str, maximum: int = 4096) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum or "\x00" in value:
        raise ForgeError("CONCEPT_EVALUATION_INVALID", f"{field} must be bounded text")
    return value.strip()


def _digest(value: Mapping[str, Any]) -> str:
    # Forge-local content identity on the shared canonical track. Unlike the
    # previous bespoke encoder this rejects non-finite floats instead of
    # hashing their invalid JSON spelling.
    return "sha256:" + hashlib.sha256(canonical_bytes(value)).hexdigest()


def build_concept_evaluation(
    *,
    concept_experiment_id: str,
    candidate_identity: str,
    language_profile: str,
    compiler_identity: str,
    backend_identity: str,
    execution_identities: Iterable[str],
    verifier_identity: str,
    verifier_version: str,
    obligation: str,
    evidence_identities: Iterable[str],
    status: str,
    unresolved_obligations: Iterable[str] = (),
    generator_identity: str | None = None,
    evaluator_policy_identity: str | None = None,
) -> dict[str, Any]:
    """Build a bounded Forge-native result without candidate self-certification."""

    if status not in {"PASS", "FAIL", "UNKNOWN"}:
        raise ForgeError("CONCEPT_EVALUATION_STATUS", "status must preserve PASS, FAIL, or UNKNOWN")
    verifier_identity = _text(verifier_identity, "verifier_identity")
    if generator_identity is not None and verifier_identity == generator_identity:
        raise ForgeError(
            "CONCEPT_EVALUATION_SELF_CERTIFICATION",
            "candidate generator and evaluator identities must be distinct",
        )
    material: dict[str, Any] = {
        "schema_version": CONCEPT_EVALUATION_SCHEMA,
        "producer": "mncs-forge",
        "concept_experiment_id": _text(concept_experiment_id, "concept_experiment_id", 256),
        "candidate_identity": _text(candidate_identity, "candidate_identity"),
        "language_profile": _text(language_profile, "language_profile"),
        "compiler_identity": _text(compiler_identity, "compiler_identity"),
        "backend_identity": _text(backend_identity, "backend_identity"),
        "execution_identities": sorted(
            {_text(item, "execution_identities[]") for item in execution_identities}
        ),
        "verifier_identity": verifier_identity,
        "verifier_version": _text(verifier_version, "verifier_version", 256),
        "obligation": _text(obligation, "obligation"),
        "evidence_identities": sorted(
            {_text(item, "evidence_identities[]") for item in evidence_identities}
        ),
        "status": status,
        "unresolved_obligations": sorted(
            {_text(item, "unresolved_obligations[]") for item in unresolved_obligations}
        ),
        "generator_identity": generator_identity,
        "evaluator_policy_identity": evaluator_policy_identity,
        "generator_certified": False,
        "interpretation": "bounded_forge_evaluation_not_mncs_conformance",
        "claim_boundary": (
            "Forge evaluator scope only; candidate generation, Language semantics, Fabric "
            "execution, scientific interpretation, and MNCS conformance remain separate"
        ),
    }
    content_digest = _digest(material)
    return {
        **material,
        "stable_id": f"mncs-forge://evaluation/{content_digest[7:]}",
        "content_digest": content_digest,
    }


def verify_concept_evaluation(evaluation: Mapping[str, Any]) -> dict[str, Any]:
    """Re-derive a built evaluation's identity; reject any tampering.

    Returns the normalized material (the evaluation without its derived
    ``stable_id``/``content_digest`` fields).  The digest and stable id are
    recomputed from that material so persisted copies cannot drift.
    """

    if not isinstance(evaluation, Mapping):
        raise ForgeError("CONCEPT_EVALUATION_INVALID", "evaluation must be an object")
    if evaluation.get("schema_version") != CONCEPT_EVALUATION_SCHEMA:
        raise ForgeError("CONCEPT_EVALUATION_INVALID", "unsupported evaluation schema version")
    unknown = sorted(
        set(evaluation)
        - set(
            build_concept_evaluation(
                concept_experiment_id="x",
                candidate_identity="x",
                language_profile="x",
                compiler_identity="x",
                backend_identity="x",
                execution_identities=[],
                verifier_identity="x",
                verifier_version="x",
                obligation="x",
                evidence_identities=[],
                status="UNKNOWN",
            )
        )
    )
    if unknown:
        raise ForgeError(
            "CONCEPT_EVALUATION_INVALID",
            f"evaluation has unknown fields: {', '.join(unknown)}",
        )
    if "stable_id" not in evaluation or "content_digest" not in evaluation:
        raise ForgeError(
            "CONCEPT_EVALUATION_INVALID",
            "evaluation must carry derived stable_id and content_digest fields",
        )
    derived_fields = {"stable_id", "content_digest"}
    material = {key: value for key, value in evaluation.items() if key not in derived_fields}
    expected_digest = _digest(material)
    if evaluation["content_digest"] != expected_digest:
        raise ForgeError(
            "RECORD_IDENTITY",
            "concept evaluation content digest does not reproduce from its material",
        )
    expected_stable = f"mncs-forge://evaluation/{expected_digest[7:]}"
    if evaluation["stable_id"] != expected_stable:
        raise ForgeError(
            "RECORD_IDENTITY",
            "concept evaluation stable id does not reproduce from its material",
        )
    if material.get("generator_certified") is not False:
        raise ForgeError(
            "CONCEPT_EVALUATION_SELF_CERTIFICATION",
            "concept evaluations must record generator_certified as false",
        )
    if material.get("interpretation") != CONCEPT_EVALUATION_INTERPRETATION:
        raise ForgeError(
            "CONCEPT_EVALUATION_INVALID",
            "concept evaluations must keep the bounded Forge interpretation",
        )
    return material
