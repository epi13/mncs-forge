# Structured MNCS development loop

Forge exposes `development.mncs.failure-loop` as a development-authority
operation (`mncs failure-loop` on the CLI and
`mncs_forge_mncs_failure_loop` over MCP).

For an MNCS source change, the operation composes the owner-native providers
through one digest-bound verification plan:

```text
Ravel      -> mncs.semantic-impact/1 -> mncs.verification-plan/1
mncs-test -> mncs.test-result/1 + mncs.check-result/1
          -> exact selected TestExecution
mncs-debug -> witness + progressive query projections
Forge      -> diagnosis projection and optional exact repair
Ravel      -> rebound plan after a source repair
mncs-test -> exact post-repair TestResult/CheckResult
Actions    -> execution receipt + evidence manifest for reuse
```

`verification_plan_file` is optional for compatibility, but it is the normal
path for source changes. The plan binds the source digest, compiler graph
identity, selected test identities, verification level, typed escalation
reasons, and the evidence required to stop. Forge validates and forwards it;
it does not select tests or rebuild the graph. A plan whose level is `family`
is a routing decision, not local family proof, so the operation returns
`UNKNOWN` until the family boundary is established.

The default diagnostic depth is `minimal`: validation and inspection run after
the failing witness. `standard` adds trace and provenance; `deep` adds open,
replay, and minimization where the debugger declares them. These queries use
the existing failing execution and do not rerun successful tests. An Actions
handoff preserves its receipt/manifest references and lets Forge consume the
existing witness.

The same operation also accepts `provider_mode = "consume"` for an
Actions-produced handoff. In that mode Forge validates the transported
`mncs-test` result/check and `mncs-debug` check/witness, preserves the Actions
execution-receipt/evidence-manifest references, runs only the remaining
structured debugger queries, and then performs the bounded repair and rebound
plan verification. A canonical suite is run only when the plan’s selected
level or an explicit boundary requires it. This is the explicit
`mncs-actions -> Forge` seam;
it does not reparse provider terminal output.

Forge is responsible for bounded invocation, artifact references, identity
correlation, authority checks, and the before/after evidence projection. It
does not implement assertion semantics, test selection semantics, trace
semantics, provenance algorithms, or replay guarantees. A test `FAIL` remains
`FAIL` when debug evidence is unavailable; debug unavailability is represented
as `UNKNOWN` in the debug and diagnosis projections.

The operation accepts an exact replacement only inside the configured
candidate/generated scopes and only after a valid debugger witness has been
established. The replacement must match once. When a plan was supplied, a
post-repair plan or declared `ravel_impact` command is required; stale plans
cannot be reused after the source digest changes. Verification runs only the
rebound selected identities and retains separate before/after result and check
artifacts. Trace capture is bounded by `failure-only`, `bounded`, or `events`
policy and a maximum event count. The result includes compact observability:
selected versus available checks, affected surface size, diagnostic depth,
escalation reasons, and reused evidence references.
