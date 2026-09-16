#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
set -euo pipefail

repository_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
server="$repository_root/.venv/bin/mncs-forge-server"
existing="$(mktemp)"
trap 'rm -f "$existing"' EXIT

if ! codex mcp get mncs-forge --json >"$existing" 2>/dev/null; then
  echo "MNCS Forge is not registered."
  exit 0
fi
if ! python3 - "$existing" "$server" <<'PY'
import json
import sys
value = json.load(open(sys.argv[1], encoding="utf-8"))
raise SystemExit(0 if value.get("transport", {}).get("command") == sys.argv[2] else 1)
PY
then
  echo "Refusing to remove mncs-forge: it belongs to another checkout." >&2
  exit 3
fi
codex mcp remove mncs-forge
echo "Removed the MNCS Forge Codex MCP registration; local files and state were retained."
