from __future__ import annotations

import copy
from collections.abc import Iterable
from pathlib import Path

import pytest

from mncs_forge import mncs_native
from mncs_forge.errors import ForgeError
from mncs_forge.mncs_native import (
    NativeBundlePreconditionProjection,
    NativeForgeAdapter,
    NativeInvocation,
    NativeLifecycleProjection,
    NativeLifecycleResult,
    canonical_candidate_digest,
    canonical_candidate_material,
)
from mncs_forge.ports import ExecutionResult
from mncs_forge.serialization import read_json

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.native


def _sequence_value(values: Iterable[int]) -> dict[str, object]:
    return {"sequence": {"values": [{"byte": {"value": value}} for value in values]}}


def test_canonical_candidate_material_matches_declared_chunk_contract() -> None:
    parent = bytes(range(32))
    source = bytes(reversed(range(32)))
    changed_files = b"\x01\x02\x03\x04"

    material = canonical_candidate_material(parent, source, "UNKNOWN", changed_files)

    assert len(material) == 71
    assert material[:3] == bytes((67, 1, 3))
    assert material[3:35] == parent
    assert material[35:67] == source
    assert material[67:] == changed_files
    assert canonical_candidate_digest(parent, source, "UNKNOWN", changed_files)


@pytest.mark.parametrize(
    ("parent", "source", "status", "changed_files"),
    [
        (b"", bytes(32), "PASS", bytes(4)),
        (bytes(32), b"", "PASS", bytes(4)),
        (bytes(32), bytes(32), "MAYBE", bytes(4)),
        (bytes(32), bytes(32), "PASS", b""),
    ],
)
def test_canonical_candidate_material_rejects_malformed_boundary_values(
    parent: bytes, source: bytes, status: str, changed_files: bytes
) -> None:
    with pytest.raises(ValueError):
        canonical_candidate_material(parent, source, status, changed_files)


def test_json_reader_rejects_duplicate_members_and_invalid_utf8(tmp_path: Path) -> None:
    duplicate = tmp_path / "duplicate.json"
    duplicate.write_bytes(b'{"status":"PASS","status":"FAIL"}')
    with pytest.raises(ValueError, match="duplicate JSON object key"):
        read_json(duplicate)

    invalid_utf8 = tmp_path / "invalid-utf8.json"
    invalid_utf8.write_bytes(b'{"status":"\xff"}')
    with pytest.raises(UnicodeDecodeError):
        read_json(invalid_utf8)


def test_native_response_rejects_duplicate_members(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = NativeForgeAdapter(ROOT)
    adapter.ensure_available()

    monkeypatch.setattr(
        mncs_native,
        "run_bounded",
        lambda *args, **kwargs: ExecutionResult(
            argv=["fixture"],
            returncode=0,
            stdout=b'{"status":"returned","status":"FAIL"}',
            stderr=b"",
            duration_seconds=0.0,
        ),
    )

    result = adapter.invoke(["fixture"])

    assert result.payload is None
    assert not result.ok


def test_native_body_execution_returns_language_owned_status() -> None:
    adapter = NativeForgeAdapter(ROOT)
    adapter.ensure_available()

    result = adapter.execute(
        adapter.native_source,
        ROOT / "examples/execution/native-status-probe.json",
    )

    assert result.ok
    assert result.payload is not None
    assert result.payload["status"] == "returned"
    assert result.payload["returned"][0]["finite"]["variant_identity"].endswith("::UNKNOWN")


def test_language_owned_abi_metadata_is_available_to_external_consumers() -> None:
    adapter = NativeForgeAdapter(ROOT)
    adapter.ensure_available()

    abi = adapter.language_owned_abi()

    assert abi.module == "mncs.forge.core.v1"
    assert abi.source_artifact_identity
    assert "evidence_readiness" in abi.functions
    readiness = abi.functions["evidence_readiness"]
    assert readiness["declaring_module"] == "mncs.forge.core.v1"
    assert readiness["name"] == "evidence_readiness"
    assert readiness["function_identity"].startswith("mncs:0.2:function:")
    assert readiness["inputs"][0]["record"]["name"] == "ReadinessInput"
    assert readiness["outputs"][0]["record"]["name"] == "ReadinessResult"
    assert any(
        value.get("record", {}).get("name") == "RequirementInput"
        for value in abi.composites.values()
        if isinstance(value, dict)
    )


def test_native_canonical_material_matches_host_materialization() -> None:
    adapter = NativeForgeAdapter(ROOT)
    adapter.ensure_available()

    result = adapter.execute(
        adapter.native_source,
        ROOT / "examples/execution/native-canonical-probe.json",
    )

    assert result.ok
    assert result.payload is not None
    fields = result.payload["returned"][0]["record"]["fields"]
    by_name = {field[0]: field[1] for field in fields}
    assert by_name["header"] == _sequence_value((67, 1, 3))
    assert by_name["parent"] == _sequence_value(range(32))
    assert by_name["source"] == _sequence_value(reversed(range(32)))
    assert by_name["changed_files"] == _sequence_value((1, 2, 3, 4))


def test_native_backend_execution_accepts_no_argument_status_probe() -> None:
    adapter = NativeForgeAdapter(ROOT)
    adapter.ensure_available()

    result = adapter.execute(
        adapter.native_source,
        ROOT / "examples/execution/native-status-probe.json",
        backend=True,
    )

    assert result.ok
    assert result.payload is not None
    assert result.payload["status"] == "returned"
    assert result.payload["returned"][0]["finite"]["variant_identity"].endswith("::UNKNOWN")


@pytest.mark.parametrize(
    ("stage", "operation", "evidence", "next_stage", "status", "reason"),
    [
        ("NoEpoch", "BeginEpoch", "UNKNOWN", "EpochActive", "PASS", 0),
        ("EpochActive", "RegisterCandidate", "UNKNOWN", "CandidateRegistered", "UNKNOWN", 0),
        ("CandidateReady", "SelectCandidate", "PASS", "CandidateSelected", "PASS", 0),
        ("CandidateReady", "RejectCandidate", "UNKNOWN", "CandidateRejected", "FAIL", 9),
        ("CandidateSelected", "FreezeCandidate", "PASS", "CandidateFrozen", "PASS", 0),
    ],
)
def test_native_lifecycle_preflight_matches_typed_kernel(
    stage: str,
    operation: str,
    evidence: str,
    next_stage: str,
    status: str,
    reason: int,
) -> None:
    adapter = NativeForgeAdapter(ROOT)
    adapter.ensure_available()

    result = adapter.lifecycle_preflight(stage, operation, evidence)

    assert result == NativeLifecycleResult(stage, operation, next_stage, status, reason)


def test_native_lifecycle_preflight_rejects_malformed_structured_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = NativeForgeAdapter(ROOT)
    adapter.ensure_available()

    monkeypatch.setattr(
        adapter,
        "execute",
        lambda *args, **kwargs: NativeInvocation(
            command=("fixture",),
            returncode=0,
            stdout=b"{}",
            stderr=b"",
            payload={"status": "returned", "returned": [{"not_a_record": {}}]},
        ),
    )
    with pytest.raises(ForgeError, match="did not return a record"):
        adapter.lifecycle_preflight("NoEpoch", "BeginEpoch")


def test_native_lifecycle_projection_is_typed_and_bounded() -> None:
    adapter = NativeForgeAdapter(ROOT)
    adapter.ensure_available()

    result = adapter.lifecycle_projection([], current_candidate=None, required_evidence=1)

    assert isinstance(result, NativeLifecycleProjection)
    assert result.stage == "NoEpoch"
    assert result.status == "UNKNOWN"
    assert result.lineage_ok is True
    assert result.epoch_count == 0
    assert result.candidate_count == 0
    assert result.evidence_count == 0
    with pytest.raises(ForgeError, match="32-event bound"):
        adapter.lifecycle_projection(
            [{"kind": "Empty"}] * 33, current_candidate=None, required_evidence=1
        )


def test_native_lifecycle_projection_covers_lineage_freshness_and_terminality() -> None:
    adapter = NativeForgeAdapter(ROOT)
    adapter.ensure_available()
    epoch = "epoch:" + ("01" * 32)
    candidate = "forge-tree-sha256-v1:" + ("02" * 32)
    events = [
        {"kind": "EpochStarted", "epoch": epoch, "parent_epoch": None},
        {
            "kind": "CandidateRegistered",
            "epoch": epoch,
            "candidate": candidate,
            "parent_candidate": None,
        },
        {"kind": "EvidenceObserved", "candidate": candidate, "status": "PASS"},
        {"kind": "CandidateSelected", "candidate": candidate},
        {"kind": "CandidateFrozen", "candidate": candidate},
        {"kind": "EvaluationRecorded", "candidate": candidate, "status": "PASS"},
    ]

    result = adapter.lifecycle_projection(events, current_candidate=candidate, required_evidence=1)

    assert result.stage == "EvaluationComplete"
    assert result.freshness == "Current"
    assert result.disposition == "Selected"
    assert result.lineage_ok is True
    assert result.epoch_count == 1
    assert result.candidate_count == 1
    assert result.evidence_count == 1
    assert result.frozen is True
    assert result.evaluated is True
    assert result.status == "PASS"

    ambiguous = adapter.lifecycle_projection(
        [{"kind": "EpochStarted", "epoch": epoch, "parent_epoch": "epoch:" + ("03" * 32)}],
        current_candidate=None,
        required_evidence=1,
    )
    assert ambiguous.stage == "AmbiguousHistory"
    assert ambiguous.lineage_ok is False
    assert ambiguous.status == "UNKNOWN"


def test_native_lifecycle_projection_accepts_recorded_selection_without_evidence() -> None:
    """Evaluator history slices omit evidence kinds; selection is still lineage-valid."""

    adapter = NativeForgeAdapter(ROOT)
    adapter.ensure_available()
    epoch = "epoch:" + ("01" * 32)
    candidate = "forge-tree-sha256-v1:" + ("02" * 32)
    events = [
        {"kind": "EpochStarted", "epoch": epoch, "parent_epoch": None},
        {
            "kind": "CandidateRegistered",
            "epoch": epoch,
            "candidate": candidate,
            "parent_candidate": None,
        },
        {"kind": "CandidateSelected", "candidate": candidate},
        {"kind": "CandidateFrozen", "candidate": candidate},
    ]

    result = adapter.lifecycle_projection(events, current_candidate=candidate, required_evidence=0)

    assert result.stage == "CandidateFrozen"
    assert result.lineage_ok is True
    assert result.disposition == "Selected"
    assert result.frozen is True
    # No evidence in the projected slice means no PASS claim from the kernel.
    assert result.status == "UNKNOWN"


def test_native_lifecycle_projection_accepts_post_selection_evidence() -> None:
    """Freeze revalidation observes evidence after selection; lineage stays valid."""

    adapter = NativeForgeAdapter(ROOT)
    adapter.ensure_available()
    epoch = "epoch:" + ("01" * 32)
    candidate = "forge-tree-sha256-v1:" + ("02" * 32)
    events = [
        {"kind": "EpochStarted", "epoch": epoch, "parent_epoch": None},
        {
            "kind": "CandidateRegistered",
            "epoch": epoch,
            "candidate": candidate,
            "parent_candidate": None,
        },
        {"kind": "EvidenceObserved", "candidate": candidate, "status": "PASS"},
        {"kind": "CandidateSelected", "candidate": candidate},
        {"kind": "EvidenceObserved", "candidate": candidate, "status": "FAIL"},
    ]

    result = adapter.lifecycle_projection(events, current_candidate=candidate, required_evidence=1)

    assert result.stage == "CandidateSelected"
    assert result.lineage_ok is True
    assert result.disposition == "Selected"


def test_native_reconciliation_projection_is_typed_and_conflict_visible() -> None:
    adapter = NativeForgeAdapter(ROOT)
    adapter.ensure_available()

    result = adapter.reconciliation_projection(
        {
            "build": [{"status": "PASS"}, {"status": "UNKNOWN"}],
            "safety": [{"status": "FAIL", "unsupported_constructs": ["opaque"]}],
        }
    )

    assert result.valid is True
    assert result.reason == 0
    assert result.status == "FAIL"
    assert result.category_count == 2
    assert result.conflicting_category_count == 1
    assert result.observed_count == 3
    assert result.unsupported_count == 1
    assert result.categories[0].status == "UNKNOWN"
    assert result.categories[0].conflict is True
    assert result.categories[0].pass_count == 1
    assert result.categories[0].unknown_count == 1
    assert result.categories[1].status == "FAIL"
    assert result.categories[1].unsupported_count == 1

    empty = adapter.reconciliation_projection({})
    assert empty.status == "UNKNOWN"
    assert empty.category_count == 0
    assert empty.observed_count == 0


def test_native_reconciliation_projection_rejects_malformed_or_unbounded_input() -> None:
    adapter = NativeForgeAdapter(ROOT)
    adapter.ensure_available()

    with pytest.raises(ForgeError, match="between 1 and 8 records"):
        adapter.reconciliation_projection({"too-many": [{"status": "PASS"}] * 9})
    with pytest.raises(ForgeError, match="16-category bound"):
        adapter.reconciliation_projection(
            {f"category-{index}": [{"status": "PASS"}] for index in range(17)}
        )
    with pytest.raises(ForgeError, match="status is invalid"):
        adapter.reconciliation_projection({"malformed": [{"status": []}]})
    with pytest.raises(ForgeError, match="categories are malformed"):
        adapter.reconciliation_projection([])  # type: ignore[arg-type]
    with pytest.raises(ForgeError, match="records are malformed"):
        adapter.reconciliation_projection({"malformed": "not-a-record-list"})  # type: ignore[arg-type]


def _readiness_requirement(
    statuses: list[str],
    *,
    freshness: str = "Current",
    comparable: bool = True,
    environment_match: bool = True,
    policy_match: bool = True,
    authority_match: bool = True,
) -> dict[str, object]:
    return {
        "records": [{"status": status} for status in statuses],
        "freshness": freshness,
        "comparable": comparable,
        "environment_match": environment_match,
        "policy_match": policy_match,
        "authority_match": authority_match,
    }


def test_native_readiness_projection_preserves_classification_and_observation_envelopes() -> None:
    adapter = NativeForgeAdapter(ROOT)
    adapter.ensure_available()

    result = adapter.readiness_projection(
        {
            "present": _readiness_requirement(["PASS"]),
            "failed": _readiness_requirement(["FAIL"]),
            "missing": _readiness_requirement([]),
            "unknown": _readiness_requirement(["UNKNOWN"]),
            "stale": _readiness_requirement(["PASS"], freshness="Stale"),
            "noncomparable": _readiness_requirement(["PASS"], comparable=False),
        },
        candidate_present=True,
        policy_valid=True,
    )

    assert result.valid is True
    assert result.ready is False
    assert result.status == "FAIL"
    assert result.reason == "Failed"
    assert result.present_count == 5
    assert result.missing_count == 1
    assert result.failed_count == 1
    assert result.unknown_count == 1
    assert result.stale_count == 1
    assert result.noncomparable_count == 1
    by_name = dict(
        zip(
            ("failed", "missing", "noncomparable", "present", "stale", "unknown"),
            result.requirements,
            strict=True,
        )
    )
    assert by_name["present"].classification == "Present"
    assert by_name["present"].observed_count == 1
    assert by_name["failed"].classification == "Failed"
    assert by_name["missing"].classification == "Missing"
    assert by_name["unknown"].classification == "Unknown"
    assert by_name["stale"].classification == "Stale"
    assert by_name["noncomparable"].classification == "NonComparable"


def test_native_readiness_projection_is_permutation_invariant_by_opaque_identity() -> None:
    adapter = NativeForgeAdapter(ROOT)
    adapter.ensure_available()
    values = {
        "zeta-check": _readiness_requirement(["PASS"]),
        "alpha-check": _readiness_requirement(["FAIL"]),
        "middle-check": _readiness_requirement(["UNKNOWN"]),
    }

    first = adapter.readiness_projection(values, candidate_present=True, policy_valid=True)
    second = adapter.readiness_projection(
        {
            "middle-check": values["middle-check"],
            "zeta-check": values["zeta-check"],
            "alpha-check": values["alpha-check"],
        },
        candidate_present=True,
        policy_valid=True,
    )

    def semantic_projection(result: object) -> dict[bytes, tuple[str, int, int, int, str]]:
        assert isinstance(result, mncs_native.NativeReadinessProjection)
        return {
            item.identity: (
                item.classification,
                item.pass_count,
                item.fail_count,
                item.unknown_count,
                "stale" if item.stale else "current",
            )
            for item in result.requirements
        }

    assert semantic_projection(first) == semantic_projection(second)
    by_identity = semantic_projection(first)
    assert by_identity[adapter.readiness_requirement_identity("zeta-check")][0] == "Present"
    assert by_identity[adapter.readiness_requirement_identity("alpha-check")][0] == "Failed"
    assert by_identity[adapter.readiness_requirement_identity("middle-check")][0] == "Unknown"


def test_native_readiness_rejects_duplicate_active_requirement_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = NativeForgeAdapter(ROOT)
    adapter.ensure_available()
    original_execute = adapter.execute

    def duplicate_execute(*args: object, **kwargs: object) -> NativeInvocation:
        invocation = original_execute(*args, **kwargs)
        request = args[1] if len(args) > 1 else None
        if isinstance(request, Path) and request.name == "readiness-request.json":
            payload = copy.deepcopy(invocation.payload)
            assert isinstance(payload, dict)
            returned = payload["returned"]
            assert isinstance(returned, list)
            root = returned[0]
            assert isinstance(root, dict)
            root_record = root["record"]
            assert isinstance(root_record, dict)
            result_fields = root_record["fields"]
            assert isinstance(result_fields, list)
            state = next(value for name, value in result_fields if name == "state")
            assert isinstance(state, dict)
            state_record = state["record"]
            assert isinstance(state_record, dict)
            state_fields = state_record["fields"]
            assert isinstance(state_fields, list)
            requirements = next(value for name, value in state_fields if name == "requirements")
            assert isinstance(requirements, dict)
            sequence = requirements["sequence"]
            assert isinstance(sequence, dict)
            values = sequence["values"]
            assert isinstance(values, list)
            values[1] = copy.deepcopy(values[0])
            return NativeInvocation(
                command=invocation.command,
                returncode=invocation.returncode,
                stdout=invocation.stdout,
                stderr=invocation.stderr,
                payload=payload,
            )
        return invocation

    monkeypatch.setattr(adapter, "execute", duplicate_execute)
    mncs_native._READINESS_CACHE.clear()

    with pytest.raises(ForgeError, match="duplicate requirement identity"):
        adapter.readiness_projection(
            {
                "alpha-check": _readiness_requirement(["PASS"]),
                "zeta-check": _readiness_requirement(["FAIL"]),
            },
            candidate_present=True,
            policy_valid=True,
        )


@pytest.mark.parametrize(
    ("overrides", "ready", "status", "reason"),
    [
        ({}, True, "PASS", "Ready"),
        ({"candidate_present": False, "current_candidate": None}, False, "UNKNOWN", "NoCandidate"),
        ({"candidate_matches": False}, False, "FAIL", "CandidateMismatch"),
        (
            {"requested_candidate": "forge-tree-sha256-v1:" + "02" * 32},
            False,
            "FAIL",
            "CandidateMismatch",
        ),
        ({"candidate_current": False}, False, "UNKNOWN", "CandidateStale"),
        ({"mode": "evaluator", "frozen": False}, False, "UNKNOWN", "FreezeMissing"),
        (
            {"mode": "evaluator", "frozen": True, "selected": False},
            False,
            "UNKNOWN",
            "SelectionMissing",
        ),
        (
            {"mode": "evaluator", "frozen": True, "selected": True, "freeze_current": False},
            False,
            "UNKNOWN",
            "FreezeStale",
        ),
        ({"request_valid": False}, False, "FAIL", "InvalidRequest"),
    ],
)
def test_native_bundle_preconditions_are_typed_and_fail_closed(
    overrides: dict[str, object], ready: bool, status: str, reason: str
) -> None:
    adapter = NativeForgeAdapter(ROOT)
    adapter.ensure_available()
    values: dict[str, object] = {
        "requested_candidate": None,
        "current_candidate": "forge-tree-sha256-v1:" + "01" * 32,
        "candidate_present": True,
        "candidate_matches": True,
        "candidate_current": True,
        "selected": True,
        "frozen": True,
        "freeze_current": True,
        "mode": "development",
        "request_valid": True,
        "evidence_status": "PASS",
        "evidence_ready": True,
    }
    values.update(overrides)
    result = adapter.bundle_precondition_projection(**values)  # type: ignore[arg-type]

    assert isinstance(result, NativeBundlePreconditionProjection)
    assert result.ready is ready
    assert result.status == status
    assert result.reason == reason
    assert result.evidence_status == "PASS"
    assert result.evidence_ready is True
    assert result.valid is True


def test_native_readiness_projection_reports_global_policy_and_candidate_reasons() -> None:
    adapter = NativeForgeAdapter(ROOT)
    adapter.ensure_available()
    requirement = {
        "records": [{"status": "PASS"}],
        "freshness": "Current",
        "comparable": True,
        "environment_match": True,
        "policy_match": True,
        "authority_match": True,
    }

    policy = adapter.readiness_projection(
        {"required": requirement}, candidate_present=True, policy_valid=False
    )
    assert policy.status == "UNKNOWN"
    assert policy.reason == "PolicyInvalid"
    assert policy.ready is False

    no_candidate = adapter.readiness_projection(
        {"required": requirement}, candidate_present=False, policy_valid=True
    )
    assert no_candidate.status == "UNKNOWN"
    assert no_candidate.reason == "NoCandidate"
    assert no_candidate.ready is False


def test_native_readiness_projection_rejects_malformed_or_unbounded_input() -> None:
    adapter = NativeForgeAdapter(ROOT)
    adapter.ensure_available()
    requirement = _readiness_requirement(["PASS"])

    with pytest.raises(ForgeError, match="16-requirement bound"):
        adapter.readiness_projection(
            {f"requirement-{index}": requirement for index in range(17)},
            candidate_present=True,
            policy_valid=True,
        )
    with pytest.raises(ForgeError, match="eight-record bound"):
        adapter.readiness_projection(
            {"too-many": _readiness_requirement(["PASS"] * 9)},
            candidate_present=True,
            policy_valid=True,
        )
    with pytest.raises(ForgeError, match="status is invalid"):
        adapter.readiness_projection(
            {"malformed": _readiness_requirement(["MAYBE"])},
            candidate_present=True,
            policy_valid=True,
        )


def test_native_readiness_projection_rejects_malformed_structured_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = NativeForgeAdapter(ROOT)
    adapter.ensure_available()
    monkeypatch.setattr(
        adapter,
        "execute",
        lambda *args, **kwargs: NativeInvocation(
            command=("fixture",),
            returncode=0,
            stdout=b"{}",
            stderr=b"",
            payload={"status": "returned", "returned": [{"record": {"fields": []}}]},
        ),
    )
    mncs_native._READINESS_CACHE.clear()

    with pytest.raises(ForgeError, match="readiness result type disagrees with language ABI"):
        adapter.readiness_projection(
            {"required": _readiness_requirement(["PASS"])},
            candidate_present=True,
            policy_valid=True,
        )


def test_native_cache_identity_changes_with_content_even_at_same_path(tmp_path: Path) -> None:
    source = tmp_path / "source.mncs"
    source.write_text("one", encoding="utf-8")
    first = NativeForgeAdapter._content_identity([source])
    source.write_text("two", encoding="utf-8")
    second = NativeForgeAdapter._content_identity([source])

    assert first != second
