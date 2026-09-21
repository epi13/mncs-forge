# Evidence and identity model

Forge uses `forge-tree-sha256-v1` for a sorted, length-delimited local file inventory and
`forge-json-sha256-v1` for Forge-owned deterministic JSON. These names deliberately do not claim
MNCS RFC 8785 canonical-document identity. Public MNCS commands should produce or verify MNCS
canonical identities.

Every result binds candidate and provider/evaluator identities, method, scope, environment-key
names (never values), duration, `PASS`/`FAIL`/`UNKNOWN`, compact witnesses, limitations,
unsupported constructs, and an output identity. Ledger entries bind sequence, time, kind,
previous hash, payload, and current hash in the derived Forge projection. Current authority is the
Store generation; the historical chain is verified only before migration.

New persistent records use schema version `"1"` and include their stable record type. Current
record-derived identities include that metadata through an explicit type-specific projection.
Candidate identity remains a project-content identity rather than a record hash. Legacy
unversioned identities retain their historical projections. Raw legacy ledger linkage is verified
before normalization; migration never redefines the old chain. See
[Versioned Forge records](record-schemas.md).

Reconciliation is workflow aggregation, not copied certification logic. It reports required-gate
effects, conflicts, stale identities, limitations, and blockers using
`FAIL > UNKNOWN > PASS`. MNCS implementation and MNCDS development-process results remain separate
offline-validator outputs.

Micro-verifier actions and results use the same immutable Store state and derived Forge history
projection. They additionally bind
verifier/configuration/policy/environment identities, exact bounded input identities, provider
response identity, and a provider-declared dependency envelope. Freshness is `CURRENT`, `STALE`,
or `UNKNOWN` lineage metadata and is not a conformance status. No verifier result cache is enabled
in the initial implementation.

## Execution observations and identity-bound receipts

`LocalProcessRunner.run()` / `observe()` expose raw, bounded observations. Complete runs report
command/environment identities, lifecycle and termination facts, bounded stream counts and
SHA-256 values, aggregate output, wall duration, runner capabilities, and host/placement facts
when known. Timeout, start-failure, and output-limit observations retain only what was observed;
unknown totals or complete hashes remain unknown rather than being inferred.

`mncs_forge.mncs_execution_receipt` still adapts a complete observation plus caller-supplied
subject, bundle, policy, challenge, and optional placement identities to MNCS's experimental
`mncs-execution-receipt` / `0.1-experimental` envelope. It uses the pinned MNCS schema at commit
`6d6380016e174feaaa774c1cf0931095d24b5280` (schema SHA-256
`f2e1860405052a40b100bead7c27dbe0cc3ac11d03dccca3fcb643b350ecab6e`). The adapter fails closed
when required context or complete stream totals are absent. All MNCS claim-boundary fields remain
`not-asserted`.

Task 7B-2 persists a separate Forge `execution_receipt_binding` record. That record is the
authoritative Forge linkage: action, project, epoch, candidate, result, runner, worker/host, and
completeness. The MNCS envelope is stored only as a referenced companion when the observation is
complete enough to build it. Binding `status` is never `PASS`; complete local provenance remains
`UNKNOWN` as evidence. Explicit `established_properties` keep execution completion, runner bounds,
isolation, same-operator execution, witnessing, custody, independence, and certification as
separate dimensions.

A Fabric-shaped execution record can be translated into the same observation type. Fabric
placement does not change Forge evidence semantics. Same-operator remote execution remains
same-operator evidence.
