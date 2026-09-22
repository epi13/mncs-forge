# Continuous development supervision

Forge can run a local, declaration-driven development supervisor over one resident
`mncs-language-service` process. It is enabled by a checked-in `[continuous]`
configuration table and can be established with one bounded command:

```text
mncs-forge continuous start
mncs-forge continuous status
mncs-forge continuous stop
```

`start` attaches to or launches the canonical Language Service, then launches
one detached Forge process that delegates to the existing
`ContinuousSupervisor`. `status` reads lifecycle state without constructing
Forge; `stop` requests a clean supervisor shutdown. The shared Language Service
may remain resident for LSP/MCP clients after Forge stops.

The service consumes its bounded `mncs.workspace-change/2` cursor; it does not
scan or retain source text as an event log. Cursors carry an explicit stream
identity. The Language Service persists only a compact workspace checkpoint
under `.mncs/`; after restart it compares source identities and reconstructs
bounded reconciliation events for offline edits.

The production topology is:

```text
mnls-language-service-host
        └── one LanguageService workspace, stream identity, and event cursor
             ├── mncs-lsp (MNLS_SERVICE_SOCKET)
             ├── mncs-mcp (MNLS_SERVICE_SOCKET)
             └── mncs-forge continuous run
```

Forge matches only explicit trigger declarations. A trigger names an action
(`doctor_safe`, `verification_plan`, `micro_verifier`, or
`security_micro_verifier`), semantic change/guarantee/risk filters, optional
contract/obligation identity patterns, a maximum cost, and an escalation policy.
`verification_plan` invokes the existing
compiler-impact → RAVEL → exact `mncs-test` path. Failures are passed through
the existing `mncs_failure_loop`, so `mncs-debug` receives the selected failure
witness rather than causing a broad rerun.

Safe Doctor repair is generation- and source-identity-bound. Forge performs a
safe-only dry run, applies the exact Doctor transaction, asks the resident
service to re-read the file, and drains the resulting rebound event. Review and
Manual fixes never enter this path. Repair outcomes are recorded as the
versioned `mncs.continuous-repair/1` projection through the normal Store-backed
Forge evidence boundary.

Verifier reuse reads existing `verifier_result` records only. A record is
reusable when the verifier, provider, configuration, policy, environment,
candidate and stable input identities still match, the provider-declared
complete dependency envelope remains unchanged, and Forge's existing
freshness projection is `CURRENT`. An unrelated edit therefore does not force
the verifier process to run again; an edit inside the declared envelope does.
Otherwise the verifier runs again or remains `UNKNOWN`; there is no private
continuous-mode cache and no whole-suite fallback.

The detached supervisor refreshes the bounded status projection while it runs,
so status is useful without stopping the process. Successful routine checks remain in that projection. Attention
events are reserved for FAIL, blocking UNKNOWN, failed/conflicted Safe repair,
stale work, or an explicit scope/security decision. The resident frontend is
still synchronous; results are generation-checked and late work is discarded,
not reported as current evidence. The event cursor is bounded and may require a
reset after transient history ages out; a host restart is handled by the
checkpoint/stream identity rather than by treating an equal cursor number from
a different host as valid.
