# Continuous hardening campaign evidence

This record covers the latency, lifecycle, restart, routing, security-trigger,
and Store-evidence-reuse pass on `campaign/continuous-hardening-20260921`.
It preserves the existing ownership boundaries: Store is authoritative,
Language Service owns local semantic impact, RAVEL selects cross-repository
scope, Actions proves selected family work, and Forge supervises declared
actions.

## Startup measurements

Measurements used the controlled Store-backed Forge state with native Store
semantics enabled. The Store adapter was not replaced with a host-side hash or
semantic fallback.

| phase | before | after |
| --- | ---: | ---: |
| clean Store `_recover()` | approximately 0 s, but followed by repeated verification | approximately 0 s, with one open-time verification owner |
| complete current-generation verification | approximately 22.5 s per pass, observed repeatedly | 22.64–23.04 s once per resident Store open; subsequent projection reads about 0.00006 s |
| `StoreBackedRecordStore.recover()` | about 47.1 s per duplicate pass | not called a second time by Forge for a Store-backed adapter |
| Forge Store projection / first ledger view | about 24 s for each record query | 1.96–2.01 s first projection; about 0.00003 s for warm queries |
| derived index validation/rebuild | included in repeated scans | 0.00007–0.00010 s when the generation/feed identity is unchanged |
| stranded-verifier recovery | about 48.85 s from repeated history scans | about 0.0001 s over one bounded action/result view |
| Forge construction | 186.1–187.2 s | 44.97–45.88 s |
| Store session construction | previously folded into the long Forge path | 20.36–20.87 s |

The largest remaining primitive is native `mncs.std.sha256.v1` / retained Store
runtime work. In one controlled retained session, native SHA calls took 1.3516 s
for 256 bytes, 4.2376 s for 1 KiB, 8.2038 s for 2 KiB, 16.2191 s for 4 KiB,
32.2814 s for 8 KiB, and 64.0360 s for 16 KiB. This is why startup is much
shorter but not yet low-single-digit seconds. No host hash was substituted.

The Store now owns one open-time recovery/verification path, and its verified
current-generation objects plus commit feed are rebuildable generation-bound
projections. A successful publication extends that projection with the already
validated object. Forge binds its ledger projection and derived index to the
Store generation and commit-feed identity; a changed identity rebuilds it.
Store-backed queries therefore inspect zero old objects on a warm lookup, while
Store remains the authority for every cold projection and publication.

## Resident lifecycle and restart

`mncs-forge continuous start` is bounded and detached. It ensures the canonical
`mnls-language-service-host`, verifies workspace-root identity, and starts one
`mncs_forge.continuous_host` process using the existing `ContinuousSupervisor`.
`continuous status` is bounded and reads the live lease/status projection;
`continuous stop` terminates only the Forge supervisor cleanly. A second start
attaches/reports the existing supervisor and does not create another semantic
service.

Controlled temporary-workspace measurements:

- status with no services: 0.24 s;
- canonical Language Service cold launch/attach itself: 0.0516 s inside the
  lifecycle helper (0.23 s including the Python command wrapper);
- first bounded start, including canonical Language Service launch and the
  Store-backed Forge host: 70.50–73.05 s;
- repeated start while the Language Service was resident: 70.04–70.14 s in the
  earlier controlled run, dominated by the new Forge process's Store
  open/verification;
- `continuous status` while resident: 0.24 s; clean supervisor stop: 0.36–0.39 s;
- an edit was visible as a semantic event and resident status update in about
  6.3 s with the controlled polling interval;
- the prior resident Language Service observation measured socket readiness at
  0.041 s, status requests at about 0.003 s, and a small `did_open` to semantic
  response at 0.031 s.

The Language Service now publishes `mncs.workspace-change/2` and
`mncs.workspace-event-cursor/2` with an explicit stream identity. It writes a
compact `.mncs/mnls-language-service.checkpoint.json` containing workspace
identity, stream identity, generation/cursor, and source identities—not the
transient event log. On restart it restores the stream identity and generation
floor, compares current files with the checkpoint, and emits bounded
`reconciled=true` events for changed files. Reconciled incomplete impact remains
`UNKNOWN` until a new proof is established.

The integration test
`restart_reconciles_offline_edit_without_reusing_cursor_alone` passed, and the
resident demonstration preserved one stream identity across a Language Service
restart, detected an offline edit at cursor 4, and surfaced `UNKNOWN` attention
rather than fabricating `PASS`.

## Security and evidence reuse

The controlled continuous fixture declares one bounded `security_micro_verifier`
(`verify-security-pass`) with a complete provider dependency envelope. The
focused continuous test demonstrates:

1. a reconciled security event matches the explicit security trigger;
2. the cold provider run returns `PASS` with `reused=false`;
3. an unchanged declared envelope returns `PASS` with `reused=true`;
4. an unrelated contract edit still reuses the result;
5. a declared `reference/reference.py` dependency edit invalidates the result
   and causes a recomputation.

The bounded fixture recorded one cold recomputation and two reuse decisions
before the dependency mutation, followed by a second recomputation. The
focused security/reuse test exercised the supervisor event path and completed
in 0.81 s. Forge
validates verifier/provider/toolchain/configuration/policy/environment
identities and the complete dependency envelope before promoting `CURRENT`.

A live Store-backed cache-hit timing could not be completed: the cold evidence
publication remained in native Store SHA work beyond 4.5 minutes and was
interrupted. The reuse semantics are proven by the focused bounded adapter test,
but Store-backed cold publication and warm lookup remain the residual latency
pressure rather than a claimed live cache hit.

## Cross-repository routing

The existing RAVEL family overlay path now receives the configured Commons
family graph, obligation inventory, current evidence, and repository identity.
Selected-family Actions proof arguments are passed through the existing Forge
failure loop. The focused RAVEL routing test selects exactly the declared
consumer set (`mncs-actions`, `mncs-forge`, and `mncs-test` in the current graph)
rather than the complete family. Incomplete family evidence remains
`UNKNOWN`/non-stopping according to the existing policy.

## Final reconciliation

Focused checks run during implementation included:

- Store embedded projection tests: `15 passed`;
- Forge continuous tests: `8 passed`;
- Forge Store adapter duplicate/reopen/index test: passed in 84.14 s;
- Language Service service-core integration tests: `30 passed`;
- RAVEL family-routing tests: `2 passed`;
- Codex launcher relocatable-entrypoint test: passed;
- Python compile and diff checks: passed.

The combined Store embedded/lifecycle run was intentionally interrupted after
14 tests and 576.92 s because it was exercising the measured native runtime
pressure; it was not treated as a test failure or hidden by a larger timeout.

The final micro-verifier benchmark was run with its declared 300 s limit and
ended at `elapsed=300.01`, exit `124`. The remaining stack was native
`mncs_model::ssa_execution` through `mncs_session_call_batch`, so the timeout
was not raised. The previous launcher timeout case now passes.

The remaining blocker to making continuous mode the default environment is
therefore the native Store/runtime cost of cold evidence publication and
verification. Lifecycle, restart reconciliation, selective family routing,
security triggering, evidence identity checks, and bounded attention behavior
are now integrated without a second authority or supervisor implementation.
