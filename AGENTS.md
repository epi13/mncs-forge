# MNCS Forge agent guidance

- Use Forge for controlled candidate/evidence workflow when a project has a validated
  `mncs-forge.toml`. After modifying candidate content, call `candidate refresh` or
  Control's `forge_candidate_refresh` before another candidate-scoped evaluation.
  Refresh registers a successor identity; it does not reuse prior evidence as if it
  were still current.
- Forge does not replace MNCS or MNCDS offline validation and is not required for conformance.
- Modify candidate or generated files only within the declared paths. Never write to contracts,
  references, evaluators, policies, or protected authorities through Forge.
- Keep MNCS, MNCDS, evidence-class, and promotion statuses separate. Missing or unsupported
  evidence remains `UNKNOWN`; `FAIL` dominates `UNKNOWN`, which dominates `PASS`.
- Final evaluator-mode results are not repair feedback for the same development epoch.
- Do not claim independence, protected custody, witnessing, governance approval, certification,
  or promotion from a local Forge result.
- Use Forge provider discovery and capability blockers to select an appropriate declared
  structural, control-flow, or data-flow provider when a change requires that evidence.
- Source reading, grep, and line counts are review aids, not substitutes for unavailable
  structural evidence.
- Missing, unsupported, malformed, stale, or unavailable required capability remains
  `UNKNOWN` or a blocker, never `PASS`.
- When comparative graph-sensitive evidence is claimed, use the same provider, method,
  scope, and relevant bounds before and after the change.
- Treat suspicious or non-orthodox constructs as requests for bounded evidence, not automatic
  failures or automatic exceptions. Orthodoxy is a heuristic; declared invariants remain
  authoritative.
- An intentional-deviation declaration may explain purpose, assumptions, compiler envelope, and
  required checks, but it must never suppress, overwrite, or reinterpret a verifier `FAIL`.
- Keep local invariant results, attack-path reachability, freshness, severity, and workflow
  disposition separate. Absence of a demonstrated exploit chain does not convert a confirmed
  weakness into `PASS`.

## MNCS agent execution contract

This repository owns **assurance semantics** in the ecosystem authority table
and adopts the ecosystem agent contract bound in mncs-actions (`AGENTS.md`
there) with the language mirror in mncs-language. Enforced by
`tests/test_agent_contract.py`: every path named below must exist.

- Forge evaluates development workflows; it never replaces MNCS/MNCDS
  validation and never closes obligations owned elsewhere. A Forge verdict
  that contradicts an owning protocol's evidence is a defect in the
  evaluation, not an override.
- Evaluator or candidate scaffolding that reimplements MNCS-expressible
  semantics is a language-pressure event routed to mncs-language as
  development-pressure evidence; fix upstream, re-run that suite, then
  resume here.
- At entry, obtain bounded family context and current language/Commons
  identities before broad search. Query the language capability index and
  existing Commons pressures before adding evaluator or provider scaffolding.
- If a missing generic capability is the reason for host code, route pressure
  to the owning language/stdlib/runtime layer and keep local code an explicit
  transport, compatibility, differential, or reference boundary.
- Missing or unsupported evidence remains `UNKNOWN`; a green evaluation
  with no evidence behind it is a defect in the check, not a success.
- This repository currently carries no MNCS conformance badge in its
  readme; do not add a decorative one. A future badge must render the
  evidence-driven verdict and must not overstate compile versus execution
  or emulated versus physical proof.
