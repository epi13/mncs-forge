# MNCS Forge development roadmap

<!-- MNCS:generated:begin -->
## Evidence-bound roadmap

- **complete** — Declared ambient projection surfaces satisfy their contract (`mncs-forge:projection-conformance`)
<!-- MNCS:generated:end -->

This roadmap separates low-risk repository maintenance from architectural changes that require
focused implementation, compatibility work, and adversarial testing. It is directional rather
than normative and does not change the claim boundary of Forge or MNCS/MNCDS.

## Current baseline — `0.1.0a2`

The current reference implementation provides:

- local CLI and stdio MCP interfaces;
- project authority and path validation;
- declared workflows and Provider Protocol 0.1 providers;
- bounded machine-native micro-verifiers;
- epoch, candidate, action, result, freeze, and evaluation lineage;
- immutable local record files and a hash-linked JSONL ledger;
- development/evaluator mode separation; and
- Linux, macOS, and Windows CI across Python 3.11 through 3.13.

The baseline remains experimental. The local ledger detects mutation but does not create external
anchoring, protected custody, independent evaluation, witnessing, or governance approval.

## `0.1.0a3` — internal consolidation

Goal: remove hidden implementation behavior and establish stable internal data and lifecycle
boundaries without materially changing the public CLI, MCP, configuration, or Provider Protocol
surfaces.

Required work:

1. completed: replace import-time verifier service substitution with one explicit implementation;
2. completed: introduce versioned internal and persistent record models and deterministic legacy
   migrations;
3. completed: define and test explicit state-transition rules;
4. completed: add transactional record-plus-ledger writes and interrupted-write recovery; and
5. completed: retain compatibility with existing `0.1` configurations and state fixtures.

The core `0.1.0a3` P0 internal-consolidation requirements are complete. The package version remains
unchanged until the repository's release process advances it.

## `0.1.0b1` — modular control plane

Goal: split the central control-plane implementation behind stable interfaces.

Required work:

1. completed: separate domain rules, application services, execution, storage, configuration, and
   interfaces;
2. completed: retain a small public `Forge` facade for compatibility;
3. completed: create one typed operation registry shared by CLI and MCP dispatch;
4. completed: add semantic schema/interface compatibility snapshots and migration tests; and
5. completed: document extension boundaries for providers, verifiers, storage, execution, and
   operations.

Task 5 completed service and dependency decomposition. Task 6 provides the shared typed operation
registry, deterministic inventory, generated FastMCP tools, registry-bound argparse dispatch,
explicit asymmetries, and compatibility enforcement. The final compatibility review added one
semantic cross-surface snapshot, early-`0.1` migration coverage, stable configuration read/parse
errors, Provider Protocol request characterization, and installed-wheel upgrade verification. The
[`0.1.0b1` compatibility boundary](docs/compatibility-boundary-0.1.0b1.md) is complete.

Task 7 is complete through 7A (local runner), 7B-1 (observations and MNCS receipt adapter),
7B-2 (persistent identity-bound workflow receipts), and 7C (rootless Podman sandbox-capable
runner, ADR 0016). Verifier-action receipt wiring is also complete, so every material local
execution path — declared workflows and verifier providers — persists identity-bound receipts.
Remaining Task 7 follow-ups are optional Docker/SSH adapters; Forge Cell Linux isolation remains
ordered Cell work below.

The MNCS-native spine is now consumed at runtime for bounded lifecycle projection, transition
preflight, technical evidence reconciliation, and candidate evidence-readiness projection. It
projects epoch/candidate successors, evidence dominance, disposition, freeze, evaluation, lineage,
candidate freshness, per-category status counts/conflicts, per-requirement readiness classes,
aggregate technical/readiness status, and bundle preconditions through typed records. The host
still owns Forge record persistence and identity production, category/requirement labels,
authority/evidence envelopes, evaluator custody, and bundle materialization. Bundle preconditions
compare opaque candidate identities in the language kernel and preserve the development/evaluator
distinction without moving workflow execution or custody authority across the boundary.

The next native tranche is a broader bundle-materialization projection only if a future pressure
demonstrates a reusable language-owned contract; packaging, signing, custody, and certification
remain explicit host/validator boundaries.

Task 5 completed the service and dependency decomposition without changing public CLI/MCP
dispatch. Task 6's shared typed operation registry is the current `0.1.0b1` priority. Full runner
receipts, sandbox-capable adapters, and execution assurance remain Task 7.

## `0.2.0` — stable local Forge

Goal: make Forge a dependable local control plane before distributed execution is introduced.

Release criteria:

- no import-order implementation replacement;
- documented record schema and migration policy;
- explicit state machine and transition tests;
- transactional storage and recovery tests;
- replaceable runner and record-store interfaces;
- malformed-protocol, subprocess, ledger, and concurrency adversarial suites;
- wheel installation and upgrade tests;
- stable machine-readable CLI/MCP operation inventory; and
- a reviewed threat model covering the local trust boundary.

Task 9A is complete in the current `main` baseline: reusable Hypothesis lifecycle and Provider
Protocol properties, a compact ledger-corruption corpus, strengthened local-runner coverage, and
reproducible branch-coverage measurement are present. Task 9B package/release engineering is
complete in the current `main` baseline, subject to the supported matrix remaining green after the
Windows test-harness portability repair: built-wheel/sdist verification, clean-environment import
checks, supported historical-state checks through the installed wheel, package/dependency audits,
and non-normative benchmark capture/comparison are present. Task 9C is complete as this bounded
implementation-mapped local threat-model and release-gate review increment. The primary remaining
`0.2.0` gate item is an adversarial study of the new sandbox-capable runner path (Cell Task 5
scope applied to `PodmanRunner`); identity-bound receipts for workflows and verifier actions and
the rootless Podman runner itself are now present.

## `0.2.x` — execution assurance

Goal: keep "what the program produced" and "what the execution environment established"
architecturally separate and machine-checkable.

Implemented:

- versioned `execution_assurance` records over receipt bindings with a declared requestable
  property vocabulary (ADR 0017);
- fail-closed assessment: unmet or unobservable requested properties stay `UNKNOWN`, incomplete
  executions confirm nothing, runner-kind contradictions (for example containerization claimed by
  a local-process runner) are `FAIL`;
- append-only retention of conflicting assessments;
- read-only Forge Cell document validation and assurance assessment through
  `cell.documents.validate` and `cell.execution.assess`.

Remaining work: challenge-bound freshness for assessments (Cell Task 4), policy documents stored
as first-class records rather than inline identities, and wiring assessments into selection gates
so promotion policies can require specific established properties.

## `0.2.x` — execution and evidence adapters

Goal: strengthen the execution environment and evidence anchoring without overstating authority.

Adapter status:

- completed: local process runner (Task 7A);
- completed: rootless Podman runner with network, filesystem, and digest-bound containerization
  properties (Task 7C, ADR 0016);
- outstanding: optional Docker runner;
- outstanding: Forge Cell Linux isolation runner (Cell Task 2);
- outstanding: SSH or remote-host runner;
- outstanding: periodic ledger checkpoints;
- outstanding: detached checkpoint signatures; and
- outstanding: optional external witness receipts or independently held checkpoint heads.

The Forge Cell specification foundation now includes versioned policy, test-bundle, and execution-
record schemas; offline validation; a fail-closed assurance assessment; reference fixtures; and a
proposed ADR. It does not yet implement an OS sandbox. The implementation queue is maintained in
[docs/codex-forge-cell-next-steps.md](docs/codex-forge-cell-next-steps.md).

Every recorded result must distinguish local validity, execution assurance, external anchoring,
witnessing, protected custody, and independence. A signature or second machine controlled by the
same operator does not establish organizational independence.

## `0.2.x` — query-driven micro-debugging

Goal: let agents use compilers and analyzers as identity-bound query engines instead of depending
primarily on large report dumps.

The specification foundation defines diagnostic sessions, reusable snapshots, normalized events,
falsifiable hypotheses, bounded probes, and diagnostic interpretations that link to the existing
verifier action/result system. It also defines `micro`, `incremental`, and `full-scan` escalation
levels without changing verifier status or evidence authority.

Planned work:

- typed diagnostic records and cross-record validation;
- normalized compiler, test, runtime, verifier, analyzer, contract, and human events;
- identity-bound snapshot lifecycle and invalidation;
- a minimal Provider Protocol extension for snapshot-backed queries;
- agent-facing hypothesis and probe operations through the shared operation registry;
- safe identity-bound snapshot and result reuse;
- a Clang/LLVM pilot provider;
- latency, output-size, repair-localization, and escalation benchmarks; and
- stale-state, substitution, resource, and disclosure adversarial studies.

The architecture is documented in [docs/micro-debugging.md](docs/micro-debugging.md), with the
ordered implementation handoff in
[docs/codex-micro-debugging-next-steps.md](docs/codex-micro-debugging-next-steps.md). Runtime work
depends on the core typed-record, transaction, state-machine, modular-service, operation-registry,
and runner tasks.

## `0.2.x` — compiler evolution observations

Goal: make compiler experiments comparable and evidence-addressable without allowing Forge to
define language legality or conformance.

The observation-only consumer and persistent control-plane path for
`mncs:language:compilation-study-result:0.1` is implemented. It preserves language-owned compiler,
pipeline, host, target, stage, pass, and obligation identities; localizes the earliest observed IR
difference; records the exact language artifact through the versioned record store and ledger; and
returns no assurance or conformance verdict. Record/list/compare are exposed through the shared
CLI/MCP operation registry, with a read-only experiment resource.

Planned work:

- completed: identity-bound candidate validation — validation evidence is bound to the exact
  validated artifact identity, substitution fails closed or collapses to `UNKNOWN`, and copied
  observations cannot promote a different candidate;
- policy-driven compiler/pass/IR regression gates backed by separate verifier results;
- separate translation-validation verifier results and assurance policy;
- language feature/profile compatibility matrices keyed by language-owned identities;
- pass-order, optimization, and backend tournaments whose candidates cannot self-authorize
  (the bounded tournament is implemented; language-contract-backed gates remain open);
- benchmark observations kept separate from semantic assurance; and
- Fabric execution across Linux, Windows, and Raspberry Pi environments with distinct host, build,
  target, and run identities.

See [compiler evolution observations](docs/compiler-evolution.md) and
[ADR 0013](docs/adr/0013-language-owned-compiler-experiment-observations.md) plus
[ADR 0014](docs/adr/0014-persistent-compiler-experiment-records.md).

## `0.3.x` — intent-aware security verification

Goal: harden machine-native code without treating unfamiliar or non-orthodox implementation
structure as automatic failure, while ensuring declared intent can never waive a failed safety
invariant.

The proposed architecture separates suspicious-pattern routing, local and trust-boundary verifier
results, attack-path composition, freshness, and project workflow disposition. It introduces
identity-bound intentional-deviation and deviation-evaluation records rather than broad scanner
suppression or syntax whitelists.

Planned work:

- versioned records and policy for intentional deviations and their evaluations;
- deterministic routing from bounded suspicion witnesses to required verifier capabilities;
- explicit approval, expiration, scope, and revalidation rules;
- local-invariant, trust-boundary, and compiler-aware verifier pilots;
- compiler, target, IR/object, reference-semantics, and deployment identity binding where material;
- a bounded attack-path composition pilot that cannot rewrite underlying verifier results;
- adversarial tests for forged links, stale declarations, broad whitelists, incomplete dependency
  envelopes, copied exceptions, and attempted `FAIL` suppression;
- agentic repair studies comparing compile/test-only feedback with verifier-guided hardening; and
- recursive-learning controls that preserve complete evidence-backed patterns rather than copying
  unusual syntax outside its verified envelope.

The architecture is documented in
[docs/intent-aware-security-verification.md](docs/intent-aware-security-verification.md) and proposed
by [ADR 0007](docs/adr/0007-intent-aware-security-verification.md). Implementation depends on typed
records, transactional storage, the shared operation registry, micro-verifier capability matching,
query-driven diagnostics, and stable freshness semantics.

## `0.3.0` — Forge over Fabric

Goal: evaluate distributed executions without Forge becoming a second execution fabric.

`mncs-fabric` is the persistent heterogeneous execution substrate. It owns worker inventory,
`fleet.refresh`, detached jobs, capability declaration, availability windows, the work queue,
containment reporting, and bounded artifact transport. Forge must not duplicate those mechanics.

Forge-owned requirements remain:

- immutable job/subject/action identity;
- worker, runner, and environment identity binding;
- capability-drift detection at the evidence layer;
- retry/attempt lineage and duplicate-result reconciliation;
- evidence classification;
- reproduction semantics;
- same-operator versus independent execution; and
- challenge, freeze, and policy authority.

The MCP server remains the agent-facing control plane. It should not become the distributed
scheduler. The principle is: Fabric decides where and how an eligible job executes; Forge decides
what that execution proves. See [ADR 0011](docs/adr/0011-forge-fabric-execution-boundary.md).

## `0.3.x` — measured and externally held evaluation

Goal: add stronger assurance backends only after local runner and challenge-bound record behavior is
stable.

Planned work:

- signed, content-addressed test bundles and output manifests;
- fresh challenge-bound execution records and an offline verifier;
- an adversarial Forge Cell mutation, escape, exhaustion, and replay study;
- TPM-backed measured execution;
- one confidential-VM backend with protected-test key release; and
- separately administered evaluator and custody adapters.

These features may make silent local substitution detectable under a declared trust model. They do
not automatically create organizational independence, evaluator competence, governance approval,
or immunity from denial of service.

## Work queue

Implementation order, constraints, acceptance criteria, likely file boundaries, and validation
commands are maintained in [docs/codex-next-steps.md](docs/codex-next-steps.md). Forge Cell work is
expanded in [docs/codex-forge-cell-next-steps.md](docs/codex-forge-cell-next-steps.md), and
micro-debugging work is expanded in
[docs/codex-micro-debugging-next-steps.md](docs/codex-micro-debugging-next-steps.md). Architecture
decisions that affect those tasks are staged under [docs/adr/](docs/adr/).
