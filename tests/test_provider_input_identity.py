"""Signature-gated provider input identity: warm reuse without full rehash."""

import time

import pytest

import mncs_forge.provider_artifacts as provider_artifacts
from mncs_forge.provider_artifacts import ProviderArtifact, ProviderArtifactError


def _spec():
    return {
        "schema_version": "mncs.provider-artifact-declaration/1",
        "provider": {"name": "identity-probe", "version": "1"},
        "configuration": {"effects": [], "target": "probe-target"},
        "inputs": [{"repository": "repo", "path": "provider.mncs"}],
        "dependency_roots": [{"repository": "repo", "path": "native"}],
        "stdlib": {"profile": "probe"},
        "abi": {"backend_schema": "probe"},
        "source": {"repository": "repo", "path": "provider.mncs"},
        "admission": {"repository": "repo", "path": "provider.mncs", "module": "m"},
        "remediation": {"repository": "repo", "path": "provider.mncs", "module": "m"},
        "modules": ["m"],
        "build_capability": {"name": "probe-build"},
    }


@pytest.fixture
def provider(tmp_path):
    root = tmp_path / "repo"
    (root / "native").mkdir(parents=True)
    (root / "provider.mncs").write_text("// provider\n")
    for index in range(10):
        (root / "native" / f"mod{index}.mncs").write_text(f"// module {index}\n")
    compiler = tmp_path / "mncs"
    compiler.write_bytes(b"compiler-bytes" * 1000)
    embed = tmp_path / "libmncs_embed.so"
    embed.write_bytes(b"embed-bytes" * 1000)
    return ProviderArtifact(
        _spec(),
        roots={"repo": root},
        compiler=compiler,
        embed=embed,
        cache=tmp_path / "cache",
    )


def _counting_digests(monkeypatch):
    calls = {"count": 0}
    real = provider_artifacts.byte_digest

    def counting(path):
        calls["count"] += 1
        return real(path)

    monkeypatch.setattr(provider_artifacts, "byte_digest", counting)
    return calls


def test_warm_expected_inputs_performs_no_material_reads(provider, monkeypatch):
    warm = _counting_digests(monkeypatch)
    first = provider.expected_inputs()
    assert first == provider.inputs()
    assert warm["count"] > 0
    warm["count"] = 0
    second = provider.expected_inputs()
    assert second == first
    assert warm["count"] == 0


def test_source_edit_rehashes_and_changes_identity(provider, monkeypatch):
    before = provider.expected_inputs()
    source = provider.path(provider.spec["source"])
    source.write_text(source.read_text() + "// owner source change\n")
    warm = _counting_digests(monkeypatch)
    after = provider.expected_inputs()
    assert warm["count"] > 0
    assert after != before
    assert after["source_inputs"] != before["source_inputs"]


def test_library_addition_and_removal_invalidate(provider):
    before = provider.expected_inputs()
    added = provider.roots["repo"] / "native" / "extra.mncs"
    added.write_text("// added\n")
    assert provider.expected_inputs() != before
    added.unlink()
    assert provider.expected_inputs() == before


def test_compiler_replacement_invalidates(provider):
    before = provider.expected_inputs()
    provider.compiler.write_bytes(b"rebuilt-compiler" * 1000)
    after = provider.expected_inputs()
    assert after != before
    assert after["compiler_artifact_sha256"] != before["compiler_artifact_sha256"]


def test_declaration_mutation_invalidates(provider):
    before = provider.expected_inputs()
    provider.spec["configuration"]["target"] = "other-target"
    assert provider.expected_inputs() != before


def test_missing_input_still_fails_closed(provider):
    provider.path(provider.spec["source"]).unlink()
    with pytest.raises(ProviderArtifactError, match="missing or escapes"):
        provider.expected_inputs()


def test_introduced_symlink_escape_fails_closed(provider, tmp_path):
    provider.expected_inputs()
    outside = tmp_path / "outside.mncs"
    outside.write_text("// outside\n")
    link = provider.roots["repo"] / "native" / "escape.mncs"
    link.symlink_to(outside)
    with pytest.raises(ProviderArtifactError, match="symlink escapes"):
        provider.expected_inputs()
    link.unlink()
    assert provider.expected_inputs() == provider.inputs()


def test_concurrent_expected_inputs_agree(provider):
    import threading

    results = []
    errors = []

    def worker():
        try:
            results.append(provider.expected_inputs())
        except Exception as error:  # pragma: no cover - coordination failure
            errors.append(error)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert not errors
    assert len(results) == 8
    assert all(result == results[0] for result in results)


def test_expected_inputs_scales_with_stats_not_bytes(provider, tmp_path):
    # A large compiler binary must not be re-read on a warm call: only the
    # first materialization pays for bytes.
    big = tmp_path / "big-compiler"
    big.write_bytes(b"x" * (8 * 1024 * 1024))
    provider.compiler = big
    provider.expected_inputs()
    start = time.perf_counter()
    for _ in range(10):
        provider.expected_inputs()
    elapsed = time.perf_counter() - start
    assert elapsed < 5.0
