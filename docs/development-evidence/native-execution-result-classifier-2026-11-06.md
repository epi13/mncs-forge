# Native declared-workflow result classification

Status: bounded native migration completed; full Forge suite not run.

## Authority and source state

- Campaign: `cmp_luna_native_ecosystem_20261106_0856z`.
- Environment session: `ses_555a3749ff234507`.
- Work Intent: `int_0a63337255aa2d99`, “Develop and verify Forge provider and execution integration within the selected checkout.”
- Forge source base: `origin/main` at `5d81fb486917e35988fde80477c4ec268c03f849`.
- Shared Stdlib source: `mncs-stdlib` main at `6de89672f573eefc7e0982c715d685399c56cff1`.
- Stdlib bundle identity: `mncs:stdlib-bundle:531e253cfdb8ffaa4139fead626db60f94e3878edebaa615d377f843a4d5c831`.

## Responsibility transferred

For normal Forge native operation, `WorkflowExecutor` no longer decides whether a declared
command result is `PASS`, `FAIL`, or `UNKNOWN`. It still starts the declared process, parses JSON,
and validates whether a structured status is recognized. The typed result facts go through
`NativeForgeAdapter.execution_result_status`, the Forge MNCS entrypoint delegates to
`mncs.core.status.v1::classify_execution_result`, and Forge retains its domain-specific diagnostic
selection, record creation, and protocol-provider behavior.

The host compatibility classifier is retained for the explicit `native_mode=off` path and for
`prefer` mode when native execution cannot be selected. Provider Protocol workflows keep their
separate failure semantics. A nonzero command exit takes precedence over a reported PASS in the
native kernel.

## Independent expectations and integration

Seven adapter vectors cover zero-exit PASS/FAIL/UNKNOWN, nonzero-exit override of reported PASS,
nonzero exit without a structured result, zero exit without a structured result, and missing exit
status despite a reported PASS. The source-level expectations are independently encoded in the
Stdlib execution corpus and execute on Cranelift.

The production Forge consumer test runs four declared commands through the native workflow path:

- zero exit with a structured PASS becomes PASS;
- exit code 7 becomes FAIL;
- zero exit without a structured status becomes UNKNOWN;
- malformed structured status `[]` remains UNKNOWN.

The downstream aggregate is FAIL. Explicit native-off mode retains and verifies the compatibility
route.

## Verification

- Stdlib Cranelift corpus: 7 cases passed; `tools/regen.sh --check` passed.
- Forge adapter classifier vectors: 7 passed.
- Forge native workflow consumer: 1 passed; all four returned statuses matched the independent
  expectations.
- Explicit native-off workflow compatibility: 1 passed.
- Direct service and Fabric adapter compatibility checks passed in the focused test invocation.
- Ruff checks, format checks, Python compilation, and `git diff --check` passed for changed paths.

The successful Forge integration used the newer automatic debug toolchain selected from
`mncs-language` revision `f8212ed8b7478196b5293a84152154be5e0bea3f`:

- `target/debug/mncs` SHA-256:
  `129ef764bd56f6a3e05ee19631c46c044217590d537d52ee1c62b0fba335d7ae`.
- `target/debug/libmncs_embed.so` SHA-256:
  `b3b1deb0b5c9ef6c317738d7144d3a18df48fbfe98c3aaf60512b15024d19c0b`.

One initial integration attempt explicitly pinned the older Nov. 4 Release CLI/embed pair and the
process transport rejected `byte_string`. With the normal newer Debug selection, the same native
integration passed. No Language source or build outputs were changed during this Forge task.

## Limits

This verifies the bounded result-classification contract and its real Forge workflow consumer. It
does not establish full-suite Forge health, process isolation, independent evaluation, or broader
MNCS/MNCDS conformance. The host remains responsible for execution mechanics and JSON validation;
the compatibility route remains by design.
