# Continuous development supervision

Forge can run a local, declaration-driven development supervisor over one resident
`mncs-language-service` process. It is enabled by a checked-in `[continuous]`
configuration table and consumes the service's bounded `mncs.workspace-change/1`
cursor; it does not scan or retain source text as an event log.

The production topology is:

```text
mnls-language-service-host
        └── one LanguageService workspace and event cursor
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
candidate, changed-path identities, and provider-declared complete dependency
envelope all match and Forge's existing freshness projection is `CURRENT`.
Otherwise the verifier runs again or remains `UNKNOWN`; there is no private
continuous-mode cache and no whole-suite fallback.

Successful routine checks remain in the bounded status projection. Attention
events are reserved for FAIL, blocking UNKNOWN, failed/conflicted Safe repair,
stale work, or an explicit scope/security decision. The resident frontend is
still synchronous; results are generation-checked and late work is discarded,
not reported as current evidence. The event cursor is bounded and may require a
reset after transient history ages out.
