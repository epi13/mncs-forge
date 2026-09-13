import pytest

from mncs_forge.concept_experiments import build_concept_evaluation
from mncs_forge.errors import ForgeError


def evaluation(status: str = "UNKNOWN") -> dict[str, object]:
    return build_concept_evaluation(
        concept_experiment_id="cre-tristate-a",
        candidate_identity="candidate:tri-state:fixture",
        language_profile="mncs-language:source-profile:0.2",
        compiler_identity="mncs:compiler:fixture",
        backend_identity="mncs:language:backend:reference-interpreter",
        execution_identities=["mncs-fabric://execution/fixture/attempt/1"],
        verifier_identity="forge:verifier:tri-state",
        verifier_version="0.1",
        obligation="tri-state UNKNOWN preservation",
        evidence_identities=["sha256:" + "a" * 64],
        status=status,
        unresolved_obligations=["independent-backend-reproduction"],
        generator_identity="harness:model:builder",
        evaluator_policy_identity="forge:policy:tri-state",
    )


def test_unknown_is_retained_at_exact_forge_scope() -> None:
    value = evaluation()
    assert value["status"] == "UNKNOWN"
    assert value["producer"] == "mncs-forge"
    assert value["generator_certified"] is False
    assert value["stable_id"].startswith("mncs-forge://evaluation/")
    assert value["interpretation"] == "bounded_forge_evaluation_not_mncs_conformance"


def test_generator_cannot_be_the_named_evaluator() -> None:
    with pytest.raises(ForgeError, match="generator and evaluator"):
        build_concept_evaluation(
            concept_experiment_id="cre-a",
            candidate_identity="candidate:a",
            language_profile="language:a",
            compiler_identity="compiler:a",
            backend_identity="backend:a",
            execution_identities=[],
            verifier_identity="same:identity",
            verifier_version="0.1",
            obligation="obligation:a",
            evidence_identities=[],
            status="UNKNOWN",
            generator_identity="same:identity",
        )


def _replication_evaluation(baseline_digest_suffix: str = "a" * 64) -> dict[str, object]:
    return build_concept_evaluation(
        concept_experiment_id="cre-tristate-a",
        candidate_identity="mncs:language:experiment:result:" + baseline_digest_suffix,
        language_profile="mncs-language:source-profile:0.2",
        compiler_identity="mncs:compiler:fixture",
        backend_identity="mncs:language:backend:reference-interpreter",
        execution_identities=[
            "mncs-fabric://execution/worker/attempt/1",
            "mncs-fabric://execution/worker/attempt/2",
        ],
        verifier_identity="forge:verifier:tri-state",
        verifier_version="0.1",
        obligation="bounded replication agreement for one frozen realization",
        evidence_identities=["sha256:" + "b" * 64],
        status="UNKNOWN",
        unresolved_obligations=["independent-backend-reproduction"],
        generator_identity="mncs-language",
        evaluator_policy_identity="forge:policy:tri-state",
    )


def test_persists_lists_and_fetches_replication_evaluations(config) -> None:
    from mncs_forge.engine import Forge

    forge = Forge(config)
    record = forge.concept_evaluation_record(_replication_evaluation())
    assert record["status"] == "UNKNOWN"
    assert record["generator_certified"] is False
    assert record["interpretation"] == "bounded_forge_evaluation_not_mncs_conformance"
    assert record["assurance_status"] is None
    assert record["conformance_status"] is None
    assert str(record["evaluation_id"]).startswith("concept-evaluation:")

    # Idempotent re-record of the identical evaluation.
    duplicate = forge.concept_evaluation_record(_replication_evaluation())
    assert duplicate["evaluation_id"] == record["evaluation_id"]

    listing = forge.concept_evaluations_list()
    assert len(listing["evaluations"]) == 1
    assert listing["assurance_status"] is None

    fetched = forge.concept_evaluation_get(str(record["evaluation_id"]))
    assert fetched["content_digest"] == record["content_digest"]
    by_digest = forge.concept_evaluation_get(str(record["content_digest"]))
    assert by_digest["stable_id"] == record["stable_id"]
    by_stable = forge.concept_evaluation_get(str(record["stable_id"]))
    assert by_stable["evaluation_id"] == record["evaluation_id"]
    assert forge.ledger.verify()["ok"] is True


def test_tampered_evaluations_are_rejected_at_record_time(config) -> None:
    from mncs_forge.engine import Forge

    forge = Forge(config)
    evaluation = _replication_evaluation()
    mutated = dict(evaluation)
    mutated["status"] = "PASS"  # strengthen UNKNOWN into PASS
    with pytest.raises(ForgeError, match="does not reproduce"):
        forge.concept_evaluation_record(mutated)

    relabeled = dict(evaluation)
    relabeled["stable_id"] = "mncs-forge://evaluation/" + "f" * 64
    with pytest.raises(ForgeError, match="does not reproduce"):
        forge.concept_evaluation_record(relabeled)


def _material(value: dict[str, object]) -> dict[str, object]:
    return {key: item for key, item in value.items() if key not in {"stable_id", "content_digest"}}


def test_content_digest_uses_shared_canonical_bytes() -> None:
    import hashlib

    from mncs_forge.serialization import canonical_bytes

    value = evaluation()
    expected = "sha256:" + hashlib.sha256(canonical_bytes(_material(value))).hexdigest()
    assert value["content_digest"] == expected

    rebuilt = build_concept_evaluation(
        concept_experiment_id="cre-unicode",
        candidate_identity="candidate:unicode",
        language_profile="mncs-language:source-profile:0.2",
        compiler_identity="mncs:compiler:fixture",
        backend_identity="mncs:language:backend:reference-interpreter",
        execution_identities=[],
        verifier_identity="forge:verifier:unicode",
        verifier_version="0.1",
        obligation="données — Öffnen ©",
        evidence_identities=[],
        status="UNKNOWN",
    )
    material = _material(rebuilt)
    assert (
        rebuilt["content_digest"]
        == "sha256:" + hashlib.sha256(canonical_bytes(material)).hexdigest()
    )
    # Non-ASCII text stays raw UTF-8 on the Forge-local track (no JCS escapes).
    assert "données — Öffnen ©".encode() in canonical_bytes(material)

    with pytest.raises(ValueError, match=r"[Nn]an"):
        canonical_bytes({"value": float("nan")})


def test_unknown_cannot_be_upgraded_through_persistence(config) -> None:
    import hashlib

    from mncs_forge.engine import Forge
    from mncs_forge.serialization import canonical_bytes

    forge = Forge(config)
    evaluation = _replication_evaluation()
    original = forge.concept_evaluation_record(evaluation)
    assert original["status"] == "UNKNOWN"

    # A tampered copy that relabels UNKNOWN into PASS must not overwrite the
    # persisted record; its digest differs, so it can only ever be a new record.
    material = {
        key: value
        for key, value in evaluation.items()
        if key not in {"stable_id", "content_digest"}
    }
    material["status"] = "PASS"
    forged = {
        **material,
        "content_digest": "sha256:" + hashlib.sha256(canonical_bytes(material)).hexdigest(),
    }
    forged["stable_id"] = f"mncs-forge://evaluation/{forged['content_digest'][7:]}"
    record = forge.concept_evaluation_record(forged)
    assert record["status"] == "PASS"
    assert record["evaluation_id"] != original["evaluation_id"]
    listing = forge.concept_evaluations_list()
    assert len(listing["evaluations"]) == 2
    still_original = forge.concept_evaluation_get(str(original["evaluation_id"]))
    assert still_original["status"] == "UNKNOWN"


def test_missing_evaluations_report_not_found(config) -> None:
    from mncs_forge.engine import Forge

    forge = Forge(config)
    with pytest.raises(ForgeError, match="unknown concept evaluation"):
        forge.concept_evaluation_get("concept-evaluation:" + "0" * 64)
