# Getting started

MNCS Forge currently supports Python 3.11 through 3.13 and runs as a local CLI or stdio MCP
server. Ordinary operation does not require network access, but configured providers and workflows
run with the permissions of their selected runner. The current local runner is not an OS sandbox.

## Install for development

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e '.[dev]'
```

On Windows PowerShell, activate the environment with:

```powershell
.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
```

Confirm the installation:

```bash
mncs-forge --help
mncs-forge-server --help
```

## Inspect the minimal example

The committed minimal example is safe for learning the interface and exercising the local record
lifecycle.

```bash
CONFIG="$PWD/examples/minimal/mncs-forge.toml"
mncs-forge --config "$CONFIG" config validate
mncs-forge --config "$CONFIG" doctor
mncs-forge --config "$CONFIG" inspect
mncs-forge --config "$CONFIG" providers list
mncs-forge --config "$CONFIG" verifier list
```

Inspection and verifier matching do not execute providers. Provider execution occurs only through
an explicit probe, declared development check, verifier run, or evaluator operation.

## Minimal controlled development flow

Begin an epoch with explicit identities:

```bash
mncs-forge --config "$CONFIG" epoch begin \
  --generator local-generator-v1 \
  --evaluator local-evaluator-v1
```

Register a candidate after modifying only paths declared writable by the configuration:

```bash
mncs-forge --config "$CONFIG" candidate register \
  --changed candidate/generated.py \
  --hypothesis "minimal controlled candidate" \
  --generator local-generator-v1 \
  --generator-config local-generator-config-v1
```

Use `mncs-forge --config "$CONFIG" inspect` to retrieve the resulting candidate identity. Then run
only the checks or verifiers declared by the project configuration. A local `PASS` covers only the
bounded declared operation; it does not establish MNCS or MNCDS conformance, independent
evaluation, protected custody, or promotion.

## Register Forge with Codex

For an MNCS checkout containing a committed `mncs-forge.toml`:

```bash
./scripts/install-codex-mcp.sh \
  /absolute/path/to/machine-native-complexity-standard/mncs-forge.toml
./scripts/verify-codex-mcp.sh \
  /absolute/path/to/machine-native-complexity-standard/mncs-forge.toml
```

Start a new Codex session after registration. An already-running MCP client is not assumed to
reload server configuration dynamically. Remove the registration with
`./scripts/uninstall-codex-mcp.sh`.

## Run the project checks

```bash
./scripts/check.sh
```

The check script runs formatting verification, linting, strict type checking, the test suite,
package building, and `git diff --check`. CI repeats the core checks across Linux, macOS, and
Windows on supported Python versions.

## Continue reading

- [Configuration](configuration.md)
- [Architecture and trust boundaries](architecture.md)
- [Security model](security.md)
- [CLI and MCP interfaces](interfaces.md)
- [Codex implementation queue](codex-next-steps.md)
