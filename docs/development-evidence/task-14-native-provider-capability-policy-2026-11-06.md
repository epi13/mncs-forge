# Native Forge provider capability policy

Implementation commit: `746273487e0346f0e1135455157f64592150a031`

Environment Work Intent: `int_0a63337255aa2d99`

Environment session: `ses_d649de66862b1aaf`

## Responsibility transferred

`ProviderService.capability_blockers()` previously decided, in Python, whether a declared
provider capability matched a required capability and whether a current probe established it.
The native Forge policy now owns those membership and qualification decisions, along with the
required-provider blocker and optional-provider availability classifications.

`NativeForgeAdapter` converts capability names to domain-separated SHA-256 identities and streams
all facts through fixed 16-item MNCS chunks. The MNCS state carries matches across chunks. The
input lists remain unbounded by configuration and are never truncated. Forge retains provider
discovery, process execution, inventory, sorting, and response presentation. The shared Stdlib
status fold remains the authority for the final status aggregate. Explicit native-off mode retains
the compatibility implementation.

## Independent expectations exercised

- A long capability name found after more than two chunks of declared and probed values is a
  candidate and is established when its current status is PASS and it is not unsupported.
- A matching unsupported construct after the first chunk prevents establishment.
- A declaration without a matching probe is not established.
- A required provider with UNKNOWN status is a blocker; an unavailable optional provider is
  informational.
- The integrated `Forge.capability_blockers()` consumer returns UNKNOWN when a probed, declared
  capability is also marked unsupported.
- Native-off mode still returns the expected compatibility result.

## Verification

- Native Forge integration check `test_provider_inventory_and_capability_blockers_use_native_status`:
  passed; the selected Forge artifact compiled and the consumer invoked the native status path.
- `PYTHONPATH=src pytest -q tests/test_native_provider_status.py`: passed all 23 tests collected at
  that point, including the native policy cases and the integrated blocker consumer.
- `PYTHONPATH=src pytest -q tests/test_native_provider_status.py::test_native_off_keeps_the_explicit_compatibility_classifier`:
  passed separately after adding the compatibility case. A subsequent collection check found 24
  total tests in the module. The declared-without-probe case was added and passed separately; the
  final module contains 25 tests.
- `python -m compileall -q src/mncs_forge tests/test_native_provider_status.py`: passed.
- `ruff check src/mncs_forge/application/providers.py src/mncs_forge/engine.py tests/test_native_provider_status.py`:
  passed.
- `git diff --check`: passed.

The full Forge suite was not run. Environment verification descriptors for this session projected
the repository's `main` checkout instead of the claim's linked worktree, so those fixed commands
were not invoked against the wrong checkout. The targeted tests above ran directly in the claimed
worktree.
