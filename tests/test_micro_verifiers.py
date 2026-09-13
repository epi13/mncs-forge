from __future__ import annotations

import json
import shutil
from dataclasses import replace
from pathlib import Path

import pytest
from conftest import with_native_latency_allowance

from mncs_forge.cli import run
from mncs_forge.config import ForgeConfig, load_config
from mncs_forge.engine import Forge
from mncs_forge.errors import ForgeError


def begin_and_register(forge: Forge) -> dict[str, object]:
    forge.epoch_begin(generator_identity="generator-v1", evaluator_identity="evaluator-v1")
    return forge.candidate_register(
        changed_files=["candidate/main.py"],
        hypothesis="bounded verifier fixture",
        generator_identity="generator-v1",
        generator_config_identity="generator-config-v1",
    )


def append_verifier(project: Path, body: str) -> None:
    path = project / "mncs-forge.toml"
    path.write_text(path.read_text(encoding="utf-8") + "\n" + body, encoding="utf-8")


def verifier_block(
    verifier_id: str,
    *,
    provider: str = "provider-pass",
    workflow: str = "provider-pass",
    method: str = "bounded-structural",
    modes: str = '["development"]',
    claim: str = "A bounded fixture claim.",
    category: str = "bounded_structural_analysis",
    scopes: str = '["file"]',
    cost: str = "low",
    extra: str = "",
) -> str:
    return f"""
[[verifiers]]
id = {json.dumps(verifier_id)}
version = "1"
workflow = {json.dumps(workflow)}
provider = {json.dumps(provider)}
method = {json.dumps(method)}
claim = {json.dumps(claim)}
category = {json.dumps(category)}
modes = {modes}
languages = ["python"]
scopes = {scopes}
input_kinds = ["candidate_identity"]
cost = {json.dumps(cost)}
{extra}
"""


def test_verifier_configuration_and_backward_compatibility(
    config: ForgeConfig, project: Path
) -> None:
    assert config.verifiers["verify-pass"].method == "bounded-structural"
    path = project / "mncs-forge.toml"
    text = path.read_text(encoding="utf-8")
    start = text.index("[[verifiers]]")
    while start >= 0:
        next_start = text.find("[[verifiers]]", start + 1)
        next_other = text.find("[[workflows]]", start + 1)
        candidates = [value for value in (next_start, next_other) if value >= 0]
        end = min(candidates) if candidates else len(text)
        text = text[:start] + text[end:]
        start = text.find("[[verifiers]]")
    path.write_text(text, encoding="utf-8")
    compatible = load_config(path)
    assert compatible.verifiers == {}


def test_duplicate_verifier_id_rejected(project: Path) -> None:
    append_verifier(project, verifier_block("verify-pass"))
    with pytest.raises(ForgeError, match="duplicate verifier"):
        load_config(project / "mncs-forge.toml")


@pytest.mark.parametrize(
    ("body", "match"),
    [
        (verifier_block("bad-provider", provider="missing"), "undeclared provider"),
        (verifier_block("bad-method", method="not-declared"), "method"),
        (
            verifier_block(
                "bad-mode",
                provider="provider-fail",
                workflow="provider-fail",
                modes='["evaluator"]',
            ),
            "modes exceed",
        ),
        (verifier_block("bad-claim", claim=" "), "does not match"),
        (verifier_block("bad-category", category="not-a-category"), "not one of"),
        (verifier_block("bad-scope", scopes='["repository"]'), "not one of"),
        (verifier_block("bad-cost", cost="unbounded"), "not one of"),
        (verifier_block("bad-command", extra='command = ["sh"]'), "not allowed"),
    ],
)
def test_invalid_verifier_declarations_rejected(project: Path, body: str, match: str) -> None:
    append_verifier(project, body)
    with pytest.raises(ForgeError, match=match):
        load_config(project / "mncs-forge.toml")


def test_verifier_cannot_broaden_workflow_disclosure(project: Path) -> None:
    append_verifier(
        project,
        verifier_block(
            "bad-disclosure",
            workflow="evaluator-provider-pass",
            modes='["evaluator"]',
            extra='disclosure = "compact"',
        ),
    )
    with pytest.raises(ForgeError, match="cannot broaden"):
        load_config(project / "mncs-forge.toml")


def test_verifier_timeout_and_development_authority_are_bounded(project: Path) -> None:
    append_verifier(
        project,
        verifier_block("bad-timeout", extra="timeout_seconds = 10"),
    )
    with pytest.raises(ForgeError, match="timeout exceeds"):
        load_config(project / "mncs-forge.toml")

    path = project / "mncs-forge.toml"
    text = path.read_text(encoding="utf-8").replace(
        "timeout_seconds = 10", "timeout_seconds = 0.25"
    )
    text = text.replace("may_run_providers = true", "may_run_providers = false")
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ForgeError, match="development provider execution without authority"):
        load_config(path)


def test_list_and_describe_are_redacted_and_do_not_execute(config: ForgeConfig) -> None:
    forge = Forge(config)
    listing = forge.verifier_list()
    assert listing["configured_count"] == len(config.verifiers)
    assert listing["inspection_executed_providers"] is False
    encoded = json.dumps(listing)
    assert '"command"' not in encoded
    assert '"environment"' not in encoded
    assert forge.ledger.verify()["entries"] == 0
    described = forge.verifier_describe("verify-pass")
    assert described["provider_id"] == "provider-pass"
    assert described["provider_identity"] == "fake-pass"
    assert described["provider_version"] == "1"
    assert described["inspection_executed_provider"] is False
    assert "PATH" not in json.dumps(described)


def test_matching_is_deterministic_stable_and_inspectable(config: ForgeConfig) -> None:
    forge = Forge(config)
    request = {
        "uncertainty_classes": ["structural"],
        "language": "python",
        "artifact_type": "source",
        "changed_paths": ["candidate/main.py"],
        "scope": "file",
        "maximum_cost": "medium",
        "required_category": "bounded_structural_analysis",
    }
    first = forge.verifier_match(**request)  # type: ignore[arg-type]
    second = forge.verifier_match(**request)  # type: ignore[arg-type]
    assert first == second
    assert first["match_outcome"] == "MATCHED"
    matches = first["matches"]
    assert isinstance(matches, list)
    assert [item["rank"] for item in matches] == list(range(1, len(matches) + 1))
    assert matches[0]["verifier_id"] == "verify-pass"
    assert first["execution_performed"] is False
    assert forge.ledger.verify()["entries"] == 0


def test_matching_no_match_preserves_unknown(config: ForgeConfig) -> None:
    result = Forge(config).verifier_match(
        uncertainty_classes=["whole-program-proof"],
        language="rust",
        scope="package",
        maximum_cost="low",
    )
    assert result["match_outcome"] == "NO_MATCH"
    assert result["unresolved_status"] == "UNKNOWN"
    assert result["matches"] == []
    assert result["incompatible"]


@pytest.mark.parametrize(
    ("verifier_id", "status"),
    [
        ("verify-pass", "PASS"),
        ("verify-fail", "FAIL"),
        ("verify-unknown", "UNKNOWN"),
    ],
)
def test_structured_verifier_statuses(config: ForgeConfig, verifier_id: str, status: str) -> None:
    forge = Forge(config)
    candidate = begin_and_register(forge)
    result = forge.verifier_run(
        verifier_id,
        candidate_identity=str(candidate["candidate_id"]),
        changed_paths=["candidate/main.py"],
        scope="file",
    )
    assert result["status"] == status
    assert result["method"] == "bounded-structural"
    assert result["candidate_identity"] == candidate["candidate_id"]
    if status == "FAIL":
        assert result["witnesses"]
    if status == "UNKNOWN":
        assert result["unsupported_constructs"] == ["dynamic-dispatch"]


@pytest.mark.parametrize(
    ("verifier_id", "error_code"),
    [
        ("verify-malformed", "PROVIDER_MALFORMED"),
        ("verify-oversize", "OUTPUT_LIMIT"),
        ("verify-timeout", "TIMEOUT"),
        ("verify-multiple", "PROVIDER_FRAMING"),
        ("verify-nonzero", "PROVIDER_EXIT"),
        ("verify-stderr", "OUTPUT_LIMIT"),
        ("verify-zero-unknown", "PROVIDER_FRAMING"),
    ],
)
def test_operational_provider_failures_are_recorded_unknown(
    config: ForgeConfig, verifier_id: str, error_code: str
) -> None:
    forge = Forge(config)
    begin_and_register(forge)
    result = forge.verifier_run(
        verifier_id,
        changed_paths=["candidate/main.py"],
        scope="file",
    )
    assert result["status"] == "UNKNOWN"
    assert result["operational_error"]["code"] == error_code  # type: ignore[index]
    assert result["status"] != "PASS"
    assert forge.ledger.verify()["ok"] is True


def test_action_result_records_and_identities_are_immutable(config: ForgeConfig) -> None:
    forge = Forge(config)
    candidate = begin_and_register(forge)
    result = forge.verifier_run(
        "verify-pass",
        changed_paths=["candidate/main.py"],
        scope="file",
        dependency_slice_identities=None,
    )
    entries = forge.ledger.records()
    assert [entry["kind"] for entry in entries[-3:]] == [
        "verifier_action",
        "execution_receipt_binding",
        "verifier_result",
    ]
    binding = entries[-2]["payload"]
    assert binding["action_kind"] == "verifier_action"
    assert binding["action_identity"] == entries[-3]["payload"]["action_id"]
    assert binding["status"] == "UNKNOWN"
    payload = entries[-1]["payload"]
    assert payload["candidate_identity"] == candidate["candidate_id"]
    assert payload["verifier_identity"].startswith("forge-json-sha256-v1:")
    assert payload["provider_configuration_identity"].startswith("forge-json-sha256-v1:")
    assert payload["input_identities"]["changed_path_identities"][  # type: ignore[index]
        "candidate/main.py"
    ].startswith("sha256:")
    assert payload["provider_response_identity"].startswith("forge-json-sha256-v1:")
    assert result["output_identity"] == payload["output_identity"]
    duplicate = forge.record_store.commit("verifier-results", "verifier_result", payload)
    assert duplicate.payload.to_json() == payload.to_json()
    assert forge.ledger.verify()["ok"] is True


def test_verifier_result_lineage_supersedes_parent_candidate_result(
    config: ForgeConfig, project: Path
) -> None:
    forge = Forge(config)
    parent = begin_and_register(forge)
    parent_result = forge.verifier_run(
        "verify-pass", changed_paths=["candidate/main.py"], scope="file"
    )
    (project / "candidate/main.py").write_text("VALUE = 2\n", encoding="utf-8")
    child = forge.candidate_register(
        changed_files=["candidate/main.py"],
        hypothesis="descendant verifier fixture",
        generator_identity="generator-v1",
        generator_config_identity="generator-config-v2",
        parent_candidate=str(parent["candidate_id"]),
    )
    child_result = forge.verifier_run(
        "verify-pass", changed_paths=["candidate/main.py"], scope="file"
    )
    assert child_result["candidate_identity"] == child["candidate_id"]
    assert child_result["candidate_parent_identity"] == parent["candidate_id"]
    assert child_result["supersedes_output_identity"] == parent_result["output_identity"]


@pytest.mark.parametrize(
    "changed_path",
    ["../secret", "/tmp/secret", "protected/holdout.txt", "contract/contract.md"],
)
def test_verifier_path_authority_rejections(config: ForgeConfig, changed_path: str) -> None:
    forge = Forge(config)
    begin_and_register(forge)
    with pytest.raises(ForgeError):
        forge.verifier_run("verify-pass", changed_paths=[changed_path], scope="file")


def test_verifier_symlink_escape_rejected(
    config: ForgeConfig, project: Path, tmp_path: Path
) -> None:
    forge = Forge(config)
    begin_and_register(forge)
    outside = tmp_path.parent / f"{tmp_path.name}-verifier-outside"
    outside.mkdir()
    (project / "candidate/escape").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ForgeError, match="escapes"):
        forge.verifier_run("verify-pass", changed_paths=["candidate/escape/file.py"], scope="file")


def test_verifier_parameter_and_request_limits(config: ForgeConfig) -> None:
    forge = Forge(config)
    begin_and_register(forge)
    with pytest.raises(ForgeError, match="undeclared"):
        forge.verifier_run(
            "verify-pass",
            changed_paths=["candidate/main.py"],
            scope="file",
            question_parameters={"command": ["sh"]},
        )
    with pytest.raises(ForgeError, match="too long"):
        forge.verifier_run(
            "verify-pass",
            changed_paths=["candidate/main.py"],
            scope="file",
            question_parameters={"note": "x" * 5000},
        )
    with pytest.raises(ForgeError, match="too many changed"):
        forge.verifier_run(
            "verify-pass",
            changed_paths=[f"candidate/path-{index}.py" for index in range(65)],
            scope="file",
        )


def test_development_workspace_cannot_read_protected_partition(
    config: ForgeConfig,
) -> None:
    forge = Forge(config)
    begin_and_register(forge)
    result = forge.verifier_run(
        "verify-protected-check",
        changed_paths=["candidate/main.py"],
        scope="file",
    )
    assert result["status"] == "PASS"
    assert "holdout" not in json.dumps(result)


def test_witnesses_are_bounded(config: ForgeConfig) -> None:
    limited = replace(
        config,
        raw={**config.raw, "verifier_limits": {"witness_bytes": 256}},
    )
    forge = Forge(limited)
    begin_and_register(forge)
    result = forge.verifier_run(
        "verify-witness",
        changed_paths=["candidate/main.py"],
        scope="file",
    )
    assert result["status"] == "FAIL"
    assert 0 < len(result["witnesses"]) < 20


def test_batch_limits_and_dominance(config: ForgeConfig) -> None:
    config = with_native_latency_allowance(config)
    forge = Forge(config)
    begin_and_register(forge)
    result = forge.verifier_batch(
        ["verify-pass", "verify-unknown", "verify-fail"],
        changed_paths=["candidate/main.py"],
        scope="file",
    )
    assert result["aggregate_status"] == "FAIL"
    assert [item["status"] for item in result["results"]] == [  # type: ignore[index]
        "PASS",
        "UNKNOWN",
        "FAIL",
    ]
    limited = replace(
        config,
        raw={**config.raw, "verifier_limits": {"max_batch": 1}},
    )
    with pytest.raises(ForgeError, match="limit"):
        Forge(limited).verifier_batch(["verify-pass", "verify-fail"])


def test_change_impact_freshness_current_stale_and_partial(
    config: ForgeConfig, project: Path
) -> None:
    forge = Forge(config)
    begin_and_register(forge)
    result = forge.verifier_run("verify-pass", changed_paths=["candidate/main.py"], scope="file")
    current = forge.verifier_explain(str(result["output_identity"]))
    assert current["freshness"]["state"] == "CURRENT"  # type: ignore[index]
    (project / "candidate/main.py").write_text("VALUE = 2\n", encoding="utf-8")
    stale = forge.verifier_explain(str(result["output_identity"]))
    assert stale["freshness"]["state"] == "STALE"  # type: ignore[index]


def test_change_outside_complete_envelope_can_remain_current(
    config: ForgeConfig, project: Path
) -> None:
    forge = Forge(config)
    begin_and_register(forge)
    result = forge.verifier_run("verify-pass", changed_paths=["candidate/main.py"], scope="file")
    (project / "generated/unrelated.py").write_text("VALUE = 9\n", encoding="utf-8")
    freshness = forge.verifier_explain(str(result["output_identity"]))["freshness"]
    assert freshness["state"] == "CURRENT"  # type: ignore[index]
    assert "semantic independence" in str(freshness)


def freeze_candidate(forge: Forge, candidate: dict[str, object]) -> None:
    forge.development_checks_run(["pass-check"])
    forge.candidate_disposition(
        str(candidate["candidate_id"]), disposition="selected", reason="fixture PASS"
    )
    forge.candidate_freeze(
        str(candidate["candidate_id"]),
        environment_identity="environment-v1",
        required_evidence_plan="evaluator/policy.json",
    )


def test_evaluator_freeze_status_only_and_non_independence(config: ForgeConfig) -> None:
    development = Forge(config)
    candidate = begin_and_register(development)
    freeze_candidate(development, candidate)
    evaluator = Forge(config, mode="evaluator")
    result = evaluator.verifier_run(
        "evaluator.status-only",
        changed_paths=["candidate/main.py"],
        scope="file",
    )
    assert result["status"] == "PASS"
    assert result["repair_feedback_withheld"] is True
    assert "witnesses" not in result
    assert result["independent_evaluation"] is False
    explained = evaluator.verifier_explain(str(result["output_identity"]))
    assert explained["repair_feedback_withheld"] is True
    assert explained["witnesses"] == []


def test_development_reuse_cannot_become_independent_evaluator_evidence(
    config: ForgeConfig,
) -> None:
    config = with_native_latency_allowance(config)
    development = Forge(config)
    candidate = begin_and_register(development)
    development.verifier_run("verify-pass", changed_paths=["candidate/main.py"], scope="file")
    freeze_candidate(development, candidate)
    result = Forge(config, mode="evaluator").verifier_run(
        "verify-pass", changed_paths=["candidate/main.py"], scope="file"
    )
    assert result["iterative_development_overlap"] is True
    assert result["independent_evaluation"] is False
    assert result["evidence_class"] == "local_evaluator_evidence"


def test_evaluator_only_verifier_is_rejected_in_development(config: ForgeConfig) -> None:
    forge = Forge(config)
    begin_and_register(forge)
    with pytest.raises(ForgeError, match="not declared for development mode"):
        forge.verifier_run(
            "evaluator.status-only",
            changed_paths=["candidate/main.py"],
            scope="file",
        )


def test_evaluator_freeze_drift_rejected(config: ForgeConfig, project: Path) -> None:
    config = with_native_latency_allowance(config)
    development = Forge(config)
    candidate = begin_and_register(development)
    freeze_candidate(development, candidate)
    (project / "contract/contract.md").write_text("drift\n", encoding="utf-8")
    with pytest.raises(ForgeError, match="drift"):
        Forge(config, mode="evaluator").verifier_run(
            "evaluator.status-only",
            changed_paths=["candidate/main.py"],
            scope="file",
        )


def test_cli_verifier_surface(config: ForgeConfig, project: Path) -> None:
    forge = Forge(config)
    begin_and_register(forge)
    config_path = str(project / "mncs-forge.toml")
    code, listing = run(["--config", config_path, "verifier", "list"])
    assert code == 0
    assert listing["configured_count"]
    code, matched = run(
        [
            "--config",
            config_path,
            "verifier",
            "match",
            "--uncertainty",
            "structural",
            "--language",
            "python",
            "--scope",
            "file",
        ]
    )
    assert code == 0
    assert matched["match_outcome"] == "MATCHED"
    code, result = run(
        [
            "--config",
            config_path,
            "verifier",
            "run",
            "verify-pass",
            "--changed",
            "candidate/main.py",
            "--scope",
            "file",
        ]
    )
    assert code == 0
    assert result["status"] == "PASS"


def test_minimal_example_verifiers_execute(tmp_path: Path) -> None:
    root = Path(__file__).parents[1]
    example = tmp_path / "minimal"
    shutil.copytree(root / "examples/minimal", example)
    config = load_config(example / "mncs-forge.toml")
    forge = Forge(config)
    forge.epoch_begin(
        generator_identity="example-generator", evaluator_identity="example-evaluator"
    )
    candidate = forge.candidate_register(
        changed_files=["candidate/generated.py"],
        hypothesis="minimal example verifier",
        generator_identity="example-generator",
        generator_config_identity="example-generator-config",
    )
    equivalence = forge.verifier_run(
        "python.bounded-add-equivalence",
        candidate_identity=str(candidate["candidate_id"]),
        changed_paths=["candidate/generated.py"],
        scope="function",
    )
    assert equivalence["status"] == "PASS"
    impact = forge.verifier_run(
        "evidence.change-impact",
        candidate_identity=str(candidate["candidate_id"]),
        changed_paths=["candidate/generated.py"],
        scope="evidence",
        question_parameters={"dependency_paths": ["evidence/summary.json"]},
    )
    assert impact["status"] == "PASS"
    assert "semantic independence" in str(impact["limitations"])
    generated = example / "generated-output"
    generated.mkdir()
    (generated / "unrelated.py").write_text("VALUE = 3\n", encoding="utf-8")
    freshness = forge.verifier_explain(str(impact["output_identity"]))["freshness"]
    assert freshness["state"] == "UNKNOWN"  # type: ignore[index]
