"""Differential checks for Forge's canonical Store-backed persistence."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from mncs_forge.migration import import_legacy_state
from mncs_forge.record_store import LocalRecordStore
from mncs_forge.records import ForgeRecord, RecordType, new_record
from mncs_forge.store_record_store import StoreBackedRecordStore


pytestmark = pytest.mark.skipif(
    not os.environ.get("MNCS_STORE_ARTIFACT"),
    reason="campaign proof requires an admitted retained Store artifact",
)


def candidate(
    value: int,
    *,
    hypothesis: str = "candidate",
    parent: str | None = None,
) -> ForgeRecord:
    return new_record(
        RecordType.CANDIDATE,
        {
            "candidate_id": f"forge-tree-sha256-v1:{value:064x}",
            "parent_candidate": parent,
            "changed_files": [],
            "declared_hypothesis": hypothesis,
            "generator_identity": "generator",
            "generator_configuration_identity": "configuration",
            "source_epoch": "epoch:fixture",
            "registered_at": "2026-01-01T00:00:00+00:00",
            "current_file_identities": {},
            "useful_benefit_objective": "contract.md",
            "objective_identity": "sha256:fixture",
            "supersedes": None,
        },
    )


def test_store_backed_duplicate_conflict_reopen_and_index_rebuild(tmp_path: Path) -> None:
    record = candidate(1)
    with StoreBackedRecordStore(tmp_path) as store:
        first = store.commit("candidates", "candidate", record)
        duplicate = store.commit("candidates", "candidate", record)
        assert first == duplicate
        with pytest.raises(Exception) as issue:
            store.commit(
                "candidates",
                "candidate",
                candidate(1, hypothesis="same Forge identity, different content"),
            )
        assert getattr(issue.value, "code", None) == "RECORD_EXISTS"
        assert store.verify()["canonical"] == "mncs-store"
        store.index_path.unlink()
        assert store.records()[0].payload["candidate_id"] == record["candidate_id"]
        assert store.index_path.is_file()

    with StoreBackedRecordStore(tmp_path) as reopened:
        assert [entry.sequence for entry in reopened.records()] == [1]
        assert reopened.store.current_generation == 1


def test_store_and_forge_identity_domains_are_explicit(tmp_path: Path) -> None:
    record = candidate(2)
    with StoreBackedRecordStore(tmp_path) as store:
        store.commit("candidates", "candidate", record)
        stored = store.store.current_objects()[0]
        assert stored.domain_schema == b"mncs-forge.record/candidate/1"
        assert stored.domain_identity == record["candidate_id"].encode()
        assert stored.logical_id != stored.domain_identity
        assert stored.content_id != stored.logical_id
        assert stored.representation_root != stored.content_id
        assert stored.generation == 1
        assert len(store.store.typed_records("provenance")) == 1


def test_forge_lineage_is_projected_into_current_store_relations(tmp_path: Path) -> None:
    parent = candidate(4)
    child = candidate(
        5,
        parent=parent["candidate_id"],
    )
    with StoreBackedRecordStore(tmp_path) as store:
        store.commit("candidates", "candidate", parent)
        store.commit("candidates", "candidate", child)
        relations = store.store.typed_records("relations")
        assert len(relations) == 1
        assert int.from_bytes(relations[0][60:68], "big") == 2
        assert len(store.store.typed_records("provenance")) == 2


def test_historical_ledger_import_preserves_source_provenance(tmp_path: Path) -> None:
    legacy = tmp_path / "legacy"
    current = tmp_path / "current"
    source_record = candidate(3)
    LocalRecordStore(legacy).commit("candidates", "candidate", source_record)

    with StoreBackedRecordStore(current) as destination:
        imported = import_legacy_state(legacy, destination)
        assert [entry.payload["candidate_id"] for entry in imported] == [source_record["candidate_id"]]
        stored = destination.store.current_objects()[0]
        descriptor = stored.descriptor.split(b"\n", 1)[1]
        metadata = json.loads(descriptor)
        assert metadata["provenance"]["origin"] == "forge-legacy-import"
        assert metadata["provenance"]["source_representation_identity"].startswith(
            "forge-legacy-jsonl-sha256:"
        )
        assert metadata["provenance"]["transformation_identity"] == (
            "mncs-forge:legacy-jsonl-to-store:v1"
        )
        assert metadata["source_schema_version"] == "1"
