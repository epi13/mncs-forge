# Forge → `mncs-store` migration

Status: Store-backed persistence is now Forge's canonical ordinary path. Forge
retains candidate lifecycle, assurance, repair, evidence interpretation, and
workflow authority. Store owns durable representation, generations, integrity,
publication, recovery, and generic structure.

## Canonical path

```text
Forge domain record
      │ typed payload + Forge identity binding
      ▼
StoreBackedRecordStore
      ▼
mncs_store.EmbeddedStore
      ▼
retained Store application session + durable local realization
```

The supported adapter imports only the `mncs_store` package. It does not import
Store tests or the historical Phase-1/Phase-2 drivers. The Forge identity is
carried as opaque domain data in a versioned binding:

```text
Forge record identity
  ├─ domain schema: mncs-forge.record/<kind>/1
  └─ domain identity: original Forge identity bytes
        │
        ▼
Store binding identity → logical object → content identity/root → generation
```

Forge descriptors carry record context, source schema version, timestamp, and
provenance. Store ordinals provide the current ordered projection; the derived
Forge ledger shape is reconstructed for existing services and is not authority.

## Proof surface

The adapter and Store realization now cover:

- identical duplicate idempotence and same-identity conflicting-content rejection;
- close/reopen projection with a retained application artifact;
- explicit typed stale-generation results without automatic retry;
- local process-shared publication locking, atomic head replacement, file and
  directory durability barriers, and evidence-based old-or-new recovery;
- bounded chunk/tree objects up to and beyond the historical 992-byte oracle
  ceiling;
- descriptor provenance and separate Forge/Store identity domains; and
- deletion and deterministic rebuild of `ledger-index.json` from current Store
  objects.

The legacy `LocalRecordStore` and `Ledger` remain only as a migration source and
differential/reference oracle. `migration.import_legacy_state` authenticates
the old hash-linked JSONL state first, records its source representation
identity and transformation identity in the Store descriptor, and then hands
typed Forge records to the Store adapter. Ordinary Forge construction never
creates either legacy class.

Per-action locks remain in the Forge adapter because action execution
serialization is workflow semantics, not generic Store publication.

## Remaining boundary

The current retained research-bytecode realization proves the existing
streaming `mncs.std.sha256.v1` semantics and is the supported local default.
Large-object measurements must record its bounded operation cost. Compiled
native backends currently reject effect-bearing Store applications or use
one-shot execution in the embed boundary; that is a language/runtime pressure,
not a reason to duplicate persistence semantics in Forge.

Fabric's next pressure should therefore be Store's multi-object generation
surface: atomic batch publication and compaction/reclamation of controller,
worker, placement, bundle, and execution-receipt objects while preserving
per-object identity bindings and stale-generation outcomes.
