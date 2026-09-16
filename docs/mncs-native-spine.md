# MNCS-native Forge spine

Forge now carries a packaged MNCS-native source spine alongside the established
Python compatibility surface. The source modules are an incremental authority
boundary for identity-shaped values, canonical candidate material, typed
candidate validation, lifecycle projection, bounded technical evidence
reconciliation, and evidence-readiness projection; the Python CLI and MCP server
remain the stable integration surface during the migration. Forge invokes the
lifecycle module for both bounded history projection and covered transition
preflight, and invokes a bundle-precondition module before bundle materialization.
History projection owns only parent-linkage coherence: a recorded selection
projects `CandidateSelected` even when the projected slice omits evidence
kinds (evaluator views), while selection authorization still requires PASS
evidence through the readiness kernel and host gates. Projected
selection/freeze status reports `PASS` only when PASS evidence is present in
the slice, so an evidence-free view can never upgrade into an assurance
claim.

| Module | Responsibility |
| --- | --- |
| `mncs.forge.identity.v1` | Host-supplied 32-byte digest values and equality/zero checks |
| `mncs.forge.serialization.v1` | Versioned candidate material layout and status codes |
| `mncs.forge.records.v1` | Candidate/validation records and fail-closed checks |
| `mncs.forge.lifecycle.v1` | Epoch/candidate lineage, evidence, disposition, freeze, evaluation, freshness, and stage projection |
| `mncs.forge.reconciliation.v1` | Bounded per-category status counts, conflict classification, unsupported-count accounting, and aggregate technical status |
| `mncs.forge.readiness.v1` | Bounded per-requirement status summaries, freshness/comparability classification, readiness reason, and aggregate counts |
| `mncs.forge.bundle.v1` | Deterministic development/evaluator bundle preconditions over opaque candidate identities and explicit freeze/selection/evidence observations |
| `mncs.forge.core.v1` | Public entrypoints used by the adapter and service drift fixture |

The authoritative Forge source files are package data under
`src/mncs_forge/resources/native/forge/`; installed wheels and sdists expose the
same files through `importlib.resources`. A release or CI lane may set
`MNCS_FORGE_NATIVE_MODE=off|prefer|required` (or configure `[native].mode`).
`off` disables selection, `prefer` selects the packaged runtime when available,
and `required` fails closed before startup when it is unavailable. Project
inspection exposes the selected/available/reason status.

## Toolchain binary selection

An explicit `MNCS_CLI` path always wins. Otherwise Forge selects the most
recently built prebuilt CLI (`target/release/mncs` or `target/debug/mncs`)
by modification time, with exact ties broken deterministically toward the
release build; only when neither binary is usable does Forge fall back to
`cargo run -p mncs-cli`. Release builds do not unconditionally win: a stale
release binary predating the current checkout or library sources fails
elaboration of newer standard-library modules, so newest-build wins. The
status surface additionally reports the selected `binary` path and its
`binary_modified_at` timestamp as observational provenance; semantic cache
keys already bind the binary content itself. See
`tests/test_toolchain_selection.py`.

The canonical candidate material is exactly 71 bytes in this tranche:

```text
kind:1 | schema:1 | status:1 | parent_digest:32 | source_digest:32 | changed_files:4
```

The MNCS source declares field order and widths. Forge’s host adapter materializes
the bytes and computes SHA-256 at the explicit host boundary; the MNCS module does
not pretend to provide cryptography. A missing language checkout, malformed native
response, timeout, or unsupported backend remains a bounded failure/`UNKNOWN`
observation and does not upgrade a Forge record into assurance or conformance.

## Local probe

With sibling checkouts at the workspace root:

```bash
MNCS_LANGUAGE_ROOT=/absolute/path/to/mncs-language \
python -c 'from pathlib import Path; from mncs_forge.mncs_native import NativeForgeAdapter; a=NativeForgeAdapter(Path("/absolute/path/to/mncs-forge")); print(a.execute(a.native_source, Path("/absolute/path/to/mncs-forge/examples/execution/native-status-probe.json")).payload)'
```

The adapter invokes `mncs-language` through Forge’s existing bounded, no-shell
runner. First-epoch creation, first-candidate registration, candidate disposition,
and candidate freeze are preflighted against the typed lifecycle kernel before
their legacy-compatible records are committed. Forge-specific record persistence,
identity production, evidence envelopes, evaluator custody, and bundle semantics
remain in the host boundary. A missing language checkout retains the explicit
compatibility path; an available but malformed, timed-out, or semantically
mismatching native result fails closed.

Evidence reconciliation now crosses the same typed boundary. Forge supplies
opaque category digests plus up to eight status observations per category and
receives typed category summaries and an aggregate status. Category labels,
record identities, unsupported-construct presentation, persistence, and
authority remain host concerns. The 16-category/8-observation limits are
explicit; native mode never truncates an oversized envelope or treats malformed
input as a compatibility success. Compatibility classification remains only
for explicit `off` mode or unavailable language runtimes.

Evidence readiness now crosses the boundary as a separate bounded projection.
Forge normalizes candidate-scoped records into up to 16 named requirements,
with at most eight status observations per requirement and explicit freshness,
environment, policy, comparability, and authority-match booleans. The MNCS
module owns the requirement classification precedence (`Missing`, `Failed`,
`Unknown`, `Stale`, or `NonComparable`) and the global reason (`PolicyInvalid`,
`NoCandidate`, or the first blocking class); Forge maps that result back to its
stable evidence view while retaining record identities and policy disclosure.
An observed record remains in the host-facing `present` envelope even when the
native class is `Failed`, `Unknown`, or stale, preserving the compatibility
state-machine semantics. Oversized or malformed readiness input fails closed.

Bundle authorization now crosses a separate typed precondition seam. The host
supplies the requested/current candidate identities plus explicit booleans for
candidate freshness, selection, freeze, freeze drift, request validity, and
evidence. The MNCS module compares the opaque identities itself and returns a
typed reason/status pair. Development mode requires a current candidate;
evaluator mode additionally requires a current freeze, selected candidate, and
current freeze bindings. Bundle materialization, custody, file writes, and any
certification or MNCS/MNCDS conformance claim remain host-owned.

The adapter consumes compiler-emitted ABI contracts dynamically: record,
finite, and digest identities come from `mncs abi`, while malformed or
identity-inconsistent results fail closed. Readiness reconstruction is keyed by
the returned opaque requirement identity rather than native array position, so
the public `EvidenceReadiness.to_dict()` surface remains permutation-invariant
and compatible with the legacy path.

The pure preflight/projection caches are keyed by the native contract, full
packaged Forge source content, language library/compiler/runtime content, exact
CLI command, and semantic inputs. They do not persist evidence or lifecycle
authority and never rely on source mtime alone. The projection input is a fixed
32-event typed array; histories outside that declared bound remain
`NATIVE_LIFECYCLE_UNKNOWN` rather than being truncated or guessed.
