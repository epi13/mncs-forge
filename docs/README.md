# MNCS Forge documentation

This directory separates user setup, control-plane concepts, provider integration, security, and
development planning. The root README remains a short project entrypoint.

## Start here

- [Getting started](getting-started.md) — install Forge, validate a project, inspect authority, and
  register the local MCP server.
- [CLI and MCP interfaces](interfaces.md) — complete command, tool, resource, and prompt surface.
- [Canonical operation registry](operation-registry.md) — shared typed dispatch, metadata, and
  machine-readable interface inventory.
- [Configuration](configuration.md) — project paths, authority, workflows, providers, verifiers,
  policies, and limits.

## Concepts and authority

- [Architecture and trust boundaries](architecture.md)
- [Evidence and identity model](evidence-model.md)
- [Versioned record schemas and migration](record-schemas.md)
- [Lifecycle state machine and transition errors](lifecycle.md)
- [Transactional local storage and recovery](storage.md)
- [Security model and residual risks](security.md)
- [Stable-local Forge threat model](local-threat-model.md)
- [Intent-aware security verification](intent-aware-security-verification.md)
- [Forge Cell execution assurance](forge-cell.md)
- [Compatibility](compatibility.md)
- [`0.1.0b1` compatibility boundary](compatibility-boundary-0.1.0b1.md)

## Providers and integrations

- [Provider Protocol integration](provider-protocol.md)
- [Machine-native micro-verifiers](micro-verifiers.md)
- [Property-oriented verifier fleet](verifier-fleet.md) — polyglot verifier families, Haskell
  pilots, proof-strength separation, overlapping verification, and the Fabric placement boundary.
- [Query-driven micro-debugging](micro-debugging.md)
- [Compiler evolution observations](compiler-evolution.md) — language-owned compiler study
  persistence, IR comparison, pass records, and observation/assurance/conformance separation.
- [EdgeStream integration](edgestream.md)
- [Codex installation details](codex-cli.md)

## Development planning

- [Development roadmap](../ROADMAP.md) — release-level direction and sequencing.
- [Codex implementation queue](codex-next-steps.md) — ordered implementation tasks, constraints,
  acceptance criteria, and validation requirements for coding agents.
- [Forge Cell Codex queue](codex-forge-cell-next-steps.md) — ordered implementation tasks for Linux
  isolation, immutable test bundles, challenge-bound attestations, adversarial studies, TPM,
  confidential execution, and external custody.
- [Micro-debugging Codex queue](codex-micro-debugging-next-steps.md) — ordered implementation
  tasks for diagnostic records, snapshots, provider queries, a Clang/LLVM pilot, benchmarks, and
  adversarial validation.
- [Architecture decision records](adr/README.md) — accepted decisions for implemented boundaries
  and proposed decisions for future work.
- [Language-pressure records](development-pressure/README.md) — Forge's local inventory of the
  open `mncs-language` frontier with reproducers and acceptance tests.
- [Task 3 validation evidence](development-evidence/task-3-validation.md) — baseline, focused/full
  test results, benchmark comparison, graph-analysis limits, and the deferred storage seam.
- [Task 4 validation evidence](development-evidence/task-4-validation.md) — transaction fault
  matrix, recovery/concurrency validation, benchmark comparison, and storage-flow review.
- [Task 5 validation evidence](development-evidence/task-5-validation.md) — application-service
  boundaries, facade compatibility, dependency enforcement, and structural review.
- [Task 6 dispatch inventory](development-evidence/task-6-dispatch-inventory.md) — pre-refactor
  CLI/MCP compatibility mapping and intentional asymmetries.
- [Task 6 validation evidence](development-evidence/task-6-validation.md) — registry design,
  compatibility enforcement, benchmark comparison, and dispatch review.
- [`0.1.0b1` compatibility validation](development-evidence/0.1.0b1-compatibility-validation.md) —
  semantic boundary, migration/wheel matrix, benchmark, and comparative provider findings.
- [Task 7B-1 MNCS receipt-adapter evidence](development-evidence/task-7b1-mncs-receipt-adapter.md) —
  pinned contract, dogfood scope, raw observations, adapter boundary, and validation classification.
- [Task 7B-2 execution-receipt integration](development-evidence/task-7b2-execution-receipt-integration.md) —
  identity-bound persistence, incomplete observations, and the Forge/Fabric adapter seam.
- [Compiler evolution observation validation](development-evidence/compiler-evolution-observation-2026-08.md) —
  full quality gate and comparative provider evidence for the language-owned compiler consumer.
- [Compiler experiment persistence validation](development-evidence/compiler-experiment-persistence-2026-08.md) —
  record/ledger/operation validation and comparative provider evidence for durable experiments.
- [Store migration readiness](store-migration-readiness.md) — Forge's measured
  Store cutover, proof surface, and remaining reusable runtime pressure.

## Documentation rules

Documentation must preserve the same boundaries as the implementation:

- MNCS and MNCDS results remain separate;
- missing or unsupported evidence remains `UNKNOWN`;
- workflow completion alone is not normative conformance;
- local operation does not create independence, protected custody, or governance approval; and
- Forge is not an operating-system or network sandbox unless a runner explicitly provides and
  records those properties.
