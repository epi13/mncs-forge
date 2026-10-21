# Rights & Provenance in Forge

Status: experimental integration of `mncs-rights-provenance` manifest contract `0.2.0`.

Forge is the first operational consumer of the MNCS Rights & Provenance framework because Forge already owns candidate lineage, declared development/evidence workflows, selection boundaries, and evidence reconciliation. This integration deliberately does **not** move legal authority into Forge.

## Boundary

The intended family split is:

```text
mncs-rights-provenance   defines vocabulary and versioned manifest semantics
          |
          v
        Forge            collects, binds, assesses, and applies configured process policy
       /     \
      v       v
  Fabric     Commons     execution facts / institutional findings and decisions
      \       /
       v     v
     validators          bounded validation under their declared authority
```

Forge may establish facts such as:

- a manifest conforms to the pinned schema;
- a manifest is bound to the candidate being evaluated;
- provenance validation is passed, failed, incomplete, or not run;
- a declared rights basis is present or explicitly unresolved;
- third-party sources and their declared license status are present, missing, incompatible, or unknown;
- a candidate has Fabric execution-receipt bindings;
- a project configured observe, advisory, or enforced treatment of this evidence.

Forge does **not** establish:

- copyrightability;
- legal authorship or ownership;
- non-infringement;
- public-domain status merely from machine origin;
- legal compatibility beyond the evidence declared to it;
- certification or legal clearance.

Every assessment therefore emits `legal_conclusion: NOT_MADE` and retains explicit limitations.

## Configuration

Rights/provenance is opt-in and non-blocking by default:

```toml
[rights_provenance]
mode = "observe"
manifest = "evidence/rights-provenance.json"
```

`manifest` is optional. When absent, Forge derives a conservative schema-valid draft from the candidate and execution facts it actually knows. Unknown facts remain unknown.

### `observe`

Default. Forge reports rights/provenance alongside technical evidence and records a selection-time snapshot, but it never changes selection eligibility.

### `advisory`

Forge reports `REVIEW_REQUIRED` when the rights/provenance evidence domain is not PASS. It still does not block candidate selection.

### `enforced`

An explicit project policy. Candidate selection is blocked when the rights/provenance evidence domain is FAIL or UNKNOWN. A PASS means only that the configured evidence requirements represented by this integration are complete; it is **not** a legal clearance result.

## Existing Forge surfaces

This first integration intentionally uses existing Forge inspection surfaces rather than introducing a parallel control plane.

`mncs-forge status` / `mncs_forge_claim_status` now returns a separate `rights_provenance` projection.

`mncs-forge blockers rights_provenance` / `mncs_forge_claim_blockers` reports rights/provenance-specific unresolved evidence. Promotion blockers include this domain only when `mode = "enforced"`.

`evidence.reconcile` retains technical/development `required_gate_aggregation` unchanged and places the rights/provenance projection in the reconciliation record's extensions. This prevents a technical PASS from silently becoming a rights PASS or vice versa.

Candidate comparison reports rights/provenance beside each candidate's technical evidence. When a candidate is selected, its current rights/provenance assessment is retained in the candidate-disposition record's `extensions` field.

## Contract handling

Forge packages a pinned copy of the `mncs-rights-provenance` `0.1.0` manifest schema. The package identifies the upstream contract as:

```text
mncs-rights-provenance/manifest@0.1.0
```

The bundled schema is an interoperability contract, not a fork of the specification. Semantic changes belong in `mncs-rights-provenance`; Forge should update its pinned version deliberately after compatibility review.

## Fabric evidence

When Forge derives a draft manifest it incorporates existing candidate-bound execution receipt identities as `fabric-receipt` process evidence. Fabric remains the authority for what execution occurred; Forge does not reinterpret a Fabric receipt as conformance, independence, protected custody, or legal provenance.

## License evidence scan

`rights.license-evidence.scan` emits the standalone `LicenseEvidence` record
defined by `mncs-forge-license-evidence/0.3.0`. It inspects only bounded,
project-root metadata and conventional license/notice filenames; it does not
walk dependencies or claim that the inspected set is complete.

Every inspected input appears in bounded `scan_inputs` as observed, absent,
or unavailable. An observed input includes a project-relative path, byte
length, and SHA-256. Declaration and notice observations also carry a
repository-relative `source_ref` with a field selector. The stable
`evidence_id` is derived from the subject and this scan state, so changing a
file changes the identity even when a keyword heuristic still recognizes the
same license label. Scan timestamps remain in the record's content digest but
do not affect that stable source identity. Full source files and absolute
checkout paths are not copied into the evidence record.

Metadata values are retained as declarations, without SPDX syntax validation
or authority claims. Text matching produces only a `heuristic` claim. Missing,
malformed, oversized, unsafe, or unrecognized inputs remain unresolved and
include actionable source references when bytes were available. The scan does
not determine license compatibility, copyright ownership, or legal effect.

## Commons integration

This PR does not modify Commons. Rights/provenance findings that outlive one candidate assessment—decisions, open questions, hypotheses, failed approaches, legal-review needs, or policy changes—are expected to be published to Commons through a future bounded integration. Publication will not make those records true or authoritative by itself.

## Why no new core Forge record type yet?

The rights/provenance specification is Incubating. Forge therefore consumes it through a versioned manifest and stores selection-time assessments in the already-supported extension mechanism instead of prematurely hard-coding a new persistent core record type.

Once experiment evidence shows the correct granularity and lifecycle, a future compatibility-reviewed change can introduce a dedicated immutable record if that is justified.

## Central invariant

> Technical status and rights/provenance status are independent evidence domains. Missing or unresolved rights evidence remains UNKNOWN; machine origin never manufactures a legal conclusion; policy determines whether that UNKNOWN is observed, advisory, or blocking.
