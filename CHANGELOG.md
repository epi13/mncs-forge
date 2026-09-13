# Changelog

## Unreleased

- Fix native lifecycle projection of recorded selections in evidence-free
  history slices. The kernel's history projection required PASS evidence for
  selection lineage, so evaluator-mode views (which deliberately exclude
  evidence kinds) projected `AmbiguousHistory` for coherent selected/frozen
  histories. Evidence sufficiency stays enforced where it belongs — the
  readiness kernel plus host selection/freeze authorization — while the
  lifecycle kernel now owns only parent-linkage coherence. Projected
  selection/freeze status additionally reports `PASS` only when PASS
  evidence is present in the slice. Covered by
  `test_native_lifecycle_projection_accepts_recorded_selection_without_evidence`
  and the evaluator verifier/state-machine suites.
- Accept post-disposition evidence observations in native history
  projection. Freeze re-runs required checks after selection, so a FAIL
  observed after disposition is revalidation input — not a lineage break —
  and surfaces as `EVIDENCE_FAILED` at freeze authorization instead of a
  misleading ambiguity limitation. Post-freeze and post-evaluation
  observations remain outside the lifecycle. Covered by
  `test_native_lifecycle_projection_accepts_post_selection_evidence` and
  `test_freeze_revalidates_selected_evidence`.
- Report native toolchain selection in `project.doctor` (`native_execution`
  with mode, selected command, binary path, and build timestamp) so a stale
  compiler is visible in pre-flight diagnostics.
- Consolidate Forge-local content identities onto the shared canonical JSON
  track (`serialization.canonical_bytes`): the hand-rolled encoders in
  concept evaluations and license evidence are retired (byte-identical for
  their value domains; non-finite floats now fail closed instead of hashing
  invalid JSON). RFC 8785 JCS remains the track for language-owned receipt
  envelopes and runner/host identities; see the `serialization` policy note.
- Select the most recently built MNCS toolchain binary instead of always
  preferring `target/release/mncs`. A stale release build predating the
  current checkout or library sources fails elaboration of newer
  standard-library modules, so newest-build wins with deterministic
  release-on-tie ordering; an explicit `MNCS_CLI` path still wins and the
  `cargo run -p mncs-cli` fallback remains last. Native status now also
  reports the selected `binary` path and `binary_modified_at` as
  observational provenance. Covered by `tests/test_toolchain_selection.py`;
  see `docs/development-pressure/forge-p-0001-toolchain-identity.md` for the
  remaining language-side identity gap.

- Add a typed `mncs.forge.bundle.v1` precondition projection. Native mode now
  checks request validity, opaque candidate identity correspondence, candidate
  freshness, and evaluator selection/freeze coherence before bundle workflow
  execution. Forge retains workflow execution, persistence, custody, and
  materialization; malformed or inconsistent native results fail closed.

- Move candidate evidence-readiness classification through the typed
  `mncs.forge.readiness.v1` kernel. Native mode now projects bounded
  per-requirement status counts, freshness/comparability classification, and
  aggregate readiness reasons over a 16-by-8 envelope while Forge retains
  record identities, policy disclosure, persistence, custody, and bundle
  materialization. The adapter consumes language-emitted ABI metadata and
  fails closed on stale, malformed, or mismatching contracts.

- Add a typed MNCS-native reconciliation kernel for Forge technical evidence.
  Native mode now classifies bounded per-category status counts, conflict flags,
  unsupported counts, and aggregate status over a 16-category/8-observation
  envelope. The host retains labels, record identity, disclosure, persistence,
  and authority; explicit `off` mode and unavailable runtimes retain the
  compatibility classifier. The language consumer fixture is now clearly
  isolated under `integration/forge/` in the sibling repository.

- Package the authoritative Forge MNCS modules in wheels and sdists and add an explicit native
  execution policy (`off`, `prefer`, `required`). The native lifecycle kernel now projects a
  bounded typed history of epochs, candidates, evidence, dispositions, freezes, and evaluations,
  including lineage and candidate freshness, into the Forge lifecycle view. Projection and
  preflight caches include full Forge/language/compiler/runtime content identities; required mode
  fails closed and project inspection exposes native selection status. A cross-repository native
  CI lane builds `mncs-language`, runs the native Forge tests, and verifies both distributions.

- Advance the MNCS-native Forge spine from an additive probe to a runtime lifecycle
  gate. The typed MNCS lifecycle kernel now preflights first-epoch creation,
  first-candidate registration, candidate disposition, and freeze before the
  compatibility record path commits. Structured finite/record results are decoded
  strictly, native execution failures fail closed, and pure preflight results are
  bounded in-process by source/CLI/input identity. Forge-specific history and
  identity authority remain explicit host boundaries.

- Add a rootless Podman sandbox-capable runner (Task 7C). Declared argv executes
  inside a container with `--network=none`, a read-only root filesystem, a
  read-only workspace mount, declared `rw,Z` writable mounts, dropped
  capabilities, and optional resource bounds. Availability probes fail closed
  (`RUNNER_UNAVAILABLE`) for missing binaries, non-rootless runtimes, missing
  images, unsupported versions, and non-POSIX hosts; image digests are resolved
  through `podman image inspect` and tags alone are never immutable identities.
  Selection is additive through the optional `[runner]` configuration section;
  the default remains `local-process`. See ADR 0016.

- Extend execution-receipt lineage to verifier actions. Verifier provider
  execution now flows through the runner observation path and persists an
  `execution_receipt_binding` with `action_kind="verifier_action"` in the same
  transaction as the terminal result. Incomplete terminations persist explicitly
  incomplete bindings without synthesized stream totals. Disclosed verifier
  results include a compact `execution_receipt` summary whose status stays
  `UNKNOWN`.

- Make execution assurance a first-class typed concept (ADR 0017). The new
  versioned `execution_assurance` record assesses one receipt binding's
  established properties against caller-declared requested properties from a
  fixed vocabulary. Unmet or unobservable properties remain `UNKNOWN`,
  incomplete executions cannot confirm any property, and isolation claims that
  contradict the declared runner kind are `FAIL` laundering attempts. A
  functional `PASS` never implies assurance `PASS`. Assessments are append-only;
  conflicting assessments are retained side by side. New operations:
  `execution.assurance.assess`, `execution.assurance.list`, plus read-only
  Forge Cell surfaces `cell.documents.validate` and `cell.execution.assess`
  and the `mncs-forge://execution/assessments` resource. Registry grows
  44 → 48 operations.

- Bind compiler-candidate validation evidence to artifact identities.
  Validation records now carry the exact `validated_artifact_identity`; callers
  can require `expected_artifact_identity` and fail closed on substitution.
  Freshness is computed by Forge from bound identities: validation carried by a
  candidate with a different artifact identity is `stale-artifact-mismatch`,
  its effective semantic status collapses to `UNKNOWN`, and it cannot promote
  in tournaments or selection. Copied `"PASS"` observations cannot authorize a
  different candidate.

- Persist bounded concept evaluations for Concept Experiments
  (`concept.evaluations.record` / `.list` / `.get`). Each record re-derives its
  `content_digest` and stable id from the stored evaluation material, keeps
  `generator_certified` pinned to `false`, and cannot claim assurance,
  conformance, or universal truth. Registry grows 41 → 44 operations.

- Accept and persist the language-owned `mncs:language:experiment-result:0.1` contract alongside
  the earlier compilation-study record. Forge projects backend, realization-request/plan, typed
  artifact, experiment-status, and validator observations for listing and comparison while
  retaining the exact language record. These are bounded observations, not Forge assurance,
  conformance, or compiler-legality verdicts.

- Add an isolated compiler-candidate search protocol. Forge can register, list,
  compare, attach independent PASS/FAIL/UNKNOWN validation, run a bounded
  tournament, and select only under an explicit protected-property policy.
  Candidate generation is not validity. A faster FAIL candidate loses. UNKNOWN
  does not promote when validation is required. Search records cannot claim
  assurance or conformance. Companion language work owns backend lowering and
  translation validation.

- Add Task 7B-2 identity-bound persistent execution-receipt integration. Declared workflow
  execution now persists `workflow_action` and `execution_receipt_binding` records that reference
  the experimental MNCS `mncs-execution-receipt` envelope without forking it. Incomplete
  timeout/output-limit executions persist explicit `UNKNOWN` bindings. Binding status cannot be
  evidence `PASS`. Add `receipts list` / `receipts get` operations and a Fabric execution-adapter
  seam that does not import or duplicate Fabric fleet mechanics.
- Add Task 7B-1 raw `LocalProcessRunner` observations and an adapter-ready seam for the
  experimental MNCS `mncs-execution-receipt` `0.1-experimental` contract. The adapter uses pinned
  RFC 8785 identities, bounded stream facts, explicit termination/enforcement mapping, and fixed
  non-claim fields without persisting receipts or asserting assurance. Persistent receipt authority,
  Podman, and sandbox work remain deferred.
- Repair the Task 9B Windows test-harness portability regressions without changing raw local-runner
  output semantics or shrinking the oversized Provider Protocol corpus. Add the Task 9C
  implementation-mapped stable-local threat model, executable evidence mapping, and explicit
  `0.2.0` release-gate review. Task 7 receipts/sandbox work and the full `0.2.0` gate remain
  deferred.
- Complete the Task 9A local-stability harness in the current baseline and begin Task 9B with
  built-wheel/source-distribution verification, clean-environment import-origin checks, historical
  state validation through the installed wheel, package/dependency audits, CI artifact checks, and
  non-normative benchmark capture/comparison. Task 9 and the `0.2.0` gate remain incomplete.
- Begin Task 7 with a typed `Runner` boundary and explicitly named `LocalProcessRunner`, while
  preserving the existing bounded no-shell subprocess semantics and compatibility aliases.
- Add deterministic local-runner capability inspection that distinguishes enforced execution
  bounds from unavailable sandbox, network, and filesystem isolation, plus adversarial execution
  and architecture-boundary coverage. Podman adapters and persistent execution receipts remain
  deferred.
- Close the `0.1.0b1` compatibility boundary with a regenerable semantic snapshot spanning record
  and configuration schemas, CLI arguments/defaults/bindings, MCP mode inventories/schemas/
  resources, canonical operations, the public `Forge` facade, and packaging entry points.
- Migrate early unversioned `0.1` workflow-like records that predate `subject_type` to the
  historically accurate candidate scope in memory without identity/status drift or project-
  authority inference; preserve raw-ledger-first verification and immutable historical bytes.
- Give malformed/unsupported and unreadable configuration stable `CONFIG_INVALID` and
  `CONFIG_READ` errors, and add Provider Protocol 0.1 request-shape, adversarial migration, and
  installed-wheel upgrade coverage.

- Add one typed operation registry for canonical IDs, frozen input models, output contracts,
  mode/mutation policy, authority/lifecycle requirements, disclosure, CLI mappings, MCP tool
  visibility, resources, and explicit interface exclusions.
- Route every argparse command leaf and generated FastMCP tool through one fail-closed invocation
  gate while preserving existing public command names, arguments, tool names, schemas, results,
  errors, and evaluator-only final-evaluation visibility.
- Add `mncs-forge operations` and `mncs-forge://operations` deterministic machine inventory,
  semantic compatibility snapshots, mode/asymmetry tests, and architecture checks preventing
  independent interface dispatch or concrete storage/execution/lifecycle behavior in the registry.
- Split the monolithic control plane into explicit project, provider, candidate, development,
  evaluation, evidence, recovery, and singular micro-verifier application services while retaining
  `Forge` as the stable CLI/MCP compatibility and composition facade.
- Add typed record-read, record-commit, command-execution, project-observation, and verifier-catalog
  ports with one local adapter composition; application services no longer receive `Forge`, invoke
  `run_bounded`, construct `LocalRecordStore`, or calculate filesystem identities directly.
- Add facade characterization, direct service, import-boundary, cycle, storage-bypass, and
  execution-boundary tests; CLI/MCP command, tool, resource, argument, result, and error behavior
  remains unchanged.
- Add a typed `RecordStore` boundary that commits each immutable record and ledger entry through
  one exclusive, journaled local transaction with deterministic, idempotent startup recovery.
- Add durable staging, atomic publication, supported file/directory synchronization, expected-head
  binding, serialized thread/process writers, and rebuildable local ledger indexing.
- Extend ledger verification to detect missing, replaced, malformed, or payload-mismatched
  immutable companions while preserving raw legacy hash verification before migration.
- Recover durable verifier actions without terminal output as exactly one bound, non-sensitive
  `UNKNOWN` result; recovery never invents provider PASS or FAIL evidence.
- Add one typed append-only-history state machine for epoch/candidate lineage, required-evidence
  readiness, terminal disposition, freeze/evaluator coherence, bundle state, and verifier action
  terminality.
- Add deterministic lifecycle inspection through the Forge facade, `mncs-forge state`, the
  `mncs_forge_state_inspect` MCP tool, and the `mncs-forge://state/lifecycle` resource, with stable
  blocker codes and CLI/MCP parity.
- Prevent conflicting candidate dispositions, arbitrary epoch/candidate ancestry, historical-only
  freeze selection, incomplete/project-scoped selection evidence, incoherent evaluator entry, and
  a second terminal verifier result for one action.
- Add frozen, typed version-1 models for current Forge records and ledger entries while retaining
  JSON-compatible filesystem, ledger, Provider Protocol, CLI, and MCP boundaries.
- Add deterministic trusted-context migration for immutable unversioned `0.1` state, verifying raw
  historical ledger linkage before normalization and preserving historical identities/statuses.
- Add explicit metadata-bound current identity projections, fail-closed future-version and
  record-context mismatch errors, documented extension policy, immutable legacy fixtures, and a
  Draft 2020-12 public record-schema snapshot.
- Consolidate the hardened micro-verifier lifecycle into the authoritative
  `MicroVerifierService` and remove package-import service replacement.
- Preserve deletion-aware changed-path identities, terminal `UNKNOWN` results, evaluator
  redaction-before-identity, and heterogeneous partial batch behavior under both package import
  orders.
- Fail closed with a recorded, non-sensitive terminal `UNKNOWN` when an unexpected execution
  exception occurs after a verifier action has been recorded.
- Streamline the repository entrypoint and add a documentation map and consolidated getting-started
  guide.
- Add a release-level development roadmap and an ordered Codex implementation queue with task
  dependencies, invariants, acceptance criteria, validation commands, and explicit exclusions.
- Add architecture decision records for control-plane composition, versioned persistent records,
  replaceable execution runners, ledger checkpoint anchoring, and Forge Cell assurance.
- Expand contributor guidance and add evidence-aware pull request and roadmap-task templates.
- Add versioned Forge Cell policy, test-bundle, and execution-record schemas as packaged resources.
- Add offline Forge Cell document validation and fail-closed assurance assessment that keeps test
  results separate from execution assurance.
- Add reference Forge Cell artifacts and tests covering missing isolation, identity substitution,
  challenge replay, contradictory assurance claims, and malformed evidence.
- Add a dedicated Codex queue for Linux isolation, immutable test bundles, challenge-bound
  attestation, adversarial studies, TPM, confidential execution, and external custody.
- Define query-driven micro-debugging architecture, status separation, identity/invalidation
  rules, and a provider-neutral escalation model over the existing verifier evidence system.
- Add a six-record micro-debugging vocabulary, proposed ADR, and ordered implementation queue for
  sessions, snapshots, queries, a Clang/LLVM pilot, benchmarks, and adversarial validation.

## 0.1.0a2

- Add the first machine-native micro-verifier foundation: declared capabilities over existing
  providers/workflows, deterministic matching, bounded single/batch execution, immutable
  action/result lineage, freshness envelopes, CLI/MCP surfaces, examples, and security tests.
- Add provider-neutral configuration for identity/version, argv transport, declared
  capabilities, required/optional status, constructs, limitations, executable identity,
  environment, and last capability probe.
- Add CLI and MCP provider list, explicit bounded probe, and capability-blocker operations.
- Fail closed to UNKNOWN for unavailable, malformed, timed-out, unsupported, or
  identity-drifted providers; do not infer PASS from exit zero alone.
- Add project-scoped development workflows that do not require candidate ledger state.
- Remove final evaluation from the development MCP inventory and demote Joern to an
  explicitly configured optional legacy provider.

## 0.1.0a1

- Initial experimental CLI and stdio MCP server.
- Declared authority, epoch, candidate, evidence, comparison, freeze, evaluation, reconciliation,
  and package workflows.
- Provider Protocol 0.1 integration, EdgeStream example, Codex installer, security boundaries,
  tests, and CI.
