# Task 8 development evidence: native process input transport

Status: bounded implementation increment complete; non-normative local development evidence.

## Authority and source state

- Environment campaign: `cmp_luna_max_native_ecosystem_20261105_0400z`.
- Session: `ses_adcab5afa74098b2`; Environment resume revalidated the selected Forge worktree and active intent.
- Work Intent: `int_0a63337255aa2d99`, “Develop and verify Forge provider and execution integration within the selected checkout.”
- Forge source base: `origin/main` at `2d226e1df2afac7db21b135fe4453a07b043af6d`.
- Native Language runtime: `f8212ed8b7478196b5293a84152154be5e0bea3f`.
- Stdlib process contract: `400a24e2a5ef01ef392cbcb6dd9e359c8b730c41`.

## Responsibility moved into MNCS

Forge's process effect client previously expanded every input byte into a nested host-created
sequence value. It now passes bounded byte strings in the canonical compact Base64 shape and uses
the `mncs.std.process.v1` initial-input plus chunk contract. The initial segment is capped at 4096
bytes, each additional segment at 4096 bytes, and the maximum is 256 additional segments
(1,052,672 bytes total). Oversized input is rejected before invoking the native process effect.

The actual process lifecycle and stdin delivery run through the MNCS runtime. Forge still owns
provider request construction, domain limits, deadlines, and execution receipts; this change does
not claim to migrate those domain responsibilities.

## Verification

- Focused transport tests: 5 passed. They check empty input, both sides of the initial boundary,
  the maximum payload, byte-for-byte reconstruction, compact encoding size, and pre-invocation
  rejection above the native limit.
- Native end-to-end verifier test: passed using the native Forge mode, Language embed runtime, and
  selected Stdlib source. The actual provider returned `PASS` only after matching all 1,024,000
  parameter characters. It reported request size greater than 4096 bytes and no greater than
  1,048,576 bytes. Forge recorded a complete `local-process` receipt with completed termination;
  the receipt status remained `UNKNOWN` and only `execution_completed` was established.
- Native Language CLI/embed build: completed successfully for `mncs-cli` and `mncs-embed`.
- Ruff check, Ruff format check, and `git diff --check`: passed.
- A broader combined Forge test run was interrupted after it remained CPU-bound in repeated native
  ABI setup. It has no complete result and is not counted as verification evidence.

## Limits

This evidence establishes bounded request delivery through the exercised Forge-to-MNCS process path.
It does not establish full Forge suite health, isolation, independence, certification, or broader
MNCS/MNCDS conformance. Host-side Base64 remains the transport encoding at the application boundary;
Forge-specific request construction and execution policy remain Forge-owned.
