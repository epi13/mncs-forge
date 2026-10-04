# Warm native runtime

Owner: `mncs-forge` (`src/mncs_forge/mncs_native.py`,
`src/mncs_forge/provider_artifacts.py`).

## Shape

```text
typed work request
      |
Forge
  |-- admitted artifact (identity-addressed disk cache)
  |-- compatible warm execution context
  |-- capability envelope, resource limits, deadline/cancellation
  |-- native/resident executor
  `-- exact receipt
```

Execution mode (normal, test, observe/debug, compile/query, remediation,
experiment) is policy over this one substrate: it affects observation,
limits, and capabilities, never the executor.

## What warm reuse skips

A warm invocation against unchanged state does not repeat compiler
startup, native compilation, artifact rediscovery, full admission,
full-input hashing, environment reconstruction, library rediscovery,
transport establishment, or temporary build-context creation. Reuse keys:

- source/artifact identity, compiler identity, runtime/target identity,
  relevant library identities, codegen configuration.

First work compiles; unchanged subsequent work reuses; a relevant change
invalidates and rebuilds exactly once. Reusable artifacts are never keyed
to `TemporaryDirectory` paths, PIDs, wall clock, or random invocation
ids.

## Identity without rehashing

`ProviderArtifact.expected_inputs()` reuses the authoritative input
identity while a cheap stat/ctime signature is unchanged (cold ~71 ms /
303 hashes, warm ~5 ms / 0 hashes for a 300-file + 23 MB compiler
identity). Corruption detection is preserved: artifact and embed bytes
are still checked unconditionally, and the canonical `inputs()` path with
containment checks remains the fallback.

## Admission

`ensure_session()` is single-flight under an in-process lock: concurrent
duplicate requests for one uncached artifact compile exactly once, and
one caller cancelling never kills shared preparation. Cache-hit admission
opens the artifact once (cold-process admission ~29 s down to ~9-16 s
depending on toolchain generation; in-process re-admit ~2 ms).

The disk cache keeps the newest 8 generations (pruning only loses warm
hits, never correctness) and skips in-flight temporary files. Per-call
latency history is bounded.

## Default engine and process argv

The default engine executes real development-provider work through the
typed process-effect transport, which admits 256-argument vectors (see
`mncs-language/docs/process-abi.md`). Forge requests its real configured
capture budget (up to the 4 MB ceiling) instead of clamping to the 1 KB
observation window; chatty provider output terminates with truncation
reported. Proven end to end: 24-argument invocation through the default
engine succeeds with all arguments delivered.

## Toolchain selection

The native adapter resolves its toolchain as:

- `MNCS_LANGUAGE_ROOT` (else the `mncs-language` sibling checkout) for
  compiler sources and the release/debug `mncs` binary (`MNCS_CLI`
  overrides the binary directly);
- `MNCS_LIBRARY_ROOT` / `MNCS_STDLIB_ROOT` (else the `mncs-stdlib`
  sibling checkout, else `language/library`) for the stdlib;
- `MNCS_EMBED_LIB` for the retained-session library.

`MNCS_BIN` is the environment/Store convention and is not consumed here.
A toolchain generation change invalidates admission once; steady state
after that is warm.
