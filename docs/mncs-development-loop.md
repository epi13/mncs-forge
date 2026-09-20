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
Actions    -> selected-family composite proof, execution receipts, and evidence manifests
```

`verification_plan_file` is optional for compatibility, but it is the normal
path for source changes. The plan binds the source digest, compiler graph
identity, selected test identities, verification level, typed escalation
reasons, and the evidence required to stop. Forge validates and forwards it;
it does not select tests or rebuild the graph. For
`selection.routing_scope = selected_repositories`, configure `mncs_actions`
and pass `family_graph_file` plus `family_workspace_root`; Forge invokes the
trusted Actions adapter and consumes its composite proof. The adapter owns
exact check routing and mncs-test execution. A selected plan without an
established composite proof remains `UNKNOWN`.

The default diagnostic depth is `minimal`: validation and inspection run after
the failing witness, followed by the typed mncs-debug sufficiency decision. If
the decision says the diagnosis is ambiguous, Forge requests only its named
next projection (trace, provenance, replay, or minimization) and asks for a
second sufficiency decision. The same witness, validation, and inspection are
reused. `standard` adds the explicitly requested trace/provenance and `deep`
adds open, replay, and minimization where the debugger declares them. These
queries use the existing failing execution and do not rerun successful tests.
Forge records the evidence gap, requested operation, reused artifacts, and the
point at which diagnosis became sufficient. An Actions handoff preserves its
receipt/manifest references and lets Forge consume the existing witness and
sufficiency projection.

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

## Native Forge assurance authority

The development service now normalizes owner-produced documents into bounded
typed facts and calls the packaged `mncs.forge.assurance.v1` state machine
through the generic `mncs run-app` application surface. The state machine is
the single Forge authority for workflow disposition:

```text
provider facts + lifecycle/readiness/reconciliation facts
        -> ForgeLoopInput
        -> mncs.forge.assurance.v1
        -> ForgeLoopDecision
        -> host performs the requested external action
```

The loop has explicit initial-verification and after-repair phases. Its
decisions are bounded to stopping with `PASS`, `FAIL`, or `UNKNOWN`, or asking
for diagnosis, repair, a rebound plan, selected verification, or family proof.
The status join is `FAIL > UNKNOWN > PASS`; missing evidence, stale plan
bindings, invalid identity bindings, and insufficient proof remain
`UNKNOWN`. Repair eligibility, source-change invalidation, rebound-plan
requirements, post-repair verification requirements, and family-proof
sufficiency are Forge-owned policy and are evaluated natively.

Provider ownership is preserved at the membrane. mncs-test supplies test
verdicts and selected executions, Ravel supplies the verification plan,
mncs-debug supplies diagnosis/sufficiency, and Actions supplies selected-family
proof. Forge consumes those facts and does not recreate their semantics.

The host still owns process invocation, deadlines, environment and path
containment, external provider commands, artifact ingress/egress, defensive
schema admission, exact source replacement, and publication. These are real
transport/mechanics boundaries. `tests/test_mncs_assurance.py` keeps the former
Python behavior as a bounded differential oracle while the native path is
enabled; it is not a second canonical implementation.

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
