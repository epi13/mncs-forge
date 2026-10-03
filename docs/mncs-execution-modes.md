# Typed MNCS execution modes

Forge owns execution mechanics for family work; consumers declare intent.
Two typed modes exist today over the same bounded execution owner:

```text
test      test-mode work (mncs-test invocation shapes)
observe   observation-mode work (record-equivalent mncs-debug observation)
```

Both modes run through the same `Runner`, produce the same execution
records (command identity, termination, duration, runner identity), and
return typed results plus exact provenance. The mode selects policy
(invocation shape, defaults, bindings), never a parallel execution stack.

## Test mode

`MncsDevelopmentService.failure_loop` executes test-mode work. The runner
policy is explicit per invocation through `test_runner_mode`:

- `auto` (default): `[python, *.py]` prefixes, `*.py` runners, and the
  `mncs-test-compat` adapter use the legacy `run --manifest` shape; a
  direct native executable (`mncs-test`) uses the native shape.
- `native` / `legacy`: force one shape (for renamed binaries).

Native shape (mirrors Actions' native branch):

```text
<runner> <source> --step-budget N --result R --check-result C
    --artifacts A --format json [--verification-plan P] [--library L ...]
```

with `source`, `step_budget`, and `libraries` projected from the
`mncs.test-manifest/1` manifest (absolute-or-manifest-relative paths, as
the legacy runner resolves them). The declared `mncs_binary` is bound as
an explicit `MNCS` environment entry for the invocation; the native CLI
has no `--mncs` / `--embed-library` flags.

Legacy shape (unchanged):

```text
<prefix> run --manifest M --result R --check-result C --artifacts A
    [--mncs B] [--library L ...] [--embed-library E] [--verification-plan P]
```

Both shapes produce the same validated `mncs.test-result/1` /
`mncs.check-result/1` envelopes. The selected policy is recorded on each
execution record as `execution_mode` (`test-native` / `test-legacy`).

## Observe mode

`development.mncs.observe` (`mncs observe`, `mncs_forge_mncs_observe`)
executes one record-equivalent observation under Forge ownership. Debug
submits program, request, and capture policy; Forge owns the working
directory, allowlisted environment, deadline, bounded output, witness
validation, and provenance.

Defaults match `mncs-debug record` (`bounded` capture, 512/1024/4096
bounds, 30s deadline) so local and Forge-submitted observations differ
only in execution ownership, never in capture semantics.

Inputs (`program`, `request`, `core_path`, `test_result`) are explicit
caller declarations: absolute paths are accepted as declared, relative
paths stay contained in the Forge project. Forge writes (witness,
observation record) stay contained. The inner record entry is always
invoked with `--executor local`: the outer execution is already
Forge-owned, so a nested Forge submission would recurse.

Result (`mncs.forge-observation/1`):

- `witness`: witness identity projection (witness/execution/observation
  identities, failure class, artifact ref).
- `witness_document`: the full validated witness (typed result; consumers
  do not re-read the file to understand the observation).
- `execution`: Forge execution record with `execution_mode: observe`.
- `provenance`: program/request digests, toolchain binding, capture policy.

## Consumer contract

- Test describes test work; Forge selects and executes the invocation.
- Debug describes observation work (`record` / `import-test` with
  `--executor forge --forge-config ...`); Forge executes and returns the
  witness with Forge execution identity.
- New consumers add a mode (policy over this owner), not an executor.

See `MNCS-TEST-P-013` (native test invocation) and `MNCS-DEBUG-P-016`
(Forge-owned observation execution) for the originating pressures.
