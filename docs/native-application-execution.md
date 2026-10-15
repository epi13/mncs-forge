# Native application execution through Forge

`mncs-forge:native-application-execution` composes one exact
`mncs-compiler:native-application-build` Store artifact with one bounded typed
call. It accepts only a verified application-build/4 response whose selected
entry, callable interface, type closure, capability dispatch, and canonical VM
artifact are all resolved.

Forge runs the selected canonical VM under its existing local-process runner
and systemd cgroup-v2 resource envelope. The current envelope is 512 MiB, 64
processes, concurrency one, and 45 seconds. VM admission and the application
call happen inside that envelope. The provider records the complete VM result,
Compiler source and dependency identities, selected Compiler and VM
identities, exact input call, Forge execution receipt, and measured resource
observations.

Environment passes the build artifact only through its private
`provider-input-artifacts/1` workspace. The provider verifies the Store
reference metadata, byte count, and SHA-256 before reading the application
artifact. Calls use canonical JSON of the form `{"arguments": [...]}` and are
limited to 32 KiB. The callable target comes from the verified Compiler entry;
callers cannot choose another function or provide VM command arguments.

Successful invocations publish `native-application-execution.json` and
`forge-execution-receipt.json` as shareable capability artifacts. Failed,
unsupported, timed out, or resource-limited runs publish a failure record and
do not retain a prior successful result as the latest output. Environment
persists the invocation artifact bundle through the canonical Store and its
artifact-read operation verifies the exact capability binding and path.

The host code owns only Store-byte validation, process dispatch, Forge
admission, and receipt transport. Application decisions and returned values
remain in MNCS code executed by the canonical VM.
