# Resident provider contract

Forge publishes `resident-status`, `resident-reconcile`, and `resident-work`
in `.mncs/project.json`. Their invocation descriptors address
checkout-owned Python entrypoints and the explicitly selected Language Service
host. Source fingerprints remain evidence, never invocation addressing.

The consumer supplies the exact selected `--workspace` root, or an explicit
absolute `--config`. When both are supplied, the workspace must agree with the
configuration's authoritative project root. Workspace-only selection uses
Forge's bounded, fail-closed configuration resolver and never falls back to the
current directory. Environment resolves the workspace path against its
selected repository bindings.

## Selection and observations

The provider requires selected absolute `MNCS_LANGUAGE_ROOT`, `MNCS_BIN`,
`MNCS_EMBED_LIB`, `MNCS_STORE_ROOT`, and `MNCS_LANGUAGE_SERVICE_HOST` paths.
Environment supplies the Language/Store runtime and resolves the host through
Forge's descriptor. Missing selections fail with `RESIDENT_BINDING_MISSING`.
No PATH compiler, installed Forge package, sibling Store package, configured
ambient Language Service command, or native-mode override substitutes for them.

Environment can additionally pass `MNLS_SERVICE_SOCKET`,
`MNLS_SERVICE_STREAM_IDENTITY`, `MNLS_SERVICE_WORKSPACE_ROOT`, and
`MNLS_SERVICE_REPOSITORY_ROOTS_JSON` from a ready selected LS provider
observation. Forge includes these values in its resident identity and verifies
the live socket reports the same root and stream and that the selected
repository set contains the Forge workspace. With this composition, Forge
attaches to the Environment-owned stream and never starts a second LS. If the
selected stream changes, Forge fails closed and must be reconciled against the
new identity.

The immutable startup identity contains Forge checkout and observed HEAD,
project/configuration identity, runtime paths, and compiler/embed/host artifact
SHA256s. `selected` is the current request; `observed` is the live resident's
startup identity. A checkout HEAD is not proof of the compiler's build origin.
The startup identity is checked again after native Forge initialization.

## Status

`resident-status` declares only read effects and emits
`mncs.forge.resident-status/1`. It creates no directories, locks, processes,
Store transactions, or lease repairs. It reads at most 8192 lease/transport
bytes, uses a 0.35-second total challenge deadline, a 0.75-second Language
Service probe, and a 0.5-second Git observation. The CLI enforces a total
2.5-second operation deadline, including configuration and artifact reads, and
8192-byte result limit. Exceeding either returns a structured
`RESIDENT_DEADLINE` or `RESIDENT_OUTPUT_LIMIT` diagnostic. Environment
additionally enforces its three-second/16384-byte invocation budget.

`ready` requires a Linux process birth identity, a live nonce challenge to the
actual supervisor, matching provider/runtime/configuration/instance identity,
and an attached current Language Service event stream. Forge either verifies
its own selected LS process lease or the exact externally selected Environment
endpoint above. File/executable/PID presence is insufficient.
The resident initializes the existing native Forge and real Store before it
can become ready. Status also reports stopped, starting, stale, incompatible,
degraded, failed, or blocked, with stable diagnostic codes and recovery context.

## Reconciliation and control

`resident-reconcile` declares execute effects. It uses the same nonblocking
project lifecycle lock as other Forge lifecycle operations, starts the existing
continuous host asynchronously, and returns
`mncs.forge.resident-reconciliation/1`. Operations include started, reused,
stopping, blocked, and busy. Consumers verify the result with another status
probe. Initialization may span multiple entry attempts; a starting owned
instance is reused within the configured startup deadline.

Changed bindings for the same owning checkout/project are stopped before
replacement. Another checkout's healthy resident is incompatible and is never
adopted or controlled. Malformed leases fail closed; live legacy PID-only
leases require recovery through their owning lifecycle. Reconciliation does
not silently upgrade ownership based on cmdline guesses.

Process control pins a Linux pidfd, verifies boot/start identity, and signals
that descriptor. PID recycling between observation and signaling cannot target
a replacement process. Linux `/proc`, abstract Unix sockets, and pidfds are the
current platform boundary; unsupported control remains explicit.

`resident-reconcile --stop` stops the selected supervisor. Adding
`--include-language-service` also stops its owned selected service, including
an owned service left behind after the supervisor exited. It does not stop
another binding's service. Long default Language socket paths
use a stable short private directory derived from project identity; an explicit
socket setting remains explicit. The supervisor uses the same resolved path.

## Work and authority

`resident-work` pins the selected runtime/package closure and exposes the
existing Forge CLI (`--config ... doctor`, `epoch begin`, candidate operations,
and other declared Forge workflow operations). It declares write effects
because workflow operations can publish Store records. Native Forge remains
the assurance authority; this transport adds no host assurance policy.

Environment owns selection, claims/effect admission, sequencing, persistence,
and readiness projection. It contains no Forge commands or process rules.
An unowned campaign checkout requires an explicit claim before startup; blocked
authority never becomes implicit permission. Forge owns status/lifecycle and
the surrounding native assurance and continuous supervision machinery.

Pure Forge applications use the selected compiler and canonical VM. The
resource-budget policy currently uses an explicit Stage-0/embed fallback only
when the VM reports one of the known float instruction gaps; that fallback is
recorded with the canonical artifact, producer/runtime receipt, Stage-0
artifact identity, and selected target. Other VM `unsupported` outcomes remain
errors and do not silently switch runtimes.

## Evidence

`tests/test_resident_provider.py` covers live challenge/stream checks, read-only
status, corrupt/legacy/stale leases, checkout changes, repeated reconciliation,
selected imports, long socket paths, and pidfd control. Environment's
`scripts/forge_resident_proof.py` exercises real providers, native tests, Store,
fresh/reused entry, recovery, descriptor corruption, and handoff.
