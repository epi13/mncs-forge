# Native provider canonical identities

Status: bounded implementation increment complete; non-normative local development evidence.

## Migration

`ProviderArtifact` now computes its canonical structured identities through a small retained
MNCS application, `mncs.forge.provider_digest.v1`, which delegates to the shared
`mncs.std.sha256.v1` kernel. This covers declaration, dependency inventory, build and execution
receipt, admission input, contract inventory, subject, and result identities. Forge still owns
JSON serialization, file reads, admission decisions, and artifact publication.

Bulk source, compiler, embed, and backend artifact byte checks remain on the optimized host
checksum path. A trial that streamed 250 KB of declared source and library bytes through the
selected research-bytecode runtime took about 86 seconds; that cost is outside the provider
latency budget. The shipped migration uses MNCS for bounded canonical identities and keeps raw
artifact integrity checks at the byte-custody boundary. The module-level `digest` helper remains
for compatibility with external callers and for the provider-specific cache-directory partition.
Automation's in-process cache-key use is the next consumer to review.

The digest application uses its own bounded directory inside the shared native-application cache.
One retained adapter is reused by provider instances in a process and closed at interpreter exit.

## Verification

- `tests/test_provider_input_identity.py`: 9 passed.
- Native SHA-256 vectors (empty, padding boundaries, block boundaries, and multi-chunk input)
  matched independent Python `hashlib` expectations.
- The real Automation coherence provider built, passed admission, executed, produced native
  build/execution receipt identities matching independent canonical-JSON hashes, and reused its
  result without spawning a provider process.
- Source mutation caused the owner artifact to rebuild; a restarted provider reused the exact
  receipt. Corrupt receipt and artifact checks remained fail-closed.
- Four matched-toolchain provider tests passed; together with the input-identity suite, the
  selected run passed 13 tests in 84.52 seconds.
- `ruff check --ignore E501` on the touched Python files, Python compilation, and `git diff --check`
  passed. The linked-worktree fixture now locates the family root by its existing Automation
  artifact marker.

The host oracle remains a test expectation only. This evidence does not claim native hashing of
large compiled artifacts or full Forge suite coverage.
