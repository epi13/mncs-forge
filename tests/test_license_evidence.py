"""License-evidence scan tests (Forge rights/provenance analysis domain)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

import mncs_forge.application.license_evidence as license_evidence_module
from mncs_forge.application.license_evidence import (
    compute_content_digest,
    scan_license_evidence,
)
from mncs_forge.config import ForgeConfig
from mncs_forge.engine import Forge
from mncs_forge.record_store import LocalRecordStore


@pytest.fixture()
def project_root(tmp_path: Path) -> Path:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "demo"\nversion = "0.1.0"\nlicense = "Apache-2.0"\n',
        encoding="utf-8",
    )
    return tmp_path


def _config(root: Path) -> ForgeConfig:
    raw: dict[str, Any] = {
        "project": {"name": "demo", "identity": "demo-project"},
        "limits": {"timeout_seconds": 1, "output_bytes": 1024},
    }
    return ForgeConfig(
        config_path=root / "mncs-forge.toml",
        root=root,
        raw=raw,
        path_values={},
        providers={},
        workflows={},
        verifiers={},
    )


def test_declared_metadata_becomes_observed_declaration(project_root: Path) -> None:
    evidence = scan_license_evidence(_config(project_root))
    claims = [c for c in evidence["claims"] if c["claim_type"] == "license-declaration"]
    assert any(
        c["confidence"] == "observed-declaration" and "Apache-2.0" in c["statement"] for c in claims
    )
    observation = next(
        item for item in evidence["observations"]
        if item["name"] == "pyproject.toml_license_declaration"
    )
    assert observation["source_ref"]["path"] == "pyproject.toml"
    assert observation["source_ref"]["resolved_path"] == "pyproject.toml"
    assert observation["source_ref"]["selector"] == "/project/license"
    assert observation["source_ref"]["sha256"] == hashlib.sha256(
        (project_root / "pyproject.toml").read_bytes()
    ).hexdigest()
    assert not Path(observation["source_ref"]["path"]).is_absolute()


def test_missing_evidence_is_explicitly_unknown(tmp_path: Path) -> None:
    evidence = scan_license_evidence(_config(tmp_path))
    assert evidence["claims"] == [
        {
            "claim_type": "unknown-license-state",
            "statement": evidence["claims"][0]["statement"],
            "confidence": "insufficient-evidence",
        }
    ]
    assert "do not infer one" in evidence["claims"][0]["statement"]


def test_notice_file_hash_and_heuristic_confidence(project_root: Path) -> None:
    (project_root / "LICENSE").write_text(
        "Apache License\nVersion 2.0, January 2004\n", encoding="utf-8"
    )
    evidence = scan_license_evidence(_config(project_root))
    heuristic = [c for c in evidence["claims"] if c["confidence"] == "heuristic"]
    assert len(heuristic) == 1
    assert heuristic[0]["spdx_expression"] == "Apache-2.0"
    observations = {o["name"]: o for o in evidence["observations"]}
    recorded = observations["notice_file:LICENSE"]["value"]["sha256"]
    assert recorded == hashlib.sha256((project_root / "LICENSE").read_bytes()).hexdigest()
    assert observations["notice_file:LICENSE"]["source_ref"]["sha256"] == recorded


def test_digest_tamper_detection(project_root: Path) -> None:
    evidence = scan_license_evidence(_config(project_root))
    ok = compute_content_digest(evidence) == evidence["content_digest"]
    assert ok
    tampered = dict(evidence)
    tampered["claims"] = []
    assert compute_content_digest(tampered) != evidence["content_digest"]


def test_producer_reference_shape(project_root: Path) -> None:
    evidence = scan_license_evidence(_config(project_root))
    producer = evidence["producer"]
    assert producer["producer"] == "mncs-forge"
    assert producer["stableId"] == evidence["evidence_id"]
    assert producer["schemaVersion"] == "0.3.0"


def test_evidence_schema_accepts_source_bound_record(project_root: Path) -> None:
    schema_path = (
        Path(__file__).parents[1]
        / "src/mncs_forge/resources/mncs-forge-license-evidence-0.3.schema.json"
    )
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    evidence = scan_license_evidence(_config(project_root))

    Draft202012Validator.check_schema(schema)
    Draft202012Validator(
        schema, format_checker=Draft202012Validator.FORMAT_CHECKER
    ).validate(evidence)


def test_forge_facade_exposes_the_versioned_scan(
    config: ForgeConfig, project: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    (project / "pyproject.toml").write_text(
        '[project]\nname = "fixture"\nlicense = "MIT"\n', encoding="utf-8"
    )
    monkeypatch.setenv("MNCS_FORGE_NATIVE_MODE", "off")
    forge = Forge(config, record_store=LocalRecordStore(config.state_dir))
    try:
        evidence = forge.license_evidence_scan()
    finally:
        forge.close()

    assert evidence["schema_version"] == "0.3.0"
    assert evidence["producer"]["recordKind"] == "LicenseEvidence"
    assert evidence["subject"]["artifact_refs"][0]["id"] == "project:fixture-v1"


def test_source_identity_ignores_scan_time_but_changes_with_source_bytes(
    project_root: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        license_evidence_module, "_utc_now", lambda: "2026-10-21T10:00:00Z"
    )
    first = scan_license_evidence(_config(project_root))
    monkeypatch.setattr(
        license_evidence_module, "_utc_now", lambda: "2026-10-21T10:01:00Z"
    )
    second = scan_license_evidence(_config(project_root))

    assert first["evidence_id"] == second["evidence_id"]
    assert first["content_digest"] != second["content_digest"]

    (project_root / "pyproject.toml").write_text(
        '[project]\nname = "demo"\nversion = "0.1.0"\n'
        'license = "Apache-2.0"\n# source bytes changed\n',
        encoding="utf-8",
    )
    third = scan_license_evidence(_config(project_root))
    assert third["claims"][0]["statement"] == second["claims"][0]["statement"]
    assert third["evidence_id"] != second["evidence_id"]


def test_all_metadata_and_notice_sources_are_retained(project_root: Path) -> None:
    (project_root / "package.json").write_text(
        '{"name":"demo","license":"GPL-3.0-only"}\n', encoding="utf-8"
    )
    (project_root / "LICENSE").write_text(
        "Apache License\nVersion 2.0, January 2004\n", encoding="utf-8"
    )
    (project_root / "COPYING").write_text(
        "GNU GENERAL PUBLIC LICENSE\nVersion 3, 29 June 2007\n",
        encoding="utf-8",
    )
    evidence = scan_license_evidence(_config(project_root))

    metadata = {
        item["source_ref"]["path"]: item
        for item in evidence["observations"]
        if item["name"].endswith("_license_declaration")
    }
    assert set(metadata) == {"pyproject.toml", "package.json"}
    assert metadata["package.json"]["source_ref"]["selector"] == "/license"
    assert {item["name"] for item in evidence["observations"]} >= {
        "notice_file:LICENSE",
        "notice_file:COPYING",
    }
    declarations = {
        claim["value"] for claim in evidence["claims"]
        if claim["claim_type"] == "license-declaration"
    }
    assert declarations == {"Apache-2.0", "GPL-3.0-only"}
    assert any("does not validate" in item for item in evidence["limitations"])


def test_external_symlink_source_is_not_read(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    missing = scan_license_evidence(_config(project))
    external_notice = tmp_path / "external-LICENSE"
    external_notice.write_text(
        "Apache License\nVersion 2.0, January 2004\n", encoding="utf-8"
    )
    try:
        (project / "LICENSE").symlink_to(external_notice)
    except (NotImplementedError, OSError):
        pytest.skip("symlinks are unavailable on this platform")

    evidence = scan_license_evidence(_config(project))

    assert not any(item["name"] == "notice_file:LICENSE" for item in evidence["observations"])
    issue = next(
        claim for claim in evidence["claims"]
        if claim["claim_type"] == "source-unavailable"
    )
    assert issue["source_path"] == "LICENSE"
    assert "outside the project root" in issue["statement"]
    assert evidence["evidence_id"] != missing["evidence_id"]
