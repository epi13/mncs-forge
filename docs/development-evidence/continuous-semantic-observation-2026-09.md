# Continuous semantic observation evidence

This record captures the controlled end-to-end demonstrations for the continuous
development supervisor. It is evidence for the implementation campaign, not a
claim that Forge is an independent evaluator or a whole-program proof engine.

## Resident service measurements

- A cold `mnls-language-service-host` reached its Unix socket in `0.041 s` on
  the controlled workspace.
- Two status requests through the same resident process each completed in about
  `0.003 s`; the second request reused the resident workspace rather than
  starting another semantic implementation.
- A transient `did_open` edit reached the semantic response in `0.031 s` and
  produced one bounded workspace event. This measures the current small fixture,
  not a promise of whole-workspace frontend incrementality.

The workspace was `/home/epi13/mncs-continuous-final-20260921`; the resident
process was the canonical Rust service host used by LSP, MCP, and Forge.

## Failure-driven verification demonstration

Two rapid edits to the controlled `tests/self_suite.mncs` fixture were submitted
through the resident service. The supervisor coalesced the obsolete same-document
work (`queued_jobs_cancelled = 1`) and processed the newest generation.

- RAVEL produced a complete minimum-sufficient plan with `1` selected identity
  out of `7` available identities.
- `mncs-test` executed exactly that one identity. It failed because the fixture
  expected `43` while the implementation returned `42`; unrelated tests were
  not rerun.
- The existing Forge failure loop passed the exact result and witness to
  `mncs-debug`. Debug reached sufficient minimal diagnosis using the existing
  execution, with no broad rerun.
- The supervisor ended with `FAIL = 1`, `PASS = 1`, `UNKNOWN = 0` and emitted a
  compact attention event for the active agent.
- The complete one-shot run took `263.00 s` wall time. This includes current
  Store-backed startup/recovery and bounded failure diagnosis; it is a current
  integration measurement, not the target latency.

## Safe migration demonstration

The controlled Index fixture was changed from `module mncs.index.scan;` to the
obsolete `module mncs.index.scan.v2;` declaration.

- Forge invoked only the `Applicability::Safe` Doctor path.
- Doctor applied `language.canonical-module-identities`, using migration rule
  `mncs-index.scan-v2-to-canonical`.
- The exact source identity was checked before promotion, the repair reached one
  bounded round, and the resident service observed the rebound generation.
- The resulting validation was `passed = true`, `idempotent = true`, with no
  post-repair errors. A second Doctor pass reported `already canonical`.
- The repair demonstration took `309.29 s` wall time, again dominated by the
  current Store-backed integration path.

The repair result retains original/resulting source identities, fix identities,
migration identities, repair rounds, validation, focused-verification result,
and conflict/failure reason. Review and Manual Doctor fixes are not eligible for
this automatic path.

## Evidence and scope

The direct compiler-impact → RAVEL → Test path also selected `1/7` identities
and passed the exact selected identity on the canonical fixture. The continuous
implementation reuses Forge's declared verifier identities and existing Store
evidence boundary. A verifier result is reusable only when provider, verifier,
candidate, source/subject, toolchain/configuration/policy/environment, contract
and profile identities match and the provider declared a complete dependency
envelope. Incomplete coverage reruns or remains `UNKNOWN`.

The live demonstration did not claim a micro-verifier cache-hit timing: the
existing large Store made a separate micro-only run dominated by recovery. The
reuse decision is implemented and identity-guarded; cache-hit performance needs
a separately instrumented Store benchmark. No live security verifier was
configured in this fixture, so security trigger dispatch remains an explicit
capability rather than an invented PASS.

During development we ran the focused Language Service workspace tests, focused
Forge tests and lint checks, Doctor tests, and the exact selected identities. We
did not repeatedly run the family-wide suites or a full Forge suite after each
edit. Comprehensive promotion checks remain an explicit final reconciliation
step.

## Remaining limiting mechanics

The ReferenceCompiler frontend is still synchronous and cannot be interrupted;
late results are generation-checked and discarded. The event cursor is bounded
and not restart-durable, so a reset requires status reconciliation. Store-backed
startup/recovery is currently much more expensive than resident semantic
requests. These are recorded as limitations rather than hidden behind a second
semantic implementation or a full-suite fallback.
