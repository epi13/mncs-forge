# Native execution-assurance request policy evidence

This is operator-controlled development and verification evidence. It is not MNCS/MNCDS
conformance, independent evaluation, protected custody, certification, promotion, or sandbox
assurance.

## Baseline and authority

- Forge baseline: `718de3b0b083330815cf4cf7a716e10b1e3f2a12` (`origin/main` at Work Intent entry).
- Environment: `renv_b6d813ecb8a594ea`.
- Campaign: `cmp_luna_max_native_ecosystem_20261105_0400z`.
- Forge Work Intent: `int_0a63337255aa2d99`, session `ses_f5675dc7553ecee0`.
- The isolated checkout was scope-granted and claimed for this Forge Work Intent. Its branch is
  `campaign/luna-max-forge-assurance-request-native-20261105`.
- The native sequence comparison dependency was verified from Stdlib `origin/main` at
  `c5ec09ecee2c31ac73a17f8a8e9bcb8b69515d55` using a temporary detached worktree. This dependency
  includes the published 4096-byte process-environment support; the older adjacent checkout did
  not and was not used for the final targeted result.

## Responsibility moved into MNCS

Forge's execution-assurance request path previously used Python sets to reject an empty request,
detect duplicate property names, subtract the declared vocabulary, and check membership for each
assurance flag. The native `execution_assurance_request_validate` function now owns those policy
decisions and returns a disposition plus the five requested-property projections. Duplicate
detection runs before vocabulary validation, including for repeated unknown names. The application
invokes this policy before receipt-binding lookup and assessment persistence. Python retains the
transport adapter and the separate persisted-record integrity checks; the assurance classifier and
assessment persistence retain their existing responsibilities.

The adapter bounds the transport to eight strings of at most 32 UTF-8 bytes each and invokes MNCS
with a 50,000-step budget. All five supported names fit (the longest is 23 bytes). A maximum-size
eight-item input vector is covered. Requests beyond the transport bound are rejected as
`ASSURANCE_REQUEST` before receipt lookup.

## Tooling correction discovered by native verification

Running the native differential test through Environment exposed a lock collision: Environment
holds an exclusive flock on the checkout directory while its provider executes, and Forge's
compiler scratch helper tried to take that same directory flock from the provider's child process.
Forge now serializes its scratch creation and pruning using the private, ignored
`.mncs/cache/forge-compile-workspace.lock` file in the exact checkout. The helper checks the root,
cache directories, lock ownership and file properties, and root identity around lock acquisition.
A regression test holds the checkout-directory flock while exercising compiler scratch and
confirms that the child-side operation completes.

## Verification

Against the Forge source checkout and the Stdlib revision listed above:

```bash
PYTHONPATH=src \
MNCS_STDLIB_ROOT=/tmp/mncs-stdlib-assurance-test-20261105 \
python -m pytest -q \
  tests/test_execution_assurance.py \
  tests/test_compile_workspace.py \
  tests/test_mncs_assurance.py
```

Result: all 57 tests passed, including independent native request-policy vectors, the maximum
bounded input, request error precedence before binding lookup, non-persistence on rejected input,
existing assurance classification, the Environment-lock regression, and compiler-scratch lifecycle
coverage. The Environment capability `mncs-forge:test/forge-assurance-differential-oracle` also
passed after the lock correction, reporting 31 passing tests.

`ruff check` and `ruff format --check` passed for `application/assurance.py`,
`tests/test_execution_assurance.py`, and `tests/test_mncs_assurance.py`. `git diff --check` passed.

The broader Environment capability `mncs-forge:test/forge-assurance-regressions` exceeded its
fixed 120-second budget. Its captured partial output contained failure markers, but the capability
did not report test names or a completed result. It is recorded as incomplete; it is not represented
as a passing full Forge regression run. The targeted suite above completed independently with the
authoritative Stdlib dependency revision.
