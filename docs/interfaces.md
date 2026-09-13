# CLI and MCP interfaces

Run `mncs-forge --help` and `mncs-forge-mcp --help` for the installed interface.

MCP tools:

- `mncs_forge_project_inspect`
- `mncs_forge_state_inspect`
- `mncs_forge_claim_status`
- `mncs_forge_claim_blockers`
- `mncs_forge_providers_list`
- `mncs_forge_provider_probe`
- `mncs_forge_capability_blockers`
- `mncs_forge_verifier_list`
- `mncs_forge_verifier_describe`
- `mncs_forge_verifier_match`
- `mncs_forge_verifier_run`
- `mncs_forge_verifier_batch`
- `mncs_forge_verifier_explain`
- `mncs_forge_epoch_begin`
- `mncs_forge_candidate_register`
- `mncs_forge_development_checks_run`
- `mncs_forge_failure_explain`
- `mncs_forge_candidate_compare`
- `mncs_forge_candidate_select`
- `mncs_forge_candidate_reject`
- `mncs_forge_candidate_freeze`
- `mncs_forge_evidence_reconcile`
- `mncs_forge_bundle_build`
- `mncs_forge_execution_receipts_list`
- `mncs_forge_execution_receipts_get`
- `mncs_forge_execution_assurance_assess`
- `mncs_forge_execution_assurance_list`
- `mncs_forge_cell_document_validate`
- `mncs_forge_cell_execution_assess`

The development inventory contains the tools above. A separately started evaluator-mode
server additionally exposes `mncs_forge_final_evaluation_run`; the development registration
cannot use final evaluation as repair feedback.

Tool registration, names, input models, mode policy, mutation classification, authority metadata,
and typed facade handlers come from the canonical operation registry. Argparse retains its
hand-tuned presentation, but command leaves invoke those same definitions. Run
`mncs-forge operations` or read `mncs-forge://operations` for the deterministic machine-readable
inventory. See [Canonical Forge operation registry](operation-registry.md).

All structured statuses remain separate. A declared command exit of zero is `UNKNOWN` unless it
emits a recognized structured status; command completion alone is not evidence `PASS`.

Persistent record objects exposed through CLI or MCP include Forge-assigned `record_type` and
`schema_version` metadata. Operation names, arguments, authority requirements, and status meanings
are unchanged. Derived summary objects need not pretend to be persisted records.

CLI equivalents are `mncs-forge providers list`, `mncs-forge providers probe PROVIDER_ID`, and
`mncs-forge providers blockers [CAPABILITY ...]`. Operation-backed MCP resources currently
include:

- `mncs-forge://project/authority-map`
- `mncs-forge://state/active-epoch`
- `mncs-forge://state/lifecycle`
- `mncs-forge://state/active-candidate`
- `mncs-forge://evidence/latest-summary`
- `mncs-forge://claims/blockers`
- `mncs-forge://providers/configured`
- `mncs-forge://providers/capability-blockers`
- `mncs-forge://verifiers/declared`
- `mncs-forge://execution/assessments`
- `mncs-forge://operations`

They are read-model projections and invoke their registered operation through the same interface
gate. The static `mncs-forge://guide/usage` resource and five named prompts are presentation and
guidance, not canonical Forge operations. The prompt names are:

- `start_controlled_machine_native_epoch`
- `evaluate_and_compare_candidates`
- `explain_unknown_claim`
- `prepare_candidate_for_freeze`
- `review_failed_development_check`

The registry inventory is the authoritative source for operation metadata and interface exposure.

`mncs-forge state`, `mncs_forge_state_inspect`, and `mncs-forge://state/lifecycle` expose the same
derived lifecycle stage, identities, evidence readiness, legal next operations, stable blockers,
and historical limitations. `project_inspect` embeds that result under `lifecycle`.

MCP resources are explicit read-model projections and prompts are guidance rather than executable
operations. Resources backed by a canonical operation pass through the registry before projecting
fields. `project.doctor`, `config.validate`, and `ledger.verify` intentionally remain CLI-only local
diagnostics; operation inventory is a CLI operation and MCP resource rather than an MCP tool.
`project.doctor` includes the `native_execution` selection status (mode, selected binary, and
build timestamp) so toolchain staleness is visible in pre-flight diagnostics.

Micro-verifier CLI equivalents are:

```text
mncs-forge verifier list
mncs-forge verifier describe VERIFIER_ID
mncs-forge verifier match [structured filters]
mncs-forge verifier run VERIFIER_ID [bounded inputs]
mncs-forge verifier batch VERIFIER_ID... [bounded shared inputs]
mncs-forge verifier explain OUTPUT_IDENTITY
```

Matching is deterministic and never executes. Run and batch requests cannot supply commands.
See [Machine-native micro-verifiers](micro-verifiers.md) for request fields and status semantics.
