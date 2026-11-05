# Native bundle-completion lifecycle evidence

This record covers the Forge lifecycle migration from host-owned bundle-stage
classification to the MNCS lifecycle projection.

## Authority and source identity

- Campaign: `cmp_luna_max_native_stdlib_extension_20261104_2252z`
- Environment session: `ses_0e32b5672f510e45`
- Work Intent: `int_0a63337255aa2d99`
- Exact checkout claim: `claim:mncs-forge:worktree:2f183ff53ae7`
- Repository: `mncs-forge`, based on `ede1e1feb0643461f5cd4c32cbc5229177644570`
- Native semantic-input identity during verification:
  `72973074844eef0f807fb3caba3a2500801fae5b30238fa2b4062e326884ea03`

## Responsibility transferred

Forge previously recognized a bundle for the current candidate and selected
`bundle_complete` in its host lifecycle projection. The MNCS lifecycle module
now owns that classification. Its typed projection input includes bounded
`bundle_present` evidence; the native stage kernel applies lineage, epoch, and
candidate checks before bundle presence, and bundle presence before evaluation
completion. The existing `Stage` discriminants remain stable; `BundleComplete`
is appended as variant 10.

Forge still selects typed bundle records for the current candidate and supplies
only their presence to the kernel. Forge retains bundle authorization,
materialization, persistence, and custody. The host projection remains as the
compatibility path when native execution is unavailable.

## Native execution

Verification used the retained Language runtime at
`mncs-language/target/release/mncs` and `libmncs_embed.so`. The selected
composition was the existing Stage-0 reference/embed lane
(`mncs-research-bytecode`). The Forge Environment closure did not select the
Compiler checkout, so canonical Compiler/VM composition was not available in
this run. The native lifecycle request retained its 32-event input bound and
200,000-step execution budget; actual step consumption was not measured.

Language repository revision: `ce468548ead4c238f8658fa32e6e7f9bcfb2afd9`.
Stdlib repository revision: `db7b17ddc4e5bfa98bef1350a649abdddbfc01f9`.

## Verification

Passed:

- `test_native_lifecycle_projection_covers_lineage_freshness_and_terminality`
  — evaluation stays `EvaluationComplete` without a bundle; a bundle changes
  the native result to `BundleComplete`; an absent candidate still dominates
  bundle presence; malformed non-boolean input is rejected.
- `test_native_projection_owns_bundle_complete_for_persisted_current_bundle`
  — production `state_inspect` projects a persisted legacy history containing
  evaluation and current-candidate bundle records as `bundle_complete`, while
  ledger verification remains valid.
- `test_legacy_history_projects_without_rewriting_or_prospective_rejection`
  — the host compatibility path continues to accept and preserve the legacy
  history.
- Python compilation, `git diff --check`, and Ruff F-rule checks on the changed
  Python modules and tests.

Incomplete broader verification:

- The Environment-declared `forge-assurance-regressions` capability timed out
  at its 120-second limit. Its partial output contained two failure markers and
  no complete test report or artifact.
- A direct full-suite run was stopped after identifying
  `test_subprocess_implementation_is_confined_to_execution_modules` failing at
  the unchanged `compile_workspace.py` subprocess import. The same focused test
  fails on repository `main`. The unrelated lifecycle end-to-end test also
  stops before bundle creation on a process-provider `stdin` contract mismatch
  (1,024 versus 4,096 bytes).
- The targeted native and consumer checks above completed successfully; the
  complete Forge suite is not claimed as passing.
