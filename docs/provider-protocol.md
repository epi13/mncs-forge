# Provider Protocol integration

Forge uses MNCS Provider Protocol 0.1 rather than defining another analyzer protocol. A provider
command must be declared both in `[[providers]]` and in its workflow. Forge sends one bounded JSON
Lines request and accepts exactly one response line. It preserves protocol version, provider
identity, method, status, compact witnesses, limitations, unsupported constructs, duration,
bounded stderr diagnostics, and output identity.

Capabilities and health are protocol concepts. Forge does not automatically execute them during
ordinary inspection. Timeouts terminate the provider process group where practical. Provider
stderr never enters protocol stdout framing. Public MNCS `provider inspect`, `provider run`, and
`provider verify-result` commands remain available for independent verification.

`providers probe` sends a capabilities request only after executable containment, availability,
and optional pinned-identity checks. The response must be one recognized capabilities object with
provider name/id, identity/version, analyses, statuses, cancellation, health-check support, and
well-formed extensions. Exit zero with text, malformed JSON, the wrong response type, timeout,
output overflow, or identity drift remains UNKNOWN.

Capability blockers are satisfied only by a current successful probe whose returned analyses
include the required capability and do not mark it unsupported. Declared capability without a
current probe is not PASS. In the native Forge path, the MNCS provider policy owns capability
membership, probe qualification, and required/optional provider disposition. The host adapter
encodes capability names as opaque digests and streams the full fact lists in bounded chunks; it
retains probe execution, provider inventory, and response presentation. Explicit native-off mode
continues to use the compatibility classifier.

Micro-verifiers use the same `analysis_request`/`analysis_response` framing. The declared verifier
method must appear in the referenced provider's configured capabilities and becomes the request
`analysis`. Forge adds bounded component data and identities under the `mncs_forge` extension.
Providers may return assumptions and a dependency envelope there. Operational protocol failure is
recorded as `UNKNOWN`; process completion is never converted to `PASS`. See
[Machine-native micro-verifiers](micro-verifiers.md).

The `0.1.0b1` boundary freezes the existing capabilities, workflow-analysis, and verifier-analysis
request envelopes with executable semantic assertions. It does not add a protocol version or make
response prose contractual. Protocol `"0.1"`, request types/IDs, analyses, component/limit fields,
and the bounded `mncs_forge` extension shapes are compatibility-sensitive; timestamps and generated
request-ID values are not.
