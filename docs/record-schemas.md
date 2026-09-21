# Versioned Forge records

Forge uses frozen typed record models inside the control plane and ordinary JSON objects only at
filesystem, ledger, Provider Protocol, CLI, and MCP boundaries. The current persisted schema
version is the string `"1"`. Strings avoid numeric-version ambiguity and are compared exactly.

Every newly persisted payload contains Forge-assigned `record_type` and `schema_version` fields.
Callers cannot supply those fields to a writer. Ledger entries are versioned records too, so their
current hash projection includes ledger metadata and the versioned payload.

Ordinary writes use `StoreBackedRecordStore` over the supported `mncs_store.EmbeddedStore` API.
The typed payload remains Forge-owned; Store owns the durable object, domain binding, content
identity, representation root, generation, publication, and recovery. `LedgerEntry` is a derived
Forge-facing projection for application compatibility, and `ledger.jsonl` is a legacy import
boundary rather than current canonical state.

## Stable vocabulary and historical contexts

| Trusted historical context | Current `record_type` |
| --- | --- |
| ledger `epoch`; `records/epochs` | `epoch` |
| ledger `candidate`; `records/candidates` | `candidate` |
| ledger `provider_probe`; `records/provider-probes` | `provider_probe` |
| ledger `workflow_action`; `records/workflow-actions` | `workflow_action` |
| ledger `execution_receipt_binding`; `records/execution-receipt-bindings` | `execution_receipt_binding` |
| ledger `result`; `records/results` | `workflow_result` |
| ledger `verifier_action`; `records/verifier-actions` | `verifier_action` |
| ledger `verifier_result`; `records/verifier-results` | `verifier_result` |
| ledger `disposition`; `records/dispositions` | `candidate_disposition` |
| ledger `freeze`; `records/freezes` | `freeze` |
| ledger `evaluation`; `records/evaluations` | `final_evaluation` |
| ledger `bundle`; `records/bundles` | `bundle` |
| ledger `compiler_experiment`; `records/compiler-experiments` | `compiler_experiment` |
| ledger line | `ledger_entry` |

The public typed vocabulary also includes `reconciliation`. In historical `0.1` Forge, workflow
actions were transient requests and reconciliation was a derived interface object. Task 7B-2 now
persists `workflow_action` and `execution_receipt_binding` as new current-schema records. Historical
fixtures remain readable without those events. Reconciliation remains a derived interface object.

Compiler experiments are current-schema observation records. They embed either an exact
language-owned compilation-study record or `mncs:language:experiment-result:0.1`, plus a bounded
Forge projection. The latter adds backend, realization-request/plan, typed artifact, experiment
status, and validator observations. Forge validates the pinned language contract and fixed
observation-only authority fields, but does not define the embedded compiler schema or infer
assurance/conformance from it.

## Legacy migration

Unversioned PR #7 records are the known historical schema `"0.1-unversioned"`. Forge determines
their type only from a trusted ledger kind, immutable-record group, or explicit expected type. It
does not infer authority from payload shape.

Legacy loading follows this order:

```text
bounded raw JSON line
  -> verify historical sequence, previous hash, and entry hash
  -> resolve trusted ledger-kind context
  -> migrate payload deterministically
  -> frozen typed record
```

No migration reads the clock, network, environment, provider state, or project contents. Historical
files and ledger lines are never rewritten. A normalized legacy model reports
`schema_version = "0.1-unversioned"` and preserves its historical candidate, record, request,
output, lineage, and ledger identities. Migration translates representation only; it never
reevaluates evidence or grants authority.

## Identity rules

Candidate IDs remain semantic candidate-content identities. They are not hashes of candidate
record JSON. Other subject, provider, policy, environment, request, response, parent,
supersession, freeze, and ledger identities retain their separate meanings.

For current self-identifying records, the identity projection is explicit: canonical Forge JSON
containing `record_type`, `schema_version`, and every identity-relevant persisted field except the
self-identity field. Verifier action identity additionally excludes `protocol_request_identity` to
avoid a request/action identity cycle; the request remains linked and authenticated by the final
record and ledger. New metadata therefore participates in every current record-derived identity.

Legacy identities use their exact historical projections. Forge never recalculates an old ID using
current metadata.

## Unknown fields and extensions

Current records reject unexpected top-level keys with `RECORD_UNKNOWN_FIELD`. Explicit
`extensions` objects round-trip and participate in current record-derived identity, but Forge does
not consult them for status, authority, freshness, independence, custody, witnessing,
certification, or governance semantics.

Unknown keys encountered in a legacy payload are preserved under
`extensions.legacy_unknown_fields`. They remain non-normative and do not change historical identity
or status. A historical extension already using that migration-reserved key fails with
`RECORD_EXTENSION_CONFLICT`; Forge does not overwrite either value. Ledger-entry extensions are
rejected because the historical chain envelope had no extension point.

Unsupported explicit versions fail closed with `UNSUPPORTED_RECORD_VERSION`. A type declared by a
record that disagrees with its trusted ledger kind or storage group fails with
`RECORD_TYPE_MISMATCH`.

Early `0.1` workflow results, final evaluations, and bundles created before project-scoped
workflows existed did not contain `subject_type`. Their migration adds
`subject_type = "candidate"` only after reproducing the historical identity without that field. A
normalized legacy record may reproduce the same candidate default on a later parse. Forge never
infers `project` from a candidate-identity string, and an explicit legacy project subject must
reproduce an identity that included the subject field.

## Schema snapshot

[`forge-records-1.schema.json`](../src/mncs_forge/resources/forge-records-1.schema.json) is the
committed JSON Schema Draft 2020-12 snapshot for every Task 2 record model. The snapshot is checked
as a schema and representative serialized models are validated against it. The explicit Python
parser remains authoritative for runtime behavior. Regenerate the snapshot deliberately with:

```bash
PATH="$PWD/.venv/bin:$PATH" python scripts/generate-record-schema.py
```

The aggregate release-boundary check is:

```bash
PATH="$PWD/.venv/bin:$PATH" python scripts/generate-compatibility-snapshot.py --check
```
