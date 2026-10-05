# Compiler/runtime composition

Forge's pure MNCS application transport accepts exact `MNCS_COMPILER_CHECKOUT`,
`MNCS_COMPILER_PROBE`, `MNCS_VM_CHECKOUT`, `MNCS_VM_BIN`, and an explicit
`MNCS_VM_ARTIFACT_CACHE`. Environment binds these through `canonical-vm-work/1`.
`CanonicalVmApplication` uses the selected compiler's direct provider and selected
VM client, retains one admitted VM process across calls, and reopens when producer
inputs change. Incomplete/stale composition refuses; it never retries on a
research-bytecode backend.

The real `forge/core.mncs::lifecycle_initial` kernel agrees with the pinned
reference and executes repeatedly in one VM process. Native assurance/readiness/
lifecycle semantics remain in MNCS. Python owns filesystem/process/receipt
transport only. No universal `NATIVE_BACKEND` remains.

Language reference/embed execution is still an explicit lane for host grants and
bootstrap/reference consumers. `STAGE0_EMBED_BACKEND` names that limited boundary.
The effect-free VM stdio service has no selected process/filesystem capability
providers, so Forge does not silently invent grants or reroute effects. Its
migration condition is actual VM provider binding for each required effect,
followed by differential evidence. Other targets remain explicit.

Resident identity now includes optional compiler/VM selections, exact executable
and transport bytes, and the artifact cache location. Unrecorded ambient selectors
are removed from the resident child environment. A changed selection closes the
old retained application; existing ownership/birth checks and warm paths remain.
The live Language Service campaign was not attached or changed for this proof.

Caches remain local hot indexes around content-addressed reusable products. An
explicit shared cache can deduplicate immutable compiler output without a Store
round trip or shared mutable VM state. Existing Stage-0 hot caches were measured
and preserved. Prebuilt Language/VM build origin remains a separate unresolved
pressure (`MNCS-TOOLING-9E7E149C94D9`); exact executable hashes are not attestation.
