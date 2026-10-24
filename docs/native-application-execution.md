# Native application execution

The `native-application-execution` provider consumes a verified
`mncs.compiler.application-build/4`, `/11`, or `/16` artifact from Environment,
admits its Compiler-generated VM artifact, runs one typed call through the
canonical VM, and publishes a Forge receipt plus an Environment result artifact.

## Bounded structured-file capability

A call may include one `capability_providers` entry using
`mncs.forge.structured-file-capability/1`:

```json
{
  "arguments": ["... typed ApplicationContext value ..."],
  "capability_providers": [{
    "schema_version": "mncs.forge.structured-file-capability/1",
    "capability_identity": "... exact identity from the admitted Compiler artifact ...",
    "input_schema": "mncs.test-digest-request/1",
    "input_value": {"record": {"...": "typed ExecutionValue fields"}},
    "output_schema": "mncs.test-digest/1"
  }]
}
```

The callable must accept the verified `ApplicationContext` record and the
selected Compiler dispatch must bind the exact capability identity to one
source alias with `structured_read` and `structured_write`. Its `argv` values
must initially be safe, relative file names, and `argv_count` must match the
request plus declared result paths. `/1` supports one result schema; `/2`
supports up to four distinct result schemas. Forge stages the typed request in
a private invocation directory and replaces those paths with the staged
request and create-only result paths. The application receives no arbitrary
host path from this adapter.

An application may also bind one `mncs.forge.structured-digest-capability/1`
entry with the exact source capability identity. Forge verifies its dispatch
alias and the admitted artifact requirement, then gives the VM a
`mncs.vm.structured-digest-provider/1` binding scoped to that artifact. The VM
uses the operand type from selected SSA and delegates canonical projection and
SHA-256 to Language. Forge checks that the execution witness records completed
`structured_digest` effects for the admitted alias.

The call envelope is limited to 32 KiB. The VM file adapter has a 2 MiB read
ceiling; Forge currently admits no more than 256 KiB for the typed result so
the wrapped Environment artifact remains under its 512 KiB publication limit.
For calls that admit this provider, Forge emits
`mncs.forge.native-application-execution/2`; the existing no-provider response
keeps `/1`. The version 2 result includes the typed output, its schema, and its
byte digest in `native-application-execution.json`. Calls using structured
digest emit `/3` with the completed capability effect summary; the canonical
VM execution witness retains each exact request and response. Forge also
records the measured execution receipt. Application decisions stay in MNCS
source; Forge and the VM provide only the declared transport and Language
runtime primitive.
