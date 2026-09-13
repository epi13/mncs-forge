# FORGE-P-0001 — toolchain build identity for stale-binary detection

- Status: open.
- Severity: medium. A stale prebuilt compiler is silently selected, then every
  native projection fails; nothing is mis-recorded, but local `prefer`/`required`
  lanes go red with a misleading `NATIVE_ABI_UNKNOWN` until the operator finds
  the old binary.
- Forge use case: `NativeForgeAdapter` must choose between
  `target/release/mncs` and `target/debug/mncs` in a sibling `mncs-language`
  checkout. `mncs --version` reports only `mncs 0.1.0` for both, so Forge
  cannot tell which build matches the checkout.

## Reproducer

With a release binary older than the checkout's library sources:

```bash
MNCS_LIBRARY_PATH="<lang>/library:<forge>/src/mncs_forge/resources/native/forge" \
  <lang>/target/release/mncs abi <forge>/src/mncs_forge/resources/native/forge/core.mncs
```

exits nonzero with elaboration error `MNE172` (`mncs.std.encoding` fails to
parse under the old compiler) while the same invocation through a freshly
built `target/debug/mncs` exits zero with the expected ABI document. Observed
2026-09-12 with a 2026-09-06 release binary against a 2026-09-12 checkout.

## Current workaround

Forge selects the most recently built binary by modification time
(`tests/test_toolchain_selection.py`) and reports the selected `binary` path
plus `binary_modified_at` in the native status surface. An explicit `MNCS_CLI`
path still wins.

## Why the workaround is deficient

Modification time is a heuristic, not an identity: a binary rebuilt from a
different worktree, a touched-but-identical binary, or a clock skew can all
defeat it. Content hashing (already in Forge's cache keys) detects *change*
but cannot answer "was this built from this checkout at this revision?".

## Desired semantics

A cheap, machine-readable compiler identity query — for example a
`mncs compiler-identity` document or `--version` fields carrying the source
revision, profile range, and build time — so a host can fail closed (or pin
`UNKNOWN`) before running a mismatched compiler against newer sources.

## Acceptance test

Forge can assert, without invoking a full compilation, that the selected
binary was built from a revision compatible with the checkout's library
sources, and `project.doctor` can surface that decision deterministically.
