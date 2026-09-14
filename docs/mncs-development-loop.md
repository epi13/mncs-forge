# Structured MNCS development loop

Forge exposes `development.mncs.failure-loop` as a development-authority
operation (`mncs failure-loop` on the CLI and
`mncs_forge_mncs_failure_loop` over MCP).

The operation composes the owner-native providers:

```text
mncs-test -> mncs.test-result/1 + mncs.check-result/1
          -> selected failing TestExecution
mncs-debug -> mncs.debug-witness/1 + bounded query projections
Forge      -> diagnosis projection and optional exact repair
mncs-test -> verification TestResult/CheckResult
```

The same operation also accepts `provider_mode = "consume"` for an
Actions-produced handoff. In that mode Forge validates the transported
`mncs-test` result/check and `mncs-debug` check/witness, preserves the Actions
execution-receipt/evidence-manifest references, runs only the remaining
structured debugger queries, and then performs the bounded repair and
canonical verification. This is the explicit `mncs-actions -> Forge` seam;
it does not reparse provider terminal output.

Forge is responsible for bounded invocation, artifact references, identity
correlation, authority checks, and the before/after evidence projection. It
does not implement assertion semantics, test selection semantics, trace
semantics, provenance algorithms, or replay guarantees. A test `FAIL` remains
`FAIL` when debug evidence is unavailable; debug unavailability is represented
as `UNKNOWN` in the debug and diagnosis projections.

The operation accepts an exact replacement only inside the configured
candidate/generated scopes and only after a valid debugger witness has been
established. The replacement must match once. Verification runs the same
canonical test command and retains separate before/after result and check
artifacts. Trace capture is bounded by `failure-only`, `bounded`, or `events`
policy and a maximum event count.
