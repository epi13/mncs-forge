# Local compiler scratch lifecycle

The Stage-0 Forge-core compiler uses a private `.mncs-native-artifact-*`
directory in the selected project root. This keeps file-only compiler outputs
visible through the runner's project mount. Normal return and exceptions
remove the directory while its process lease remains held.

A small owner marker binds the allocation to its filesystem identity and its
physical parent directory. The allocator and orphan collector serialize with
a lock on that parent directory; no extra coordination file is left in the
project. Each allocation holds a separate file lease through compilation and
cleanup. Operating-system process exit releases a lease after an abrupt kill.

Before a later compilation, Forge removes recognized workspaces whose lease
is free and which have no observable surviving compiler child or file user.
The random directory component is checked across process argv and file paths,
including Control mount aliases. An incomplete command-line observation
preserves the directory. This is local filesystem/process recovery, not a
distributed worker fence.

Unmarked historical directories, changed filesystem identities, symlinks,
tracked files and unexpected additional contents are preserved. Historical
unmarked scratch requires a separate audited cleanup. These rules own only
temporary compiler outputs; they do not delete source, evidence, candidates,
canonical artifacts or resident execution state, and do not change assurance
decisions. The existing compiler invocation continues to use `--quiet` and
`--omit-result` so successful compilation does not duplicate a large result
document in the output directory or captured stdout.
