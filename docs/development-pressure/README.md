# Forge language-pressure records

Forge routes language fixes upstream to `mncs-language` as development-pressure
evidence (see `AGENTS.md`); this directory is Forge's local inventory of the
open language frontier encountered while building Forge. Each record carries a
stable identifier, a reproducer against the current toolchain, the Forge-side
workaround in use, and the acceptance test that would retire it.

Resolved pressures are removed from this directory; the reasoning is preserved
in git history and in the record that supersedes them.

## Open pressures

- [FORGE-P-0001](forge-p-0001-toolchain-identity.md) — toolchain build identity
  for stale-binary detection.
