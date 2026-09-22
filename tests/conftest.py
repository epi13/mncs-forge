from __future__ import annotations

import json
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from mncs_forge.config import ForgeConfig, load_config


def _verifier_modes(mode: str) -> str:
    return json.dumps(["development", "evaluator"] if mode == "PASS" else ["development"])


def with_native_latency_allowance(config: ForgeConfig) -> ForgeConfig:
    """Copy the fixture config with headroom for native-toolchain latency.

    Fixture workflow timeouts are tiny (0.5s), so the batch budgets derived
    from them cannot absorb native compiler executions. Tests asserting
    semantic outcomes — not budgets — opt in explicitly. Budget exhaustion
    itself stays pinned by the fake-clock test, and product defaults are
    untouched.
    """

    return replace(config, raw={**config.raw, "verifier_limits": {"batch_timeout_seconds": 300}})


@pytest.fixture
def project(tmp_path: Path) -> Path:
    for directory in (
        "candidate",
        "generated",
        "contract",
        "reference",
        "evaluator",
        "evidence",
        "protected",
        "output",
    ):
        (tmp_path / directory).mkdir()
    (tmp_path / "candidate/main.py").write_text("VALUE = 1\n", encoding="utf-8")
    (tmp_path / "contract/contract.md").write_text("Return VALUE.\n", encoding="utf-8")
    (tmp_path / "reference/reference.py").write_text("VALUE = 1\n", encoding="utf-8")
    (tmp_path / "evaluator/policy.json").write_text(
        '{"required":["pass-check"]}\n', encoding="utf-8"
    )
    (tmp_path / "evidence/result.json").write_text('{"status":"UNKNOWN"}\n', encoding="utf-8")
    (tmp_path / "protected/holdout.txt").write_text("sealed\n", encoding="utf-8")
    fixture = Path(__file__).parent / "fixtures/fake_provider.py"
    python = sys.executable
    providers = [
        ("provider-pass", "PASS"),
        ("provider-fail", "FAIL"),
        ("provider-unknown", "UNKNOWN"),
        ("provider-malformed", "MALFORMED"),
        ("provider-oversize", "OVERSIZE"),
        ("provider-timeout", "TIMEOUT"),
        ("provider-identity-drift", "IDENTITY_DRIFT"),
        ("provider-multiple", "MULTIPLE"),
        ("provider-nonzero", "NONZERO"),
        ("provider-stderr", "STDERR"),
        ("provider-zero-unknown", "ZERO_UNKNOWN"),
        ("provider-protected-check", "PROTECTED_CHECK"),
        ("provider-witness", "WITNESS"),
        ("provider-security", "SECURITY_PASS"),
    ]
    provider_tables = "\n".join(
        (
            "[[providers]]\n"
            f"id = {json.dumps(name)}\n"
            f"name = {json.dumps(name)}\n"
            f"command = {json.dumps([python, str(fixture), mode])}\n"
            'transport = "stdio-jsonl"\n'
            "required = false\n"
            f"capabilities = {json.dumps(['bounded-structural', 'security-micro'] if mode.startswith('SECURITY_') else ['bounded-structural'])}\n"
            f"identity = {json.dumps('fake-' + mode.lower())}\n"
            'version = "1"\n'
        )
        for name, mode in providers
    )
    provider_workflows = "\n".join(
        (
            "[[workflows]]\n"
            f"name = {json.dumps(name)}\n"
            f"category = {json.dumps('sanitizers' if mode.startswith('SECURITY_') else 'bounded_structural_analysis')}\n"
            f"mode = {json.dumps('both' if name == 'provider-pass' else 'development')}\n"
            f"command = {json.dumps([python, str(fixture), mode])}\n"
            "provider_protocol = true\n"
            f"provider_id = {json.dumps(name)}\n"
        )
        for name, mode in providers
    )
    verifier_tables = "\n".join(
        (
            "[[verifiers]]\n"
            f"id = {json.dumps('verify-' + mode.lower().replace('_', '-'))}\n"
            'version = "1"\n'
            f"workflow = {json.dumps(name)}\n"
            f"provider = {json.dumps(name)}\n"
            f"method = {json.dumps('security-micro' if mode.startswith('SECURITY_') else 'bounded-structural')}\n"
            f"claim = {json.dumps(('Bounded security micro-verifier claim for ' if mode.startswith('SECURITY_') else 'Fixture bounded claim for ') + mode)}\n"
            f"category = {json.dumps('sanitizers' if mode.startswith('SECURITY_') else 'bounded_structural_analysis')}\n"
            f"modes = {_verifier_modes(mode)}\n"
            'languages = ["python"]\n'
            'artifact_types = ["source"]\n'
            'scopes = ["file"]\n'
            'input_kinds = ["candidate_identity", "changed_paths", "question_parameters"]\n'
            'uncertainty_classes = ["structural", "change-impact"]\n'
            f"cost = {json.dumps('low' if mode in {'PASS', 'SECURITY_PASS'} else 'medium')}\n"
            'parameter_keys = ["note"]\n'
            "timeout_seconds = 0.25\n"
        )
        for name, mode in providers
    )
    config = f"""
version = 1
environment_allowlist = [
    "PATH",
    "LANG",
    "LC_ALL",
    "MNCS_COMMONS_ROOT",
    "MNCS_VERIFICATION_POLICY",
]

[project]
name = "fixture"
identity = "fixture-v1"
root = "."

[paths]
candidates = ["candidate"]
generated = ["generated"]
contracts = ["contract"]
references = ["reference"]
evaluators = ["evaluator"]
acceptance_policies = ["evaluator/policy.json"]
development_evidence = ["evidence"]
protected = ["protected"]
outputs = ["output"]

[limits]
timeout_seconds = 0.5
output_bytes = 4096

[commands]
mncs = [{json.dumps(python)}]
mncds = [{json.dumps(python)}]

[authority.development]
may_write_candidates = true
may_write_generated = true
may_run_providers = true

[authority.evaluator]
candidate_read_only = true
authority_read_only = true
require_frozen_identities = true
withhold_repair_feedback = true

[policies]
selection = "evaluator/policy.json"
useful_benefit_objective = "contract/contract.md"

{provider_tables}
{provider_workflows}
{verifier_tables}

[[workflows]]
name = "evaluator-provider-pass"
category = "bounded_structural_analysis"
mode = "evaluator"
command = {json.dumps([python, str(fixture), "PASS"])}
provider_protocol = true
provider_id = "provider-pass"
disclosure = "status-only"

[[verifiers]]
id = "evaluator.status-only"
version = "1"
workflow = "evaluator-provider-pass"
provider = "provider-pass"
method = "bounded-structural"
claim = "Fixture evaluator status-only bounded claim."
category = "bounded_structural_analysis"
modes = ["evaluator"]
languages = ["python"]
scopes = ["file"]
input_kinds = ["candidate_identity", "changed_paths"]
uncertainty_classes = ["structural"]
cost = "low"
timeout_seconds = 0.25
disclosure = "status-only"

[[workflows]]
name = "pass-check"
category = "inspection"
mode = "development"
command = {json.dumps([python, "-c", "import json; print(json.dumps({'status':'PASS'}))"])}
provider_protocol = false

[[workflows]]
name = "project-check"
category = "inspection"
mode = "development"
command = {json.dumps([python, "-c", "import json; print(json.dumps({'status':'PASS'}))"])}
provider_protocol = false
subject = "project"

[[workflows]]
name = "fail-check"
category = "build"
mode = "development"
command = {json.dumps([python, "-c", "import sys; sys.exit(7)"])}
provider_protocol = false

[[workflows]]
name = "timeout-check"
category = "inspection"
mode = "development"
command = {json.dumps([python, "-c", "import time; time.sleep(5)"])}
provider_protocol = false

[[workflows]]
name = "output-check"
category = "inspection"
mode = "development"
command = {json.dumps([python, "-c", "print('x' * 100000)"])}
provider_protocol = false

[[workflows]]
name = "injection-check"
category = "inspection"
mode = "development"
command = {
        json.dumps(
            [
                python,
                "-c",
                "import json; print(json.dumps({'status':'PASS'}))",
                ";touch",
                "PWNED",
            ]
        )
    }
provider_protocol = false

[[workflows]]
name = "evaluator-pass"
category = "inspection"
mode = "evaluator"
command = {json.dumps([python, "-c", "import json; print(json.dumps({'status':'PASS'}))"])}
provider_protocol = false
disclosure = "status-only"
"""
    (tmp_path / "mncs-forge.toml").write_text(config, encoding="utf-8")
    return tmp_path


@pytest.fixture
def config(project: Path) -> ForgeConfig:
    return load_config(project / "mncs-forge.toml")
