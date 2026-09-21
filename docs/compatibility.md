# Compatibility

MNCS Forge `0.1.0a2` targets Python 3.11–3.13, official Python MCP SDK `>=1.29,<2`, local stdio
MCP, Codex CLI MCP registration, MNCS Provider Protocol 0.1, MNCS validator public commands, and
MNCDS validator public commands.

Provider-neutral discovery was tested at Forge implementation commit
`7e0599a080a36d3415780ba9e9ff617a7d012fbd` against MNCS tooling commit
`202c13fad2ca613a3d4ad9340d384b2a079beadf` with Codex CLI `0.144.6`. The development stdio
server currently exposes the registry's development MCP tools; an evaluator-mode server exposes
those tools plus `mncs_forge_final_evaluation_run`. The CLI exposes every registered operation,
including `receipts list` and `receipts get`, and `mncs-forge operations` remains the inventory.
Final evaluation remains evaluator-only.

Compatibility is non-normative. Core MNCS validation never launches Forge or a provider, and
MNCS CI does not depend on mutable Forge `main`.

The micro-verifier configuration is optional. Version-1 project files without `[[verifiers]]` or
`[verifier_limits]` continue to validate. The implementation uses Provider Protocol 0.1 extension
objects and adds no runtime dependency on a specific analysis provider.

Task 2 adds current persisted-record schema version `"1"` while retaining the historical
unversioned `0.1` state format as `"0.1-unversioned"` during normalization. The committed
pre-Task-2 fixture ledger is verified with its historical bytes and hash projection before payload
migration. Historical files are not rewritten. Unsupported explicit future record versions fail
with `UNSUPPORTED_RECORD_VERSION`, and trusted-context type mismatches fail with
`RECORD_TYPE_MISMATCH`. Public persistent record objects may now contain `record_type` and
`schema_version`; operation and Provider Protocol argument shapes are otherwise unchanged.

Task 3 adds the read-only `state` CLI command, `mncs_forge_state_inspect` MCP tool, lifecycle MCP
resource, and an embedded `project_inspect.lifecycle` summary. Existing records and the immutable
legacy corpus are not rewritten. Prospective transitions are stricter: successor epochs and
candidates require current parents, dispositions are terminal, selection requires the policy's
declared candidate evidence, and freeze/evaluator entry requires coherent current selection.

The Store cutover replaces the former ledger-backed typed-record transaction with the supported
`mncs_store.EmbeddedStore` boundary. Store stages bounded immutable content and structural
metadata, compares the expected generation, publishes a durable new generation, and recovers from
actual durable evidence. Forge retains typed record and workflow semantics; its `LedgerEntry`
view and `ledger-index.json` are derived projections. Historical unversioned `0.1` bytes are
verified before migration and are not rewritten. A stranded durable verifier action receives one
bound terminal `UNKNOWN` rather than an invented provider result.

Task 5 decomposes the control plane behind typed application services and ports while retaining
the public `Forge` facade and existing interface behavior. One composition root shares the ledger,
transactional store, lifecycle context, local observer, and bounded command executor; services do
not receive the facade or depend on CLI/MCP adapters. The current `CommandExecutor` is dependency
inversion over bounded local process execution, not the future runner/assurance architecture.

Task 6 makes `mncs_forge.operations` the canonical typed operation inventory. CLI parser leaves and
FastMCP tools resolve the same registry definitions and pre-handler mode/exposure gate. The
deterministic version-1 inventory is available from `mncs-forge operations` and
`mncs-forge://operations`. Existing CLI command names, MCP tool names, input/output schemas, and
mode behavior are compatibility-tested. Intentional asymmetries remain: local diagnostics are
CLI-only, the inventory is a CLI operation and MCP resource, and final evaluation is evaluator-
only in MCP.

The final `0.1.0b1` compatibility review freezes the semantic configuration/record, CLI, MCP,
operation, facade, and packaging boundary in a deterministic aggregate snapshot without freezing
presentation prose or internal layout. It also supports early unversioned `0.1` workflow-like
records that predate `subject_type`: they receive the historically accurate candidate-only default
in memory while retaining their original identity and status. Missing subject metadata never
infers project-scoped authority. Malformed TOML fails with `CONFIG_INVALID`; unreadable
configuration fails with `CONFIG_READ`. See the
[`0.1.0b1` compatibility boundary](compatibility-boundary-0.1.0b1.md).
