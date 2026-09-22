"""Forge's supported Store-backed persistence adapter.

Store owns durable objects, generations, integrity, publication, and recovery.
Forge owns the record vocabulary and projects the current Store generation into
the existing inward-facing ``RecordReader`` shape so application services do
not need to know about Store's physical representation.

The historical ``LocalRecordStore``/``Ledger`` implementation remains in its
modules as a differential and migration oracle during this cutover.  It is not
constructed by :class:`mncs_forge.engine.Forge`.
"""

from __future__ import annotations

import hashlib
import json
import os
import struct
import sys
import tempfile
from collections.abc import Collection, Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from filelock import FileLock, Timeout

from .errors import ForgeError
from .ports import RecordCommitter, RecordReader
from .records import (
    CURRENT_SCHEMA_VERSION,
    PERSISTED_RECORD_CONTEXTS,
    ForgeRecord,
    JsonObject,
    LedgerEntry,
    parse_record,
    persisted_record_context,
    safe_record_identity,
)
from .serialization import canonical_bytes


GENESIS = "0" * 64
_DESCRIPTOR_PREFIX = b"mncs-forge.store.descriptor/1\n"
_TRANSFORMATION_IDENTITY = "mncs-forge:legacy-jsonl-to-store:v1"
_STORE_PACKAGE = "mncs-store/python"


def _load_store_types(store_root: Path | None = None) -> tuple[Any, ...]:
    """Load only the supported Store package, never Store test drivers."""

    try:
        from mncs_store import (  # type: ignore[import-not-found]
            BoundObjectInput,
            EmbeddedStore,
            StoreError,
            StoreResultCode,
        )

        return BoundObjectInput, EmbeddedStore, StoreError, StoreResultCode
    except ModuleNotFoundError:
        configured = os.environ.get("MNCS_STORE_ROOT")
        root = Path(configured).expanduser().resolve() if configured else store_root
        if root is None:
            root = Path(__file__).resolve().parents[3] / "mncs-store"
        package_path = root / "python"
        if not (package_path / "mncs_store").is_dir():
            raise ForgeError(
                "STORE_ADAPTER_UNAVAILABLE",
                f"supported Store package is unavailable: {package_path / 'mncs_store'}",
            )
        sys.path.insert(0, str(package_path))
        try:
            from mncs_store import (  # type: ignore[import-not-found]
                BoundObjectInput,
                EmbeddedStore,
                StoreError,
                StoreResultCode,
            )
        except ModuleNotFoundError as exc:
            raise ForgeError(
                "STORE_ADAPTER_UNAVAILABLE",
                "supported Store package could not be imported",
            ) from exc
        return BoundObjectInput, EmbeddedStore, StoreError, StoreResultCode


def _timestamp() -> str:
    return datetime.now(UTC).isoformat()


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _json_object(value: bytes, *, label: str) -> JsonObject:
    try:
        parsed = json.loads(value)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ForgeError("STORE_DESCRIPTOR_MALFORMED", f"{label} is not canonical JSON") from exc
    if not isinstance(parsed, dict):
        raise ForgeError("STORE_DESCRIPTOR_MALFORMED", f"{label} is not an object")
    return cast(JsonObject, parsed)


class StoreBackedRecordStore(RecordReader, RecordCommitter):
    """Forge record persistence over one retained local Store application."""

    def __init__(
        self,
        state_dir: Path,
        *,
        failpoint: Any | None = None,
        recover_on_open: bool = True,
        store_root: Path | None = None,
        session: Any | None = None,
    ) -> None:
        BoundObjectInput, EmbeddedStore, _StoreError, _StoreResultCode = _load_store_types(store_root)
        self.state_dir = Path(state_dir)
        self.store_path = self.state_dir / "store"
        self.index_path = self.state_dir / "ledger-index.json"
        self.action_locks_dir = self.state_dir / "action-locks"
        self.store = EmbeddedStore(
            self.store_path,
            session=session,
            failpoint=failpoint,
            verify_on_open=True,
        )
        self._store_error_type = _StoreError
        self._store_result_code = _StoreResultCode
        self._bound_object_input_type = BoundObjectInput
        # This is a generation-bound, rebuildable projection.  Store remains
        # authoritative; the projection only prevents every Forge query from
        # rereading and revalidating the same current generation.
        self._projection_generation: int | None = None
        self._projection_feed: str | None = None
        self._projection: tuple[LedgerEntry, ...] = ()
        if recover_on_open:
            entries = self._all_entries()
            self._ensure_index(entries)

    # ---- Store/Forge identity binding ---------------------------------

    @staticmethod
    def _context(record_group: str, ledger_kind: str):
        return persisted_record_context(group=record_group, ledger_kind=ledger_kind)

    @staticmethod
    def _domain_schema(ledger_kind: str) -> bytes:
        return f"mncs-forge.record/{ledger_kind}/{CURRENT_SCHEMA_VERSION}".encode()

    @staticmethod
    def _domain_identity(record: ForgeRecord, identity_field: str) -> bytes:
        identity = record.get(identity_field)
        if not isinstance(identity, str) or not identity:
            raise ForgeError("RECORD_IDENTITY", f"record requires string identity field {identity_field}")
        return identity.encode()

    @staticmethod
    def _descriptor(
        *,
        record_group: str,
        ledger_kind: str,
        identity_field: str,
        timestamp: str,
        source_schema_version: str = CURRENT_SCHEMA_VERSION,
        provenance: Mapping[str, object] | None = None,
    ) -> bytes:
        value: JsonObject = {
            "schema_version": "1",
            "record_group": record_group,
            "ledger_kind": ledger_kind,
            "identity_field": identity_field,
            "timestamp": timestamp,
            "source_schema_version": source_schema_version,
            "provenance": dict(provenance or {"origin": "forge-native"}),
        }
        return _DESCRIPTOR_PREFIX + canonical_bytes(value)

    @staticmethod
    def _decode_descriptor(raw: bytes) -> JsonObject:
        if not raw.startswith(_DESCRIPTOR_PREFIX):
            raise ForgeError("STORE_DESCRIPTOR_MALFORMED", "Store descriptor is not Forge descriptor v1")
        value = _json_object(raw[len(_DESCRIPTOR_PREFIX) :], label="Forge Store descriptor")
        required = {
            "schema_version",
            "record_group",
            "ledger_kind",
            "identity_field",
            "timestamp",
            "source_schema_version",
            "provenance",
        }
        if set(value) != required or value["schema_version"] != "1":
            raise ForgeError("STORE_DESCRIPTOR_MALFORMED", "Forge Store descriptor shape is invalid")
        if not all(isinstance(value[key], str) for key in ("record_group", "ledger_kind", "identity_field", "timestamp", "source_schema_version")):
            raise ForgeError("STORE_DESCRIPTOR_MALFORMED", "Forge Store descriptor text fields are invalid")
        if not isinstance(value["provenance"], dict):
            raise ForgeError("STORE_DESCRIPTOR_MALFORMED", "Forge Store provenance is not an object")
        return value

    def _store_failure(self, error: Exception) -> ForgeError:
        code = getattr(error, "code", "INTEGRITY_FAILURE")
        code_text = getattr(code, "value", str(code))
        if code_text == "IDENTITY_CONFLICT":
            return ForgeError("RECORD_EXISTS", str(error))
        if code_text == "STALE_GENERATION":
            return ForgeError("STORE_STALE_GENERATION", str(error))
        if code_text == "PLATFORM_UNSUPPORTED":
            return ForgeError("STORE_PLATFORM_UNSUPPORTED", str(error))
        if code_text == "INTEGRITY_FAILURE":
            return ForgeError("STORE_INTEGRITY", str(error))
        return ForgeError(f"STORE_{code_text}", str(error))

    # ---- authoritative Store projection -------------------------------

    def _entry_for_object(self, stored: Any) -> LedgerEntry:
        descriptor = self._decode_descriptor(stored.descriptor)
        group = str(descriptor["record_group"])
        kind = str(descriptor["ledger_kind"])
        context = self._context(group, kind)
        expected_schema = self._domain_schema(kind)
        if stored.domain_schema != expected_schema:
            raise ForgeError("STORE_BINDING_MISMATCH", "Forge domain schema binding differs")
        try:
            identity = stored.domain_identity.decode()
        except UnicodeDecodeError as exc:
            raise ForgeError("STORE_BINDING_MISMATCH", "Forge domain identity is not UTF-8") from exc
        record = parse_record(
            _json_object(stored.payload, label="Forge Store record payload"),
            expected_type=context.record_type,
        )
        record_identity = record.get(context.identity_field)
        if record_identity != identity:
            raise ForgeError("STORE_BINDING_MISMATCH", "Forge record identity differs from Store binding")
        timestamp = str(descriptor["timestamp"])
        source_schema_version = str(descriptor["source_schema_version"])
        return LedgerEntry(
            sequence=int(stored.ordinal),
            timestamp=timestamp,
            kind=kind,
            previous_hash=GENESIS if stored.ordinal == 1 else "",
            payload=record,
            entry_hash="",
            schema_version=CURRENT_SCHEMA_VERSION,
            source_schema_version=source_schema_version,
        )

    @staticmethod
    def _with_projection_hashes(entries: list[LedgerEntry]) -> list[LedgerEntry]:
        previous = GENESIS
        projected: list[LedgerEntry] = []
        for entry in entries:
            body: JsonObject = {
                "record_type": "ledger_entry",
                "schema_version": CURRENT_SCHEMA_VERSION,
                "sequence": entry.sequence,
                "timestamp": entry.timestamp,
                "kind": entry.kind,
                "previous_hash": previous,
                "payload": entry.payload.to_json(),
            }
            entry_hash = _sha256(canonical_bytes(body))
            projected_entry = LedgerEntry(
                sequence=entry.sequence,
                timestamp=entry.timestamp,
                kind=entry.kind,
                previous_hash=previous,
                payload=entry.payload,
                entry_hash=entry_hash,
                schema_version=CURRENT_SCHEMA_VERSION,
                source_schema_version=entry.source_schema_version,
            )
            projected.append(projected_entry)
            previous = entry_hash
        return projected

    def _all_entries(self) -> list[LedgerEntry]:
        generation = self.store.current_generation
        feed = self.store.commit_feed(generation).hex()
        if (
            self._projection_generation == generation
            and self._projection_feed == feed
        ):
            return list(self._projection)
        try:
            objects = self.store.current_objects()
            entries = [self._entry_for_object(stored) for stored in objects]
        except ForgeError:
            raise
        except Exception as exc:
            raise ForgeError("STORE_INTEGRITY", f"cannot project Store records: {exc}") from exc
        entries.sort(key=lambda entry: entry.sequence)
        expected = list(range(1, len(entries) + 1))
        if [entry.sequence for entry in entries] != expected:
            raise ForgeError("STORE_ORDER_INVALID", "Store ordinals are not a contiguous Forge history")
        projected = self._with_projection_hashes(entries)
        self._projection_generation = generation
        self._projection_feed = feed
        self._projection = tuple(projected)
        return list(projected)

    def records(self, kind: str | None = None) -> list[LedgerEntry]:
        all_entries = self._all_entries()
        entries = all_entries
        if kind is not None:
            entries = [entry for entry in entries if entry.kind == kind]
        self._ensure_index(all_entries)
        return entries

    def records_for(self, kinds: Collection[str]) -> list[LedgerEntry]:
        selected = frozenset(kinds)
        all_entries = self._all_entries()
        entries = [entry for entry in all_entries if entry.kind in selected]
        self._ensure_index(all_entries)
        return entries

    def verify(self) -> dict[str, object]:
        try:
            result = self.store.verify()
        except Exception as exc:
            if isinstance(exc, self._store_error_type):
                raise self._store_failure(exc) from exc
            raise ForgeError("STORE_INTEGRITY", f"Store verification failed: {exc}") from exc
        entries = self._all_entries()
        self._ensure_index(entries)
        return {
            **result,
            "canonical": "mncs-store",
            # Preserve the inward-facing Ledger verification contract while
            # keeping Store's generation/object fields authoritative.
            "entries": len(entries),
            "forge_records": len(entries),
            "ledger": "derived projection; not authoritative",
        }

    def _typed_store_metadata(
        self,
        *,
        record: ForgeRecord,
        record_group: str,
        ledger_kind: str,
        domain_identity: bytes,
        payload: bytes,
        expected_generation: int,
    ) -> tuple[list[bytes], list[bytes]]:
        """Project Forge references into Store-owned generic relation records."""

        next_generation = expected_generation + 1
        source = self.store.logical_id_for(self._domain_schema(ledger_kind), domain_identity)
        producer = self.store.content_identity(b"mncs-forge/store-record-store/1")[:12]
        transformation = self.store.content_identity(
            f"mncs-forge:{record_group}:{ledger_kind}:store-binding/1".encode()
        )[:12]
        ancestry = self.store.content_identity(struct.pack(">Q", expected_generation))
        provenance = self.store.make_provenance(
            source=source,
            producer=producer,
            transformation=transformation,
            generation=next_generation,
            evidence=self.store.content_identity(payload),
            ancestry=ancestry,
        )
        provenance_identity = self.store.content_identity(provenance)

        references: list[tuple[str, bytes, bytes]] = []
        parent = record.get("parent_candidate")
        if isinstance(parent, str) and parent:
            references.append(
                (
                    "parent-candidate",
                    self._domain_schema("candidate"),
                    parent.encode(),
                )
            )
        supersedes = record.get("supersedes")
        if isinstance(supersedes, str) and supersedes:
            references.append(
                ("supersedes-candidate", self._domain_schema("candidate"), supersedes.encode())
            )
        for field in ("candidate_parent_identity", "supersedes_output_identity"):
            value = record.get(field)
            if isinstance(value, str) and value:
                references.append((field, b"", value.encode()))
        evidence = record.get("evidence_identities")
        if isinstance(evidence, (list, tuple)):
            for value in evidence:
                if isinstance(value, str) and value:
                    references.append(("evidence", b"", value.encode()))
        input_identities = record.get("input_identities")
        if isinstance(input_identities, Mapping):
            for key, value in sorted(input_identities.items(), key=lambda item: str(item[0])):
                if isinstance(value, str) and value:
                    references.append((f"input:{key}", b"", value.encode()))

        if not references:
            return [], [provenance]

        relations: list[bytes] = []
        for ordinal, (label, schema, identity) in enumerate(references):
            if schema:
                target = self.store.logical_id_for_domain(schema, identity)
            else:
                matches = self.store.logical_ids_for_domain_identity(identity)
                target = matches[0] if matches else None
            if target is None or target == source:
                continue
            relation_type = self.store.content_identity(
                f"mncs-forge.relation.{label}/1".encode()
            )
            relations.append(
                self.store.make_relation(
                    relation_type=relation_type,
                    source=source,
                    target=target,
                    generation=next_generation,
                    provenance=provenance_identity,
                    ordinal=ordinal,
                )
            )
        return relations, [provenance]

    # ---- commit/recovery ----------------------------------------------

    def _commit_record(
        self,
        record_group: str,
        ledger_kind: str,
        record: ForgeRecord,
        *,
        timestamp: str | None = None,
        source_schema_version: str = CURRENT_SCHEMA_VERSION,
        provenance: Mapping[str, object] | None = None,
    ) -> LedgerEntry:
        context = self._context(record_group, ledger_kind)
        if record.record_type is not context.record_type:
            raise ForgeError(
                "RECORD_TYPE_MISMATCH",
                f"storage context requires {context.record_type.value}, got {record.record_type.value}",
            )
        if source_schema_version == CURRENT_SCHEMA_VERSION and record.schema_version != CURRENT_SCHEMA_VERSION:
            raise ForgeError("RECORD_VERSION_WRITE", "new records require schema version 1")
        domain_identity = self._domain_identity(record, context.identity_field)
        payload = canonical_bytes(record.to_json())
        if len(payload) > 4_000_000:
            raise ForgeError("RECORD_SIZE", "Forge record exceeds the Store object byte limit")
        expected = self.store.current_generation
        descriptor = self._descriptor(
            record_group=record_group,
            ledger_kind=ledger_kind,
            identity_field=context.identity_field,
            timestamp=timestamp or _timestamp(),
            source_schema_version=source_schema_version,
            provenance=provenance,
        )
        relations, typed_provenance = self._typed_store_metadata(
            record=record,
            record_group=record_group,
            ledger_kind=ledger_kind,
            domain_identity=domain_identity,
            payload=payload,
            expected_generation=expected,
        )
        try:
            result = self.store.put_bound_object(
                domain_schema=self._domain_schema(ledger_kind),
                domain_identity=domain_identity,
                descriptor=descriptor,
                payload=payload,
                expected_generation=expected,
                relations=relations,
                provenance=typed_provenance,
            )
        except self._store_error_type as exc:
            raise self._store_failure(exc) from exc
        if result.code.value == "STALE_GENERATION":
            raise ForgeError(
                "STORE_STALE_GENERATION",
                f"Store expected generation {expected}, observed {result.observed_generation}; retry is consumer policy",
            )
        if result.code.value not in {"COMMITTED", "DUPLICATE"}:
            raise ForgeError("STORE_COMMIT_REJECTED", f"Store returned {result.code.value}")
        entries = self._all_entries()
        matching = [entry for entry in entries if entry.payload.get(context.identity_field) == record.get(context.identity_field)]
        if len(matching) != 1:
            raise ForgeError("STORE_INTEGRITY", "committed Store object is absent from the Forge projection")
        self._ensure_index(entries)
        return matching[0]

    def commit(self, record_group: str, ledger_kind: str, record: ForgeRecord) -> LedgerEntry:
        return self._commit_record(record_group, ledger_kind, record)

    def commit_batch(
        self,
        records: Collection[tuple[str, str, ForgeRecord]],
    ) -> list[LedgerEntry]:
        """Commit multiple Forge records into one generic Store generation."""

        prepared: list[tuple[str, str, ForgeRecord, str]] = []
        objects: list[Any] = []
        expected = self.store.current_generation
        next_generation = expected + 1
        for record_group, ledger_kind, record in records:
            context = self._context(record_group, ledger_kind)
            if record.record_type is not context.record_type:
                raise ForgeError(
                    "RECORD_TYPE_MISMATCH",
                    f"storage context requires {context.record_type.value}, got {record.record_type.value}",
                )
            if record.schema_version != CURRENT_SCHEMA_VERSION:
                raise ForgeError("RECORD_VERSION_WRITE", "new records require schema version 1")
            domain_identity = self._domain_identity(record, context.identity_field)
            payload = canonical_bytes(record.to_json())
            if len(payload) > 4_000_000:
                raise ForgeError("RECORD_SIZE", "Forge record exceeds the Store object byte limit")
            descriptor = self._descriptor(
                record_group=record_group,
                ledger_kind=ledger_kind,
                identity_field=context.identity_field,
                timestamp=_timestamp(),
            )
            relations, typed_provenance = self._typed_store_metadata(
                record=record,
                record_group=record_group,
                ledger_kind=ledger_kind,
                domain_identity=domain_identity,
                payload=payload,
                expected_generation=expected,
            )
            objects.append(
                self._bound_object_input_type(
                    self._domain_schema(ledger_kind),
                    domain_identity,
                    descriptor,
                    payload,
                    tuple(relations),
                    tuple(typed_provenance),
                )
            )
            prepared.append((record_group, ledger_kind, record, context.identity_field))
        if not objects:
            raise ForgeError("STORE_BATCH", "Store record batch must not be empty")
        try:
            result = self.store.put_bound_objects(objects, expected_generation=expected)
        except self._store_error_type as exc:
            raise self._store_failure(exc) from exc
        if result.code.value == "STALE_GENERATION":
            raise ForgeError(
                "STORE_STALE_GENERATION",
                f"Store expected generation {expected}, observed {result.observed_generation}; retry is consumer policy",
            )
        if result.code.value not in {"COMMITTED", "DUPLICATE"}:
            raise ForgeError("STORE_COMMIT_REJECTED", f"Store returned {result.code.value}")
        entries = self._all_entries()
        matched: list[LedgerEntry] = []
        for _record_group, _ledger_kind, record, identity_field in prepared:
            candidates = [
                entry
                for entry in entries
                if entry.payload.get(identity_field) == record.get(identity_field)
            ]
            if len(candidates) != 1:
                raise ForgeError("STORE_INTEGRITY", "committed Store object is absent from the Forge projection")
            matched.append(candidates[0])
        self._ensure_index(entries)
        return matched

    def import_legacy_entries(
        self,
        entries: Collection[LedgerEntry],
        *,
        source_representation_identity: str,
    ) -> list[LedgerEntry]:
        """Import old Forge history through a narrow migration-only boundary."""

        imported: list[LedgerEntry] = []
        for entry in sorted(entries, key=lambda item: item.sequence):
            context = self._context(
                PERSISTED_RECORD_CONTEXTS[entry.kind].group,
                entry.kind,
            )
            provenance: JsonObject = {
                "origin": "forge-legacy-import",
                "source_representation_identity": source_representation_identity,
                "source_entry_identity": entry.entry_hash,
                "transformation_identity": _TRANSFORMATION_IDENTITY,
                "unknown_or_lossy": [],
            }
            imported.append(
                self._commit_record(
                    context.group,
                    entry.kind,
                    entry.payload,
                    timestamp=entry.timestamp,
                    source_schema_version=entry.source_schema_version,
                    provenance=provenance,
                )
            )
        return imported

    def recover(self) -> dict[str, int]:
        try:
            self._projection_generation = None
            self._projection_feed = None
            self._projection = ()
            self.store.recover()
        except self._store_error_type as exc:
            raise self._store_failure(exc) from exc
        entries = self._all_entries()
        self._ensure_index(entries)
        return {"completed": 0, "abandoned": 0}

    # ---- derived index and Forge action serialization -----------------

    def _index_data(self, entries: list[LedgerEntry]) -> JsonObject:
        by_kind: dict[str, list[int]] = {}
        identities: dict[str, int] = {}
        for entry in entries:
            by_kind.setdefault(entry.kind, []).append(entry.sequence)
            context = PERSISTED_RECORD_CONTEXTS[entry.kind]
            identity = entry.payload.get(context.identity_field)
            if isinstance(identity, str):
                identities[f"{entry.kind}:{identity}"] = entry.sequence
        return {
            "schema_version": 1,
            "canonical": "mncs-store",
            "store_generation": self.store.current_generation,
            "store_commit_feed": self.store.commit_feed().hex(),
            "entry_count": len(entries),
            "sequences_by_kind": cast(JsonObject, by_kind),
            "identity_sequences": cast(JsonObject, identities),
        }

    def _ensure_index(self, entries: list[LedgerEntry]) -> None:
        expected = canonical_bytes(self._index_data(entries)) + b"\n"
        try:
            if self.index_path.is_file() and self.index_path.read_bytes() == expected:
                return
            self.index_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                dir=self.index_path.parent,
                prefix=f".{self.index_path.name}.",
                suffix=".stage",
                delete=False,
            ) as staged:
                staged.write(expected)
                staged.flush()
                os.fsync(staged.fileno())
                temporary = Path(staged.name)
            os.replace(temporary, self.index_path)
            try:
                descriptor = os.open(self.index_path.parent, os.O_RDONLY)
            except OSError:
                return
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        except OSError as exc:
            raise ForgeError("STORE_INDEX_PROJECTION", f"cannot update derived Store index: {exc}") from exc

    @contextmanager
    def action_execution(self, action_id: str, *, timeout: float = 30) -> Iterator[None]:
        path = self.action_locks_dir / f"{safe_record_identity(action_id)}.lock"
        self.action_locks_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            with FileLock(str(path), timeout=timeout):
                yield
        except Timeout as exc:
            raise ForgeError("ACTION_EXECUTION_BUSY", "verifier action is already executing") from exc

    def close(self) -> None:
        self.store.close()

    def __enter__(self) -> "StoreBackedRecordStore":
        return self

    def __exit__(self, _exc_type: object, _exc: object, _traceback: object) -> None:
        self.close()
