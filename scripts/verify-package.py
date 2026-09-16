#!/usr/bin/env python3
"""Build and verify Forge sdist/wheel artifacts in clean environments.

This is a local-development/release-engineering check. It does not establish conformance,
independence, custody, witnessing, certification, promotion, or sandbox assurance.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import textwrap
import zipfile
from email.parser import Parser
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
RUNTIME_DEPENDENCIES = {"filelock", "jsonschema", "mcp"}
DEV_ONLY_NAMES = {"hypothesis", "pytest", "pytest-cov", "mypy", "ruff", "build"}
EXPECTED_PYTHON = ">=3.11"
REQUIRED_WHEEL_FILES = {
    "mncs_forge/__init__.py",
    "mncs_forge/cli.py",
    "mncs_forge/server.py",
    "mncs_forge/resources/forge-records-1.schema.json",
    "mncs_forge/resources/mncs-forge-config.schema.json",
    "mncs_forge/resources/usage.md",
    "mncs_forge/resources/native/forge/core.mncs",
    "mncs_forge/resources/native/forge/identity.mncs",
    "mncs_forge/resources/native/forge/lifecycle.mncs",
    "mncs_forge/resources/native/forge/reconciliation.mncs",
    "mncs_forge/resources/native/forge/records.mncs",
    "mncs_forge/resources/native/forge/serialization.mncs",
}
FORBIDDEN_PARTS = {
    ".coverage",
    ".git",
    ".mncs-forge",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "ledger.jsonl",
    "transactions",
    "workspace",
}


def command_path(venv: Path, name: str) -> Path:
    directory = venv / ("Scripts" if os.name == "nt" else "bin")
    candidates = [directory / name]
    if os.name == "nt":
        candidates.extend(directory / f"{name}{suffix}" for suffix in (".exe", "-script.py"))
    for candidate in candidates:
        if candidate.exists():
            return candidate
    raise RuntimeError(f"cannot find {name} in {directory}")


def run(
    command: list[str | Path], *, cwd: Path, env: dict[str, str], timeout: int = 120
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        [str(item) for item in command],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"command failed ({completed.returncode}): {' '.join(str(item) for item in command)}\n"
            f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
        )
    return completed


def clean_environment(*, venv: Path) -> dict[str, str]:
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    environment.pop("PYTHONHOME", None)
    bindir = command_path(venv, "python").parent
    environment["PATH"] = str(bindir) + os.pathsep + environment.get("PATH", "")
    environment["VIRTUAL_ENV"] = str(venv)
    return environment


def build_artifacts(python: Path, artifact_dir: Path) -> None:
    artifact_dir.mkdir(parents=True, exist_ok=True)
    run(
        [python, "-m", "build", "--wheel", "--sdist", "--outdir", artifact_dir],
        cwd=ROOT,
        env=os.environ.copy(),
        timeout=180,
    )


def find_artifact(artifact_dir: Path, suffix: str) -> Path:
    artifacts = sorted(artifact_dir.glob(f"*{suffix}"))
    if len(artifacts) != 1:
        raise RuntimeError(f"expected exactly one {suffix} in {artifact_dir}, found {artifacts}")
    return artifacts[0]


def safe_members(names: list[str]) -> None:
    for name in names:
        parts = Path(name).parts
        if any(part in FORBIDDEN_PARTS or part.startswith(".") for part in parts):
            raise RuntimeError(f"artifact contains an unintended path: {name}")
        if ".." in parts:
            raise RuntimeError(f"artifact contains a traversal path: {name}")


def audit_wheel(wheel: Path) -> dict[str, Any]:
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        safe_members(names)
        name_set = set(names)
        missing = sorted(REQUIRED_WHEEL_FILES - name_set)
        if missing:
            raise RuntimeError(f"wheel is missing required files: {', '.join(missing)}")
        dist_info = sorted(name for name in names if name.endswith(".dist-info/METADATA"))
        if len(dist_info) != 1:
            raise RuntimeError("wheel must contain exactly one METADATA file")
        metadata = Parser().parsestr(archive.read(dist_info[0]).decode("utf-8"))
        if metadata.get("Requires-Python") != EXPECTED_PYTHON:
            raise RuntimeError("wheel Python-version metadata is incorrect")
        optional_extras = set(metadata.get_all("Provides-Extra", []))
        if "dev" not in optional_extras:
            raise RuntimeError("wheel is missing the development optional extra")
        entry_points_name = dist_info[0].removesuffix("METADATA") + "entry_points.txt"
        entry_points = archive.read(entry_points_name).decode("utf-8")
        expected_entries = {
            "mncs-forge = mncs_forge.cli:main",
            "mncs-forge-server = mncs_forge.server:main",
            "mncs-forge-mcp = mncs_forge.server:main",
        }
        if not expected_entries.issubset(set(entry_points.splitlines())):
            raise RuntimeError("wheel console entry points are incomplete")
        license_files = [name for name in names if name.endswith("/licenses/LICENSE")]
        if len(license_files) != 1:
            raise RuntimeError("wheel must contain the packaged license file")
        required_distributions = {
            re.split(r"[<>=!~; \[]", str(item), maxsplit=1)[0].lower().replace("_", "-")
            for item in metadata.get_all("Requires-Dist", [])
            if "extra ==" not in str(item)
        }
        if not RUNTIME_DEPENDENCIES.issubset(required_distributions):
            raise RuntimeError("wheel runtime dependency metadata is incomplete")
        leaked = sorted(required_distributions.intersection(DEV_ONLY_NAMES))
        if leaked:
            raise RuntimeError(f"development dependencies leaked into wheel metadata: {leaked}")
        return {
            "file_count": len(names),
            "metadata_name": metadata.get("Name"),
            "metadata_version": metadata.get("Version"),
            "requires_python": metadata.get("Requires-Python"),
            "optional_extras": sorted(optional_extras),
            "entry_points": sorted(expected_entries),
            "license": license_files[0],
            "runtime_dependencies": sorted(required_distributions),
        }


def audit_sdist(sdist: Path) -> dict[str, Any]:
    with tarfile.open(sdist, "r:gz") as archive:
        names = archive.getnames()
        safe_members(names)
        relative = {Path(name).relative_to(Path(names[0]).parts[0]).as_posix() for name in names}
        required = {
            "LICENSE",
            "README.md",
            "pyproject.toml",
            "src/mncs_forge/resources/forge-records-1.schema.json",
            "src/mncs_forge/resources/mncs-forge-config.schema.json",
            "src/mncs_forge/resources/native/forge/core.mncs",
            "src/mncs_forge/resources/native/forge/identity.mncs",
            "src/mncs_forge/resources/native/forge/lifecycle.mncs",
            "src/mncs_forge/resources/native/forge/reconciliation.mncs",
            "src/mncs_forge/resources/native/forge/records.mncs",
            "src/mncs_forge/resources/native/forge/serialization.mncs",
        }
        missing = sorted(required - relative)
        if missing:
            raise RuntimeError(f"sdist is missing required files: {', '.join(missing)}")
        return {"file_count": len(names), "required_files": sorted(required)}


def module_origin(python: Path, *, cwd: Path, env: dict[str, str]) -> dict[str, str]:
    code = (
        "import json, site, sys, sysconfig, mncs_forge; "
        "from importlib.resources import files; "
        "native_root = files('mncs_forge.resources').joinpath('native', 'forge'); "
        "native_names = ("
        "'core.mncs', 'identity.mncs', 'lifecycle.mncs', 'reconciliation.mncs', "
        "'records.mncs', 'serialization.mncs'); "
        "print(json.dumps({'module': mncs_forge.__file__, "
        "'purelib': sysconfig.get_paths()['purelib'], "
        "'site': site.getsitepackages()[0], 'python': sys.executable, "
        "'native_modules': all(native_root.joinpath(name).is_file() "
        "for name in native_names)}, sort_keys=True))"
    )
    result = json.loads(run([python, "-c", code], cwd=cwd, env=env).stdout)
    if not isinstance(result, dict):
        raise RuntimeError("installed import-origin probe returned invalid JSON")
    module = Path(str(result["module"])).resolve()
    site_packages = Path(str(result["purelib"])).resolve()
    if site_packages not in module.parents:
        raise RuntimeError(f"mncs_forge imported outside site-packages: {module}")
    if ROOT.resolve() in module.parents:
        raise RuntimeError(f"mncs_forge imported from the checkout: {module}")
    if result.get("native_modules") is not True:
        raise RuntimeError("packaged Forge MNCS modules are unavailable after installation")
    result["module"] = str(module)
    result["site"] = str(site_packages)
    return {str(key): str(value) for key, value in result.items()}


def run_cli_smoke(cli: Path, config: Path, *, cwd: Path, env: dict[str, str]) -> None:
    run([cli, "--help"], cwd=cwd, env=env)
    for command in (("config", "validate"), ("inspect",), ("operations",)):
        result = run([cli, "--config", config, *command], cwd=cwd, env=env)
        if not result.stdout.strip():
            raise RuntimeError(f"CLI command produced no output: {' '.join(command)}")


def run_mcp_smoke(mcp: Path, config: Path, *, cwd: Path, env: dict[str, str]) -> None:
    run(
        [sys.executable, ROOT / "scripts/mcp-smoke.py", mcp, config],
        cwd=cwd,
        env=env,
        timeout=90,
    )


def run_minimal_workflow(
    python: Path, config: Path, *, cwd: Path, env: dict[str, str]
) -> dict[str, Any]:
    code = textwrap.dedent(
        """
        import json
        import sys
        from mncs_forge.config import load_config
        from mncs_forge.engine import Forge

        forge = Forge(load_config(sys.argv[1]))
        inspection = forge.project_inspect()
        probe = forge.provider_probe("minimal-micro-verifier-provider")
        forge.epoch_begin(
            generator_identity="wheel-smoke-generator",
            evaluator_identity="wheel-smoke-evaluator",
        )
        candidate = forge.candidate_register(
            changed_files=["candidate/generated.py"],
            hypothesis="installed wheel smoke",
            generator_identity="wheel-smoke-generator",
            generator_config_identity="wheel-smoke-config",
        )
        result = forge.verifier_run(
            "python.bounded-add-equivalence",
            candidate_identity=str(candidate["candidate_id"]),
            changed_paths=["candidate/generated.py"],
            scope="function",
        )
        ledger = forge.ledger.verify()
        assert inspection["project"]["identity"] == "mncs-forge-minimal-v1"
        assert probe["status"] == "PASS"
        assert result["status"] == "PASS"
        assert ledger["ok"] is True
        print(json.dumps({
            "probe": probe["status"],
            "result": result["status"],
            "ledger_entries": ledger["entries"],
        }, sort_keys=True))
        """
    )
    result = run([python, "-c", code, config], cwd=cwd, env=env, timeout=90)
    value = json.loads(result.stdout)
    if not isinstance(value, dict):
        raise RuntimeError("installed minimal workflow returned invalid JSON")
    return value


def run_legacy_upgrade(
    python: Path, legacy: Path, *, cwd: Path, env: dict[str, str]
) -> dict[str, Any]:
    code = textwrap.dedent(
        """
        import hashlib
        import json
        import shutil
        import sys
        from pathlib import Path
        from mncs_forge.errors import ForgeError
        from mncs_forge.ledger import Ledger
        from mncs_forge.record_store import LocalRecordStore
        from mncs_forge.records import RecordType, new_record
        from mncs_forge.serialization import canonical_bytes

        state = Path(sys.argv[1])
        original_ledger = (state / "ledger.jsonl").read_bytes()
        original_files = {
            path.relative_to(state).as_posix(): path.read_bytes()
            for path in (state / "records").glob("*/*.json")
        }
        before = Ledger(state).records()
        assert len(before) == 14
        assert Ledger(state).verify()["ok"] is True
        statuses = [entry.payload.status for entry in before if entry.payload.status is not None]
        identities = {entry.payload.identity for entry in before if entry.payload.identity}
        fields = {
            "candidate_id": "forge-tree-sha256-v1:" + ("a" * 64),
            "parent_candidate": None,
            "changed_files": [],
            "declared_hypothesis": "installed wheel historical successor",
            "generator_identity": "wheel-generator",
            "generator_configuration_identity": "wheel-config",
            "source_epoch": str(
                next(entry.payload["epoch_id"] for entry in before if entry.kind == "epoch")
            ),
            "registered_at": "2026-01-01T00:00:00+00:00",
            "current_file_identities": {},
            "useful_benefit_objective": "contract/contract.md",
            "objective_identity": "sha256:historical-fixture",
            "supersedes": None,
        }
        record = new_record(RecordType.CANDIDATE, fields)
        LocalRecordStore(state).commit("candidates", "candidate", record)
        mixed = Ledger(state).verify()
        assert mixed["entries"] == 15
        assert (state / "ledger.jsonl").read_bytes().startswith(original_ledger)
        assert statuses and {"PASS", "FAIL", "UNKNOWN"}.issubset(set(statuses))
        assert identities
        for relative, content in original_files.items():
            assert (state / relative).read_bytes() == content

        future = state.parent / "future-state"
        shutil.copytree(state, future)
        ledger_path = future / "ledger.jsonl"
        lines = [json.loads(line) for line in ledger_path.read_text(encoding="utf-8").splitlines()]
        payload = lines[-1]["payload"]
        payload["record_type"] = "candidate"
        payload["schema_version"] = "999"
        body = {key: value for key, value in lines[-1].items() if key != "entry_hash"}
        lines[-1]["entry_hash"] = hashlib.sha256(canonical_bytes(body)).hexdigest()
        ledger_path.write_bytes(
            b"".join(canonical_bytes(line) + bytes([10]) for line in lines)
        )
        try:
            Ledger(future).verify()
        except ForgeError as exc:
            assert exc.code == "UNSUPPORTED_RECORD_VERSION"
        else:
            raise AssertionError("future schema was accepted")
        print(json.dumps({
            "historical_entries": len(before),
            "mixed_entries": mixed["entries"],
            "statuses": sorted(set(statuses)),
            "identity_count": len(identities),
        }, sort_keys=True))
        """
    )
    result = run([python, "-c", code, legacy], cwd=cwd, env=env, timeout=90)
    value = json.loads(result.stdout)
    if not isinstance(value, dict):
        raise RuntimeError("installed legacy upgrade returned invalid JSON")
    return value


def verify_installation(*, artifact: Path, label: str, root: Path, full: bool) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix=f"mncs-forge-{label}-") as temporary:
        temporary_root = Path(temporary)
        venv = temporary_root / "venv"
        run([sys.executable, "-m", "venv", venv], cwd=root, env=os.environ.copy(), timeout=120)
        python = command_path(venv, "python")
        environment = clean_environment(venv=venv)
        if os.environ.get("MNCS_FORGE_NATIVE_MODE") == "required":
            environment["MNCS_FORGE_NATIVE_MODE"] = "required"
        run(
            [python, "-m", "pip", "install", artifact],
            cwd=temporary_root,
            env=environment,
            timeout=240,
        )
        run([python, "-m", "pip", "check"], cwd=temporary_root, env=environment, timeout=60)
        origin = module_origin(python, cwd=temporary_root, env=environment)
        project = temporary_root / "minimal"
        shutil.copytree(root / "examples/minimal", project)
        cli = command_path(venv, "mncs-forge")
        mcp = command_path(venv, "mncs-forge-server")
        run_cli_smoke(cli, project / "mncs-forge.toml", cwd=temporary_root, env=environment)
        native_required = environment.get("MNCS_FORGE_NATIVE_MODE") == "required"
        if full or native_required:
            run_mcp_smoke(mcp, project / "mncs-forge.toml", cwd=temporary_root, env=environment)
            workflow = run_minimal_workflow(
                python, project / "mncs-forge.toml", cwd=temporary_root, env=environment
            )
            legacy = temporary_root / "legacy-0.1"
            shutil.copytree(root / "tests/fixtures/legacy-0.1/complete-state", legacy)
            upgrade = run_legacy_upgrade(python, legacy, cwd=temporary_root, env=environment)
        else:
            workflow = None
            upgrade = None
        return {
            "label": label,
            "artifact": artifact.name,
            "module_origin": origin,
            "pip_check": "passed",
            "workflow": workflow,
            "legacy_upgrade": upgrade,
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact-dir", type=Path)
    parser.add_argument("--no-build", action="store_true")
    arguments = parser.parse_args(argv)
    try:
        if arguments.artifact_dir is None:
            with tempfile.TemporaryDirectory(prefix="mncs-forge-artifacts-") as temporary:
                artifact_dir = Path(temporary)
                if not arguments.no_build:
                    build_artifacts(Path(sys.executable), artifact_dir)
                result = verify_artifacts(artifact_dir)
        else:
            artifact_dir = arguments.artifact_dir.resolve()
            if not arguments.no_build:
                build_artifacts(Path(sys.executable), artifact_dir)
            result = verify_artifacts(artifact_dir)
    except (OSError, RuntimeError, subprocess.SubprocessError, ValueError) as exc:
        print(f"package verification error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


def verify_artifacts(artifact_dir: Path) -> dict[str, Any]:
    wheel = find_artifact(artifact_dir, ".whl")
    sdist = find_artifact(artifact_dir, ".tar.gz")
    wheel_audit = audit_wheel(wheel)
    sdist_audit = audit_sdist(sdist)
    wheel_result = verify_installation(artifact=wheel, label="wheel", root=ROOT, full=True)
    sdist_result = verify_installation(artifact=sdist, label="sdist", root=ROOT, full=False)
    return {
        "evidence_class": "operator-controlled-development-package-verification",
        "normative": False,
        "wheel_audit": wheel_audit,
        "sdist_audit": sdist_audit,
        "wheel": wheel_result,
        "sdist": sdist_result,
    }


if __name__ == "__main__":
    raise SystemExit(main())
