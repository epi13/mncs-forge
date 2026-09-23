from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from mncs_forge.adapters import build_runner
from mncs_forge.application.execution_receipts import established_properties
from mncs_forge.config import load_config
from mncs_forge.errors import ForgeError
from mncs_forge.podman_runner import PodmanRunner

# The rootless Podman adapter is POSIX-only by contract; on Windows hosts the
# adapter itself refuses (ForgeError), so these tests assert POSIX behavior.
pytestmark = pytest.mark.skipif(
    os.name != "posix",
    reason="rootless Podman runner requires a POSIX host",
)

FAKE_PODMAN = """\
#!/usr/bin/env python3
import json, os, sys, time

home = os.environ["FAKE_PODMAN_HOME"]
args = sys.argv[1:]
with open(os.path.join(home, "invocations.jsonl"), "a", encoding="utf-8") as stream:
    stream.write(json.dumps(args) + "\\n")

if args[:1] == ["--version"]:
    print("podman version 5.0.0")
elif args[:2] == ["info", "--format"]:
    rootless = open(os.path.join(home, "rootless"), encoding="utf-8").read().strip() == "true"
    print(json.dumps({"host": {"security": {"rootless": rootless}}}))
elif args[:2] == ["image", "exists"]:
    present = open(os.path.join(home, "image_present"), encoding="utf-8").read().strip()
    sys.exit(0 if present == "1" else 1)
elif args[:2] == ["image", "inspect"]:
    digest = open(os.path.join(home, "digest.txt"), encoding="utf-8").read().strip()
    record = (
        {"Digest": digest, "RepoDigests": [f"fixture@{digest}"]}
        if digest
        else {"Digest": "", "RepoDigests": []}
    )
    print(json.dumps([record]))
elif args[:1] == ["rm"]:
    pass
elif args[:1] == ["run"]:
    with open(os.path.join(home, "podman_environment.json"), "w", encoding="utf-8") as stream:
        json.dump({"MNCS_TEST_SECRET": os.environ.get("MNCS_TEST_SECRET")}, stream)
    behavior = open(os.path.join(home, "run_behavior"), encoding="utf-8").read().strip()
    if behavior == "sleep":
        time.sleep(30)
    elif behavior == "stdout":
        print("canned-container-output")
    sys.exit(0)
else:
    print(f"fake-podman-unhandled: {args}", file=sys.stderr)
    sys.exit(125)
"""


@pytest.fixture
def fake_podman(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "fake-podman-home"
    home.mkdir()
    (home / "rootless").write_text("true", encoding="utf-8")
    (home / "image_present").write_text("1", encoding="utf-8")
    (home / "digest.txt").write_text("sha256:" + "ab" * 32, encoding="utf-8")
    (home / "run_behavior").write_text("stdout", encoding="utf-8")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "podman"
    script.write_text(FAKE_PODMAN, encoding="utf-8")
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("FAKE_PODMAN_HOME", str(home))
    return home


def make_runner(fake_podman: Path, **overrides: object) -> PodmanRunner:
    return PodmanRunner(
        image="quay.io/example/forge-fixture:latest",
        **overrides,  # type: ignore[arg-type]
    )


def invocations(fake_podman: Path, prefix: str) -> list[list[str]]:
    lines = (fake_podman / "invocations.jsonl").read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if json.loads(line)[:1] == [prefix]]


def test_capabilities_report_only_enforced_properties(fake_podman: Path) -> None:
    runner = make_runner(fake_podman)
    capabilities = runner.inspect_capabilities().to_dict()
    assert capabilities["runner_kind"] == "podman-rootless"
    assert capabilities["network_isolation"] == "enforced"
    assert capabilities["filesystem_isolation"] == "enforced"
    assert capabilities["sandbox_isolation"] == "enforced"
    assert capabilities["shell_execution"] == "disabled"
    runtime = runner.inspect_runtime()
    assert runtime["rootless_confirmed"] is True
    assert runtime["image_digest_sha256"] == "ab" * 32
    assert "host root account" in runtime["trusted_computing_base"]


def test_non_rootless_runtime_fails_closed(fake_podman: Path) -> None:
    (fake_podman / "rootless").write_text("false", encoding="utf-8")
    with pytest.raises(ForgeError) as excinfo:
        make_runner(fake_podman)
    assert excinfo.value.code == "RUNNER_UNAVAILABLE"
    assert "rootless" in excinfo.value.message


def test_missing_binary_is_unavailable() -> None:
    with pytest.raises(ForgeError) as excinfo:
        PodmanRunner(image="quay.io/example/missing:latest", podman_path="mncs-no-such-podman")
    assert excinfo.value.code == "RUNNER_UNAVAILABLE"


def test_missing_image_fails_closed(fake_podman: Path) -> None:
    (fake_podman / "image_present").write_text("0", encoding="utf-8")
    with pytest.raises(ForgeError) as excinfo:
        make_runner(fake_podman)
    assert excinfo.value.code == "RUNNER_UNAVAILABLE"


def test_tag_only_image_is_not_an_immutable_identity(fake_podman: Path, tmp_path: Path) -> None:
    (fake_podman / "digest.txt").write_text("", encoding="utf-8")
    runner = make_runner(fake_podman)
    workspace = tmp_path / "workspace-tag-only"
    workspace.mkdir()
    session = runner.run(["true"], cwd=workspace, timeout=5, output_cap=4096, environment={})
    assert session.observation.image_identity is None
    properties = established_properties(session.observation)
    assert properties["containerization"] == "not-established"


def test_container_invocation_preserves_declared_argv(fake_podman: Path, tmp_path: Path) -> None:
    runner = make_runner(fake_podman)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    environment = {
        "LANG": "C",
        "PATH": "/usr/bin",
        "MNCS_TEST_SECRET": "private-provider-value",
    }
    session = runner.run(
        ["python3", "-c", "print('x')"],
        cwd=workspace,
        timeout=5,
        output_cap=4096,
        environment=environment,
    )
    assert session.result is not None
    runs = invocations(fake_podman, "run")
    assert len(runs) == 1
    argv = runs[0]
    for flag in ("--network=none", "--read-only", "-i", "--rm", "--cap-drop=all"):
        assert flag in argv
    assert f"--volume={workspace.resolve(strict=True)}:/workspace:ro" in argv
    assert "--env=LANG" in argv
    assert "--env=PATH" in argv
    assert "--env=MNCS_TEST_SECRET" in argv
    assert not any(item.startswith("--env=LANG=") for item in argv)
    assert "private-provider-value" not in "\x00".join(argv)
    assert json.loads((fake_podman / "podman_environment.json").read_text()) == {
        "MNCS_TEST_SECRET": "private-provider-value"
    }
    separator = argv.index("--")
    assert argv[separator + 1] == "quay.io/example/forge-fixture:latest"
    assert argv[separator + 2 :] == ["python3", "-c", "print('x')"]
    assert list(session.observation.argv) == ["python3", "-c", "print('x')"]
    assert session.observation.network_policy == "podman-network-none"
    properties = established_properties(session.observation)
    assert properties["network_isolation"] == "established"
    assert properties["filesystem_isolation"] == "established"
    assert properties["containerization"] == "established"


def test_resource_envelope_places_container_under_delegated_service_cgroup(
    fake_podman: Path, tmp_path: Path
) -> None:
    runner = make_runner(fake_podman)
    runner._resource_envelope = object()  # type: ignore[assignment]
    workspace = tmp_path / "workspace-cgroup-parent"
    workspace.mkdir()
    argv = runner._container_argv(
        ["true"],
        cwd=workspace,
        mounts=[],
        environment={},
        container_name="forge-fixture",
    )
    assert "--cgroup-parent=@mncs.current-cgroup@" in argv


def test_writable_mount_requires_existing_directory(fake_podman: Path, tmp_path: Path) -> None:
    runner = make_runner(fake_podman, writable_paths=["generated"])
    workspace = tmp_path / "workspace-mounts"
    workspace.mkdir()
    with pytest.raises(ForgeError):
        runner.run(["true"], cwd=workspace, timeout=5, output_cap=4096, environment={})


def test_timeout_yields_explicitly_incomplete_observation(
    fake_podman: Path, tmp_path: Path
) -> None:
    (fake_podman / "run_behavior").write_text("sleep", encoding="utf-8")
    runner = make_runner(fake_podman)
    workspace = tmp_path / "workspace-timeout"
    workspace.mkdir()
    session = runner.run(
        ["sleep", "30"], cwd=workspace, timeout=0.4, output_cap=4096, environment={}
    )
    assert session.error_code == "TIMEOUT"
    assert session.result is None
    assert session.observation.termination_category == "timeout"
    properties = established_properties(session.observation)
    assert properties["execution_completed"] == "unknown"
    assert len(invocations(fake_podman, "rm")) == 1


def test_build_runner_defaults_to_local_process(config) -> None:
    assert type(build_runner(config)).__name__ == "LocalProcessRunner"


def test_load_config_rejects_podman_runner_without_image(project: Path) -> None:
    config_path = project / "mncs-forge.toml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8") + '\n[runner]\nkind = "podman-rootless"\n',
        encoding="utf-8",
    )
    with pytest.raises(ForgeError) as excinfo:
        load_config(config_path)
    assert excinfo.value.code == "CONFIG_INVALID"


def test_load_config_keeps_runner_writable_paths_disjoint_from_protected(
    project: Path,
) -> None:
    config_path = project / "mncs-forge.toml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8") + '\n[runner]\nkind = "podman-rootless"\n'
        'image = "quay.io/example/img:latest"\n'
        'writable_paths = ["protected"]\n',
        encoding="utf-8",
    )
    with pytest.raises(ForgeError) as excinfo:
        load_config(config_path)
    assert excinfo.value.code == "AUTHORITY_OVERLAP"


@pytest.mark.skipif(shutil.which("podman") is None, reason="podman is unavailable")
@pytest.mark.skipif(os.name != "posix", reason="real podman integration requires POSIX")
def test_real_podman_container_enforcement(tmp_path: Path) -> None:
    image = os.environ.get("MNCS_FORGE_PODMAN_TEST_IMAGE", "quay.io/fedora/fedora:latest")
    try:
        runner = PodmanRunner(image=image)
    except ForgeError as exc:
        pytest.skip(f"podman runner unavailable: {exc.message}")
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    echo = runner.run(
        ["/bin/echo", "forge-integration"],
        cwd=workspace,
        timeout=60,
        output_cap=65536,
        environment={},
    )
    assert echo.result is not None and echo.result.returncode == 0
    assert b"forge-integration" in echo.stdout

    readonly = runner.run(
        ["/usr/bin/touch", "/write-attempt-marker"],
        cwd=workspace,
        timeout=60,
        output_cap=65536,
        environment={},
    )
    assert readonly.result is not None and readonly.result.returncode != 0

    network = runner.run(
        [
            "/bin/bash",
            "-c",
            "(exec 3<>/dev/tcp/1.1.1.1/443) 2>/dev/null && echo NET-OK || echo NET-BLOCKED",
        ],
        cwd=workspace,
        timeout=30,
        output_cap=65536,
        environment={},
    )
    assert network.result is not None and network.result.returncode == 0
    assert b"NET-BLOCKED" in network.stdout

    writable_dir = workspace / "generated"
    writable_dir.mkdir()
    mounted = PodmanRunner(image=image, writable_paths=["generated"])
    persisted = mounted.run(
        ["/bin/sh", "-c", "echo persisted > /workspace/generated/out.txt"],
        cwd=workspace,
        timeout=60,
        output_cap=65536,
        environment={},
    )
    assert persisted.result is not None and persisted.result.returncode == 0
    assert (writable_dir / "out.txt").read_text(encoding="utf-8").strip() == "persisted"
