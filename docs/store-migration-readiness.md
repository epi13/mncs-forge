# Forge → `mncs-store` migration readiness

Status: audit complete; Forge's private persistence remains canonical until the
gates below have executable parity evidence.

This document records the first Forge workload audit for the family Store
campaign. It is a migration boundary, not a second persistence design. Forge
continues to own candidate lifecycle, assurance, repair, evidence
interpretation, and authority. Store may own durable representation,
generations, integrity, recovery, provenance, and generic relations.

## Current canonical surfaces

`LocalRecordStore` and `Ledger` currently provide the authoritative Forge
state path:

- immutable typed record files;
- a hash-linked JSONL history with sequence and predecessor ordering;
- transaction journals binding the record bytes and complete replacement ledger
  to one expected predecessor;
- atomic publication, file and directory barriers, and startup recovery;
- a derived `ledger-index.json`; and
- per-action locks for durable verifier execution.

The current limits are measurable: immutable records are admitted up to
4,000,000 bytes, ledger lines up to 16,000,000 bytes, and the local
implementation uses a filesystem lock around the record-plus-ledger decision.
Historical 0.1 representations remain readable and are not rewritten.

## Intended Store mapping

| Forge authority or surface | Store target | Migration condition |
|---|---|---|
| immutable Forge record | typed immutable Store object | Store must accept the real record payload, read it back by a stable binding, and preserve the Forge identity as domain data |
| ledger entry and ordering | Store generation/event object plus `relationship.v2` ordinal | Store needs a consumer-visible ordered commit feed and an atomic expected-generation transition |
| Forge record identity | Forge domain identity plus Store logical/content identities | A typed alias/binding is required; Store identity must not replace or reinterpret Forge identity |
| transaction journal | Store publication/recovery | Old-or-new recovery, torn-publication classification, and conflict behavior must match the Forge differential |
| `ledger-index.json` | `mncs-index` projection | The index must be deletable and rebuildable from Store objects/feed state |
| receipt/evidence lineage | Store provenance and generic relations | Store persists identity links; Forge retains sufficiency and assurance meaning |

## Blocking requirements from the real workload

1. **Large-object path.** Store Phase 2 currently admits general blobs only
   through 992 bytes. Forge explicitly permits immutable records up to 4 MB,
   and some evidence payloads are larger than a single bounded object. A
   migration must add or select a streaming/multi-chunk typed-object path; it
   must not truncate or hide a Forge record inside an unbounded JSON field.
2. **Publication boundary.** Forge's correctness boundary covers one record,
   one complete ledger successor, the expected sequence/predecessor, and
   recovery metadata. Store's native CAS decision is useful, but the current
   embedded proof driver does not yet expose equivalent multi-process writer
   exclusion, durable compare/transition, or a consumer-facing recovery
   result. This is a Store/runtime requirement, not a Forge lock to preserve
   indefinitely.
3. **Reusable local adapter.** Store's object/generation implementation is
   currently exercised through its repository test driver. Forge needs a
   supported embedded consumer boundary that reuses the native Store modules
   without importing Store test internals or starting a mandatory daemon.
4. **Identity binding.** Forge's candidate, evidence, and record identities
   carry domain meaning and historical compatibility. Store's 12-byte logical
   identity and 32-byte content/root identities are separate domains. A
   versioned binding or relation is required before Store object IDs can be
   used for lookup without changing Forge semantics.

## Safe migration sequence

```text
Forge LocalRecordStore (canonical)
        │
        ├── differential export/import through mncs-ingest
        │
        ▼
Store shadow object + generation/feed + provenance/relations
        │  parity: bytes, identity binding, ordering, recovery, reopen
        ▼
Store-backed Forge adapter (canonical)
        │
        ▼
LocalRecordStore retained as migration oracle/reference fixture
```

The legacy record store must not be deleted before the differential proves:

- candidate/evidence records survive close and reopen with the same Forge
  semantic result;
- duplicate and conflicting identities have the same outcomes;
- every modeled crash point converges to the same old-or-new state;
- stale writers cannot publish over a newer generation;
- legacy fixtures remain readable through an explicit migration/reference
  path; and
- the derived index can be deleted and rebuilt from Store.

Until those checks exist, Forge must not declare `mncs-store` canonical and
must not add a second private “Store-compatible” ledger. The next highest
value Store work is therefore the generic large-object and concurrent
publication contract driven by this audit.
