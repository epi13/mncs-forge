# Stable-local Forge threat model

Status: reviewed for the Task 9C local-stability increment. This document describes the current
local implementation and its executable evidence. It is not a claim of sandboxing, independent
evaluation, protected custody, witnessing, certification, governance approval, or MNCS/MNCDS
conformance.

## Scope and claim boundary

The scope is the stable-local Forge target currently implemented in this repository:

```text
user / agent
    |
CLI or MCP
    |
canonical operation registry and invocation gate
    |
Forge facade / application services
    |
state machine and typed records
    |
Runner | ProjectObserver | RecordStore ports
    |              |              |
LocalProcessRunner project FS     local FS, ledger, recovery
    |
configured provider or workflow executable
```

The model covers the CLI, stdio MCP server, operation registry, application services, lifecycle
authorization, local project observation, bounded local execution, Provider Protocol 0.1 parsing,
transactional local persistence, package/runtime integrity, and development/evaluator evidence
classification.

It does not model a future Podman, Docker, SSH, Forge Cell, attestation, external-anchor, or
Fabric-backed live runner as though those controls exist. Task 7B-2's persisted
`execution_receipt_binding` is a local provenance/linkage record. It does not create sandbox,
independence, custody, witnessing, or certification. A Fabric adapter seam can translate remote
facts into the same observation type; it does not import Fabric or move the trust boundary. In
particular, `LocalProcessRunner` executes trusted configured code with host permissions; it is not
an operating-system, filesystem, process, memory, CPU, disk, or network sandbox.

The central status invariant remains:

```text
FAIL > UNKNOWN > PASS
```

Missing, malformed, stale, unsupported, or unavailable evidence remains `UNKNOWN` or a blocker.
Process exit zero alone is not verifier `PASS`.

## Protected assets and security properties

| Asset | Primary concern | Current protection or binding |
| --- | --- | --- |
| Candidate identity and lineage | integrity, authority | content identity, epoch/candidate ancestry, state-machine checks |
| Contract/reference/policy identities | integrity, authority | configured protected paths, identity maps, freeze and drift checks |
| Evaluator identity and protected material | confidentiality, authority | evaluator-only mode, protected-path write rejection, status-only disclosure |
| Configuration and provider declarations | integrity, authority | schema validation, path containment, declared-command and capability checks |
| Provider/executable/environment identity | integrity, freshness | executable identity, provider identity/version, allowlisted environment and drift checks |
| Verifier action/result lineage | integrity, freshness | typed records, candidate/provider/configuration/input identities, terminal action rules |
| Freeze bindings and evaluation records | integrity, authority | selection/evidence/freeze revalidation and evaluator entry authorization |
| Store objects, generations, and descriptors | integrity, recoverability | content/tree identities, domain bindings, durable publication, verification and recovery |
| Transaction journals and derived indexes | availability, integrity | durable staging, expected-head checks, startup recovery, rebuildable index |
| CLI/MCP operation authority | authority, integrity | one canonical registry, mode/mutation metadata, centralized invocation gate |
| Disclosure boundaries | confidentiality | evaluator status-only policy and redaction-before-identity behavior |
| Package/runtime installation | integrity | wheel/sdist audit, import-origin check, isolated install, `pip check`, CI matrix |
| Benchmark/development evidence | classification, interpretation | machine-readable non-normative evidence class and environment metadata |

The historical local ledger is an integrity detector for the legacy history it can read. It is not external
timestamping, independent custody, or a guarantee that the current filesystem was not replaced by a
privileged local attacker.

## Actors and capabilities

| Actor or condition | Capability assumed | Trust treatment |
| --- | --- | --- |
| Untrusted candidate/project content | malicious source, symlinks, traversal, malformed/pathological data, unexpected files | untrusted; validate containment and identity before use |
| Malformed or hostile provider output | invalid framing, oversized output, deceptive status/identity, unsupported protocol data | untrusted data; parse fail-closed and retain bounds |
| Configured provider executable | arbitrary behavior available to a normal host process, including network/filesystem use | explicitly trusted executable code, not sandboxed |
| Local unprivileged user | normal filesystem/process access and the ability to invoke configured interfaces | constrained by host permissions and Forge validation only |
| Host administrator/root | modify files, executables, configuration, processes, secrets, and complete local history | in the local TCB; can defeat local-only controls and deny service |
| Compromised dependency/build/runtime | alter imports, package behavior, execution, or evidence | in the local TCB; package checks reduce accidental contamination, not compromise |
| Crash or concurrency fault | interrupt process/power, race writers, strand transactions, corrupt indexes | reliability threat; recovery detects or resolves bounded failure modes |
| GitHub-hosted CI environment | build and test the submitted artifact | development evidence only, not independent evaluation or custody |

## Trusted computing base

The current local TCB includes the host OS and kernel, host administrator/root, Python runtime,
Forge package, runtime dependencies, Forge configuration, explicitly configured provider
executables, local filesystem and process semantics, and evaluator executables/material when
evaluator mode is used. The TCB also includes the operator's interpretation of local evidence.

Forge validates and constrains many interactions around that base, but does not eliminate trust in
those components. A local hash-valid record is not independent merely because the Python process,
filesystem, or CI runner produced it.

## Trust-boundary controls

| Boundary | Validation or authorization | Type | Evidence |
| --- | --- | --- | --- |
| User/agent -> CLI/MCP | typed input normalization, mode visibility, registry binding | prevent | `test_cli.py`, `test_mcp.py`, `test_operation_registry.py` |
| Interface -> operation registry | canonical operation ID, schema, mode/mutation policy, centralized invoke | prevent | registry binding and dispatch tests |
| Application -> domain state | lifecycle, authority, freshness, freeze, terminality checks | prevent | `test_state_machine.py`, `test_state_machine_properties.py` |
| Application -> Runner | typed `Runner` port; no application subprocess bypass | prevent/contain | `test_architecture.py`, `test_execution.py` |
| Runner -> configured executable | argument array, no shell, declared cwd/environment, stdin/output/timeout bounds, raw observation | prevent/contain/label | execution and observation tests; MNCS adapter tests |
| Project input -> observer/workspace | relative containment, traversal/symlink checks, protected overlap checks | prevent/detect | `test_config_paths.py`, `test_micro_verifiers.py` |
| Provider output -> evidence | one JSONL response, UTF-8, schema/type/status/capability validation | prevent/detect | `test_provider_protocol_adversarial.py` |
| State transition -> RecordStore | typed record construction, transactional commit and expected ledger head | prevent/recover | `test_record_store.py`, `test_recovery.py` |
| RecordStore -> history | hash chain, immutable companion matching, raw-first legacy verification | detect/recover | `test_ledger.py`, `test_compatibility.py` |
| Evaluator -> disclosure | evaluator-only operation visibility and status-only repair withholding | prevent/disclose | evaluator tests in `test_mcp.py`, `test_micro_verifiers.py` |
| Artifact -> runtime import | isolated install, origin under temporary site-packages, package-content/dependency checks | detect/label | `scripts/verify-package.py`, `test_release_engineering.py`, CI |

## Threat and control matrix

Statuses mean `controlled` when the current control is exercised and bounded, `detected-only` when
Forge can identify a condition but cannot prevent it, `accepted-local-risk` when the behavior is
explicitly trusted or outside the local authority, and `future-control-required` when stronger
architecture is needed.

| Threat | Preconditions and affected asset | Current control and type | Executable evidence | Residual status |
| --- | --- | --- | --- | --- |
| Path traversal or absolute-path escape | candidate/config input attempts `..`, absolute paths, symlink escape, or protected overlap | normalize, contain, reject symlinks and protected/writable overlap; prevent | `test_traversal_and_absolute_rejected`, `test_symlink_escape_rejected`, `test_protected_writable_overlap_rejected` | controlled |
| Caller-selected executable, argv, shell, environment, or cwd authority | caller turns verifier input into arbitrary process authority | declared commands, typed runner, no shell, architecture checks; prevent | invalid-array, shell-metacharacter, and architecture tests | controlled within configuration authority |
| Malicious configured executable | configuration authorizes hostile code | no-shell and bounds constrain I/O and duration only; no OS sandbox | runner capability and execution tests | accepted-local-risk; future sandbox required |
| Provider framing/status/capability spoofing | hostile provider emits malformed, oversized, unsupported, or deceptive data | strict bounded parsing and fail-closed errors; prevent/detect | Provider Protocol adversarial and Hypothesis tests | controlled at protocol boundary |
| Evidence status escalation | malformed, stale, missing, unsupported, or later PASS follows material FAIL | precedence/readiness separation and selection blockers; prevent | state-machine evidence tests | controlled for modeled paths |
| Lifecycle bypass or contradictory terminal state | invalid epoch/candidate/freeze/action ordering or duplicate terminal result | centralized projection, lineage, freeze, and terminality rules; prevent | `test_state_machine.py`, `test_state_machine_properties.py` | controlled for modeled transitions |
| Evaluator disclosure becomes repair feedback | evaluator detail enables same-epoch repair | evaluator-only mode and status-only disclosure; prevent/disclose | evaluator disclosure tests | controlled for declared policy; not secrecy proof |
| Freeze or authority identity drift | protected material changes after selection/freeze | identity maps and drift checks invalidate authorization; prevent/detect | freeze/evaluator drift tests and provider identity tests | controlled for observed identities |
| Store chunk/node/generation corruption or legacy-history truncation/reorder | current Store state or imported history changes | content/tree/generation verification; legacy hash-chain verification before import; detect/fail closed | Store corruption, recovery, and compatibility tests | detected-only against whole-state replacement |
| Whole-history replacement | local attacker controls filesystem/root and replaces all state | no external checkpoint or witness exists in current Forge | ledger verification tests | future-control-required |
| Transaction interruption or partial publication | crash/power loss/process exit during commit | journaled staging, expected-head binding, startup recovery, terminal UNKNOWN recovery | RecordStore/recovery failpoint and process tests | controlled for modeled failures |
| Concurrent writers or stale index | multiple writers race or derived index is corrupt | Store publication lock, generation checks, idempotency, rebuildable index | concurrent Store and index tests | controlled for tested concurrency; DoS remains |
| Process escape or incomplete child cleanup | timed-out/overflowing process creates children; platform semantics differ | POSIX process groups; Windows termination is not equivalent process-group proof | execution timeout/overflow/cleanup and Windows collector tests | partially-controlled; Windows weaker |
| Network access by local provider | configured executable uses ambient network | capability reports network isolation `not-provided`; no isolation | `test_local_runner_capabilities_are_explicit` | future-control-required |
| Filesystem access by local provider | temporary workspace is mistaken for an access-control boundary | copied/reduced workspace and path policy, but ambient host access remains | config/path/workflow tests | accepted-local-risk; future isolation required |
| Secret leakage | provider output, diagnostics, environment, or files contain secrets | omission/redaction and bounds are defensive, not complete secrecy | disclosure/redaction tests | accepted-local-risk |
| CPU/memory/disk/process/network exhaustion | hostile executable uses unbounded resource dimensions | timeout/output bounds only; no quotas or network controls | timeout/overflow tests | future-control-required |
| Dependency/wheel/import contamination | wrong checkout, missing resource, dev leak, inconsistent install | isolated artifact install, origin/content/metadata checks, `pip check`, CI | `scripts/verify-package.py`, release tests | controlled for checked cases; compromise remains TCB |
| CI/build compromise | hosted runner or build environment is compromised | repeatable development checks and artifacts, not independent authority | CI matrix and package verifier | accepted-local-risk |

## Controls not present

This iteration does not claim persistent Forge execution receipts or receipt authority; Podman,
Docker, SSH, Forge Cell, namespaces, mounts, seccomp, Landlock, cgroups, network isolation,
external checkpoints, witnesses, protected custody, independently administered evaluators, complete
resource isolation, complete secret-disclosure prevention, or protection from host root replacing
the entire unanchored history.

## Local-stability release-gate review

| Criterion | Status | Evidence or blocker |
| --- | --- | --- |
| Import-order replacement absent | satisfied | import-order hardening tests |
| Versioned records and migrations | satisfied | record/compatibility/legacy tests |
| State-machine transitions | satisfied | lifecycle and property tests |
| Transactional storage and recovery | satisfied | RecordStore/recovery/ledger suites |
| Replaceable runner boundary | satisfied | `Runner`, `LocalProcessRunner`, architecture tests |
| Adversarial Provider Protocol corpus | satisfied | malformed corpus plus Hypothesis tests |
| Adversarial subprocess corpus | satisfied | validation, bounds, timeout, shell, stdin, cleanup tests |
| Store/recovery/concurrency corpus | satisfied for the Store-backed path | chunk/node/generation mutation, journal, index, writer tests |
| Wheel/sdist install and historical-state gate | satisfied for this increment | `scripts/verify-package.py`; all 3 OS × 3 Python CI rows passed and Windows artifact logs were confirmed |
| CLI/MCP inventory stability | satisfied | operation registry and compatibility tests |
| Reviewed local threat model | this iteration | this document and linked evidence |
| External anchoring/protected custody | outstanding | future Task 8/evidence architecture |
| Identity-bound execution receipts | outstanding | later Task 7 iteration |
| Raw runner observations and MNCS adapter readiness | satisfied for this increment | observation, adapter, pinned-schema, and sibling-validator tests |
| Sandbox-capable runner | outstanding | later Task 7 iteration |

## Review limitations

This is a source-, test-, and configuration-mapped local review. Static analysis is supplemental
and cannot prove absence of dynamic bypasses, host-level attacks, secrecy, or
independence. Executable tests are authoritative only for the behavior they cover; untested paths
remain residual risk, not PASS. Each supported CI OS/Python row must be reported separately.
