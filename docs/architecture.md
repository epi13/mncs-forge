# Architecture and trust boundaries

Forge controls an agent-facing workflow; it does not decide normative conformance. It separates:

1. Codex interaction over local stdio MCP;
2. deterministic analyzer interaction over MNCS Provider Protocol 0.1;
3. replaceable declared compiler, analyzer, test, mutation, sanitizer, benchmark, and harness
   commands; and
4. public offline MNCS and MNCDS validators.

Forge is orchestration, not analysis. Provider discovery records configured identity,
version, argv/transport, capabilities, required/optional status, availability, constructs,
limitations, executable identity, and the last explicit probe. A recognized capabilities
response can satisfy discovery policy; it is not structural-analysis or conformance PASS.
Missing capability remains UNKNOWN.

## MNCS-native migration boundary

The repository contains an incremental MNCS-native Forge source spine as
packaged data under `src/mncs_forge/resources/native/forge/`. It owns the first
bounded identity, canonical-material, typed-record, lifecycle, technical
evidence-reconciliation, and evidence-readiness seams, while
`src/mncs_forge/` remains the Python compatibility shell. `NativeForgeAdapter` invokes the language-owned CLI through
Forge’s existing bounded runner and returns its structured observation. Host-side
SHA-256 is explicit and limited to material declared by the MNCS module; no
cryptographic authority is implied by the source helper. The migration is
deliberately incremental: native availability, backend support, and response validity
are all independently observable. Covered lifecycle mutations are gated by the
typed native preflight, and bounded epoch/candidate/evidence/disposition/freeze/
evaluation projection is decoded into the host view. Missing or unsupported
capabilities retain the explicit compatibility path; malformed native results
remain fail-closed `UNKNOWN` observations rather than being treated as success.
Reconciliation follows the same rule: the native kernel classifies status,
counts, and category conflicts over a fixed 16-by-8 envelope; Forge retains
labels, record identity, disclosure, persistence, and authority policy.
Readiness follows the same rule over a fixed 16-requirement by 8-observation
envelope: MNCS owns bounded status/classification projection, while Forge
retains labels, records, policy errors, and bundle materialization. Bundle
authorization is a separate opaque-identity precondition projection; the
language compares requested/current candidate identities and evaluates the
development/evaluator freeze and selection envelope, while Forge retains
workflow execution, custody, and file writes.

## Control-plane composition

`Forge` is the stable compatibility and composition facade used by both existing interfaces. It
constructs one Store-backed record boundary, lifecycle context, project observer, and bounded
command executor, then delegates public behavior to cohesive application services:

```text
CLI / MCP
    -> typed operation registry and common invocation gate
    -> Forge compatibility facade
    -> project | provider | candidate | workflow | evaluation | evidence | recovery services
    -> typed records and ForgeStateMachine
    -> RecordReader | RecordCommitter | Runner | ProjectObserver ports
    -> Store-backed record boundary / process / filesystem adapters
```

The incremental package layout keeps stable domain and storage modules such as `records.py`,
`state_machine.py`, `record_store.py`, and the explicit legacy `ledger.py` migration reader in
their established locations. Application
services live under `application/`; inward-facing protocols live in `ports.py`; local execution and
filesystem observation implementations live in `adapters.py`. This avoids compatibility churn
while making dependency direction enforceable.

Application services never receive the `Forge` facade and do not import CLI, MCP, argparse,
`LocalRecordStore`, or the local subprocess function. `MicroVerifierService` remains the one
authoritative verifier lifecycle and receives the same shared ports as other services. CLI and MCP
normalize their existing presentation into frozen operation input models and invoke one validated
registry definition. The registry enforces interface exposure and allowed Forge mode before its
typed facade handler runs; it describes lifecycle and authority requirements without implementing
transition policy. FastMCP tools are generated from registry metadata, while argparse layout stays
hand tuned and registry-bound. Operation-backed MCP resources use the same gate; static resources
and prompts remain presentation. See [Canonical Forge operation registry](operation-registry.md).

## Extension boundaries

Forge extensions attach at explicit inward-facing boundaries:

| Extension | Current boundary | What it does not establish |
| --- | --- | --- |
| Provider | declared Provider Protocol workflow and capability probe | analyzer authority, conformance, or independence |
| Micro-verifier | typed verifier declaration over a declared provider method | a whole-program proof, result cache, or normative validator |
| Application service | focused service with typed ports and shared composition-root dependencies | a replacement lifecycle policy or interface adapter |
| Storage | `RecordReader`/`RecordCommitter` ports implemented by `StoreBackedRecordStore` over `mncs_store.EmbeddedStore` | external anchoring, custody, witnessing, or remote storage |
| Execution | typed `Runner` port, `LocalProcessRunner`, identity-bound receipt bindings, and Fabric adapter seam | sandbox assurance, containers, Fabric scheduling, or attestation |
| Public operation | frozen definition in `operations.py` with CLI/MCP/resource metadata | lifecycle authorization, which remains in `ForgeStateMachine` |

The operation registry is the public dispatch extension point; application services are the
behavior extension point; ports are the adapter substitution points. New providers or verifiers
must remain declared and capability-bound. Future Task 7 adapters may join the runner boundary
only after they record the properties needed for any stronger assurance claim. See
[Provider Protocol integration](provider-protocol.md), [Machine-native micro-verifiers](micro-verifiers.md),
[Transactional local storage](storage.md), and [Canonical Forge operation registry](operation-registry.md)
for the detailed contracts.

`Runner` is dependency inversion over bounded execution. The `LocalProcessRunner` adapter preserves
argument-array, no-shell, explicit cwd/environment, stdin, output, timeout, return-code, and
platform-specific termination behavior. `Runner.run()` returns one `ExecutionSession` with retained
bytes and raw observation facts. Capability inspection reports local process facts and enforced
bounds while marking sandbox, network, and filesystem isolation as `not-provided`.

Task 7B-2 persists an identity-bound `execution_receipt_binding` after declared workflow
execution. The binding links Forge action, project, epoch, candidate, result, runner, and
environment identities to an optional upstream MNCS `mncs-execution-receipt` envelope. Incomplete
observations remain explicit `UNKNOWN` and cannot become evidence `PASS`. The envelope is a
referenced companion, not a forked Forge schema. A Fabric-backed runner is supported only as a
translation adapter over the same observation boundary; Forge does not import Fabric or own fleet
scheduling. See [ADR 0011](adr/0011-forge-fabric-execution-boundary.md). `CommandExecutor` and
`LocalCommandExecutor` remain compatibility aliases. Podman, Docker, and stronger isolation
remain later Task 7 work.

Development mode can see declared contracts, references, and development evidence, register
candidates, run declared development workflows, compare candidates under the configured policy,
and write only candidate/generated/output/Forge-state paths. Evaluator mode requires frozen
candidate, evaluator, policy, contract, reference, protected, environment, and evidence-plan
identities. It checks identity drift before and after each run and withholds repair details under
status-only disclosure.

The Forge state directory is `.mncs-forge/`. Epoch, candidate, action, result, selection,
rejection, freeze, evaluation, and bundle records are typed Store objects projected into the
Forge reader shape. Versioned frozen models form the internal domain boundary; filesystem, Store,
CLI, MCP, and Provider Protocol boundaries remain JSON-compatible. Supersession and lineage are
explicit. Historical JSONL is an import/migration boundary only. See [Versioned Forge records](record-schemas.md)
for schema, identity, and legacy migration rules.

Authorized persistent transitions pass to `RecordStore` only after state-machine approval and
typed record construction. The Store adapter stages immutable content and a generation successor,
binds publication to the expected generation, and publishes the head under one process-shared
lock. Startup recovery resolves durable Store evidence before lifecycle projection. A local derived index
is rebuildable acceleration data derived from the current Store generation; Store objects and
their bindings remain authoritative. See
[Transactional local storage](storage.md).

`ForgeStateMachine` derives active epoch, candidate lineage/freshness, required-evidence readiness,
terminal disposition, freeze/evaluation/bundle state, and verifier action terminality from one
typed Store projection. It authorizes transitions but does not execute providers or write records.
There is no mutable current-state file. See [Forge lifecycle state machine](lifecycle.md).

Project-scoped development workflows may run without candidate ledger state. Their subject
is the declared project identity, and their PASS is limited to the development workflow.
Candidate-scoped evidence keeps its candidate and epoch binding. Final evaluation is
registered only by a separate evaluator-mode MCP process.

Micro-verifiers are capability declarations over the same Provider Protocol workflows, bounded
command executor, temporary workspace, freeze checks, immutable record store, and ledger. They do
not form a parallel execution or evidence system. Forge controls matching and invocation; the provider owns
the narrow verification method; offline MNCS/MNCDS validators retain normative result authority.

See [Machine-native micro-verifiers](micro-verifiers.md) for the bounded query flow and freshness
model.
