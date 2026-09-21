# Historical persistent record inventory and current Store projection

The table below describes the historical unversioned `0.1.0a2` representation at merged PR #7
(`dbf8d652c531996b24e632f53698b84b2a58fc30`). It was completed before the Task 2
writers changed. Names in the first two columns are historical storage names; the stable
Task 2 vocabulary is deliberate and does not infer record types from payload fields.

Ordinary Forge persistence now uses `StoreBackedRecordStore` and the supported
`mncs_store.EmbeddedStore` boundary. Store objects carry the typed Forge payload, a Forge domain
schema/identity binding, a descriptor with provenance, and a Store generation. The ledger-shaped
`LedgerEntry` returned to application services is a derived compatibility projection. The
historical JSONL ledger and immutable-record files are read only by the explicit migration and
differential paths documented below.

| Historical ledger `kind` | Immutable group | Stable vocabulary | Writer | Readers | Identity class | Authority and freshness fields | Status and time | Public exposure / dynamic data |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `epoch` | `records/epochs` | `epoch` | `Forge.epoch_begin` | `_latest_payload`, `_record_by_id`, candidate and verifier lifecycle, inspect/resource | `epoch_id` is record-derived; `baseline_identity`, generator/evaluator, contract/objective, authority and partition identities are semantic/content identities; `parent_epoch` is lineage | authority and visible-partition identities govern candidate registration; no computed freshness field | `created_at`; no evidence status | CLI/MCP begin and active-epoch inspection; maps of authority/partition identities are bounded dynamic objects |
| `candidate` | `records/candidates` | `candidate` | `Forge.candidate_register` | `_candidate`, `_record_by_id`, workflows, verifiers, compare/disposition/freeze/evaluation, inspect/resource | `candidate_id` is project candidate-content identity, **not** a record hash; parent/supersession/source epoch and generator/config/objective identities retain separate meanings | source epoch, current file identities, parent and generator/config identities; current candidate content is checked for freshness | `registered_at`; no evidence status | CLI/MCP registration, inspection, compare; changed-file and identity maps are bounded dynamic objects |
| `provider_probe` | `records/provider-probes` | `provider_probe` | `Forge._record_provider_probe` from `provider_probe` | provider inventory/listing and capability blockers | `output_identity` is record-derived; provider declaration, reported provider, executable, and response identities are distinct | executable identity and reported/declaration identities determine probe freshness and capability usability | `status`, availability, `recorded_at`, duration; probe `PASS` is capability discovery only | CLI/MCP probe/list/blockers; provider descriptor and protocol extension-derived arrays are dynamic but non-authoritative unless explicitly interpreted |
| none | none | `workflow_action` | No `0.1.0a2` persistent writer; a Provider Protocol request is transient | request identity is linked from a workflow result | request ID/output is request identity, not a persisted action identity | request binds candidate/workflow/mode and limits | request construction uses current time; no record status | Not exposed as a record. Task 2 models the boundary without adding a new persistence event. |
| `result` | `records/results` | `workflow_result` | `Forge.development_checks_run` via `_execution_record` | `_result_records`, explain, compare, disposition, reconciliation | `output_identity` is record-derived; candidate/project subject, provider/evaluator, and protocol request identities are semantic/request identities | candidate subject, method/category/scope, environment key names, provider/evaluator identity | `status`, `recorded_at`, duration, return code | CLI/MCP check/explain/reconcile/inspect-derived responses; provider witnesses and identity objects are bounded dynamic JSON |
| `verifier_action` | `records/verifier-actions` | `verifier_action` | `MicroVerifierService.run` | verifier request/result lineage, ledger inspection | `action_id` is record-derived under the historical pre-request projection; request, candidate, parent, freeze, superseded output, verifier/provider/config/policy/environment/input identities remain separate | all named material identities and bounded inputs govern authority/freshness | `requested_at`; no result status | CLI/MCP run returns its linked result, not the action; source-region and input identity objects are bounded dynamic JSON |
| `verifier_result` | `records/verifier-results` | `verifier_result` | `MicroVerifierService.run` via `_execute` or terminal fallback | explain, freshness, supersession, iterative-overlap checks | `output_identity` is record-derived; action, verifier/provider/config/policy/environment/candidate/freeze/supersession/request/response identities are distinct | complete material identity set, dependency envelope, mode/disclosure/evidence class, explicit `independent_evaluation=false` | `status`, `recorded_at`, duration, return code; freshness is derived at read time | CLI/MCP run/batch/explain; witnesses, provider identity, operational error, input identities, and dependency envelope contain bounded dynamic JSON |
| `disposition` | `records/dispositions` | `candidate_disposition` (`selected` or `rejected`) | `Forge.candidate_disposition` | freeze selection check and ledger inspection | `disposition_id` is record-derived; candidate and selection-policy identities are semantic/content identities | selection rule/policy and aggregate evidence status control selection; rejection preserves history | `evidence_status`, disposition, `recorded_at` | CLI/MCP select/reject; no undeclared top-level extensions |
| `freeze` | `records/freezes` | `freeze` | `Forge.candidate_freeze` | evaluator workflows and evaluator verifiers | `freeze_id` is record-derived; candidate, contract, reference, evaluator, acceptance-policy, protected, plan, and selection-record identities are distinct | frozen identities, path sets, required evidence plan, and environment are checked before/during evaluation | `frozen_at`; no evidence status | CLI/MCP freeze and evaluator summaries; frozen path-set object is bounded configuration data |
| `evaluation` | `records/evaluations` | `final_evaluation` | `Forge.final_evaluation_run` via `_execution_record` | ledger inspection and returned evaluator summary | historical `output_identity` uses the workflow-result identity rule; candidate, evaluator/provider, and request identities stay distinct | freeze is checked around execution but the historical payload does not contain `freeze_id`; evaluator disclosure is applied before identity | `status`, `recorded_at`, duration, return code | Evaluator-only CLI/MCP operation; status-only workflow redacts witnesses before identity |
| none | none | `reconciliation` | `Forge.evidence_reconcile` returns a transient derived result | caller only | references workflow-result output identities; no reconciliation record identity in `0.1.0a2` | candidate filter, per-category records, conflicts and stale identities | derived aggregate `required_gate_aggregation`; no timestamp | CLI/MCP/resource output only. Task 2 models and versions this JSON boundary without adding persistence. |
| `bundle` | `records/bundles` | `bundle` | `Forge.bundle_build` via `_execution_record` | ledger inspection and returned bundle summary | historical `output_identity` uses the workflow-result identity rule | candidate/provider/environment/request identities and workflow category | `status`, `recorded_at`, duration, return code | CLI/MCP bundle result summary; persisted payload is the underlying workflow result, not the summary wrapper |
| ledger line | `ledger.jsonl` | `ledger_entry` | historical `Ledger.append` | explicit migration/differential reader only | `entry_hash` is a historical ledger-chain identity over the exact entry body; `previous_hash` is chain lineage | trusted `kind` supplies historical payload context after raw integrity succeeds | `timestamp`; sequence and hash linkage | authenticated before import; not current authority |

Candidate comparison, claim status/blockers, provider inventory, verifier discovery/matching,
failure explanations, and bundle/evaluation summary wrappers are derived interface objects rather
than persisted records. The historical implementation has no persisted general workflow action.

## Identity rules found in the historical writers

- Candidate identity is `forge-tree-sha256-v1` over declared candidate/generated content and must
  not be replaced during migration.
- Epoch, disposition, freeze, and verifier-action IDs use a type-specific prefix over a local
  canonical JSON projection.
- Provider probes, workflow/final-evaluation/bundle results, and verifier results use
  `forge-json-sha256-v1` over the historical payload without `output_identity`.
- The historical verifier action adds `protocol_request_identity` after calculating `action_id`.
  Its migration therefore preserves that exact historical projection instead of pretending the
  field participated in the old identity.
- Ledger hashes authenticate the raw entry body, including the raw historical payload. Migration
  may occur only after sequence, link, and entry-hash verification.

## Task 7B-2 current persistence

Task 7B-2 added two current-schema ledger kinds that do not exist in the historical `0.1`
fixtures:

- `workflow_action` in `records/workflow-actions`, identity `action_id`;
- `execution_receipt_binding` in `records/execution-receipt-bindings`, identity `binding_id`.

The binding stores Forge linkage and completeness. The optional `mncs_receipt` object is excluded
from the binding identity and is verified against `receipt_identity` on read. Binding `status`
cannot be `PASS`. Historical state remains readable without these records.

## Compiler experiment persistence

The compiler-evolution increment adds `compiler_experiment` in
`records/compiler-experiments`. Its `experiment_id` is record-derived from the exact
language-owned study and normalized observation; `recorded_at` is non-material for idempotent
retries. Required `null` assurance and conformance fields prevent the persistence envelope from
laundering pass-local observations into stronger claims.
