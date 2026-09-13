"""Unit tests for MNCS toolchain binary selection.

These tests use fake language checkouts and never invoke a real compiler,
so they run in the default (non-native) lane. They pin the campaign fix
for stale release binaries: selection prefers the most recently built
binary instead of always preferring ``target/release/mncs``.
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

import pytest

from mncs_forge.errors import ForgeError
from mncs_forge.mncs_native import NativeForgeAdapter

RELEASE = Path("target/release/mncs")
DEBUG = Path("target/debug/mncs")


def _fake_language_root(
    path: Path,
    *,
    release_ns: int | None,
    debug_ns: int | None,
    executable: bool = True,
) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    (path / "Cargo.toml").write_text('[package]\nname = "mncs-fake"\n', encoding="utf-8")
    (path / "library").mkdir(parents=True, exist_ok=True)
    for relative, stamp in ((RELEASE, release_ns), (DEBUG, debug_ns)):
        if stamp is None:
            continue
        binary = path / relative
        binary.parent.mkdir(parents=True, exist_ok=True)
        binary.write_bytes(b"#!/bin/sh\nexit 0\n")
        binary.chmod(0o755 if executable else 0o644)
        os.utime(binary, ns=(stamp, stamp))
    return path


def _adapter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    language_root: Path,
) -> NativeForgeAdapter:
    monkeypatch.delenv("MNCS_CLI", raising=False)
    monkeypatch.delenv("MNCS_LANGUAGE_ROOT", raising=False)
    forge_root = tmp_path / "forge"
    forge_root.mkdir(exist_ok=True)
    return NativeForgeAdapter(forge_root, language_root=language_root)


def test_newer_debug_binary_wins_over_stale_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    language_root = _fake_language_root(
        tmp_path / "lang", release_ns=1_700_000_000_000_000_000, debug_ns=1_757_000_000_000_000_000
    )
    adapter = _adapter(tmp_path, monkeypatch, language_root)

    assert adapter._command() == [str(language_root / DEBUG)]


def test_newer_release_binary_still_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    language_root = _fake_language_root(
        tmp_path / "lang", release_ns=1_757_000_000_000_000_000, debug_ns=1_700_000_000_000_000_000
    )
    adapter = _adapter(tmp_path, monkeypatch, language_root)

    assert adapter._command() == [str(language_root / RELEASE)]


def test_release_wins_exact_mtime_ties_deterministically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    language_root = _fake_language_root(
        tmp_path / "lang", release_ns=1_757_000_000_000_000_000, debug_ns=1_757_000_000_000_000_000
    )
    adapter = _adapter(tmp_path, monkeypatch, language_root)

    assert adapter._command() == [str(language_root / RELEASE)]


def test_unexecutable_newer_binary_is_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    language_root = tmp_path / "lang"
    _fake_language_root(
        language_root, release_ns=1_700_000_000_000_000_000, debug_ns=None, executable=False
    )
    debug = language_root / DEBUG
    debug.parent.mkdir(parents=True, exist_ok=True)
    debug.write_bytes(b"#!/bin/sh\nexit 0\n")
    debug.chmod(0o755)
    os.utime(debug, ns=(1_757_000_000_000_000_000, 1_757_000_000_000_000_000))
    release = language_root / RELEASE
    release.chmod(0o644)
    adapter = _adapter(tmp_path, monkeypatch, language_root)

    assert adapter._command() == [str(debug)]


def test_explicit_mncs_cli_override_wins_over_binaries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    language_root = _fake_language_root(
        tmp_path / "lang", release_ns=1_700_000_000_000_000_000, debug_ns=1_757_000_000_000_000_000
    )
    monkeypatch.delenv("MNCS_LANGUAGE_ROOT", raising=False)
    monkeypatch.setenv("MNCS_CLI", "/opt/mncs/bin/mncs")
    forge_root = tmp_path / "forge"
    forge_root.mkdir(exist_ok=True)
    adapter = NativeForgeAdapter(forge_root, language_root=language_root)

    assert adapter._command() == ["/opt/mncs/bin/mncs"]


def test_cargo_fallback_without_prebuilt_binaries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    language_root = _fake_language_root(tmp_path / "lang", release_ns=None, debug_ns=None)
    adapter = _adapter(tmp_path, monkeypatch, language_root)

    command = adapter._command()

    assert command[0].endswith("cargo")
    assert command[-3:] == ["-p", "mncs-cli", "--"]


def test_status_reports_selected_binary_provenance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stamp = 1_757_000_000_000_000_000
    language_root = _fake_language_root(
        tmp_path / "lang", release_ns=1_700_000_000_000_000_000, debug_ns=stamp
    )
    adapter = _adapter(tmp_path, monkeypatch, language_root)

    status = adapter.status("prefer")

    assert status["selected"] is True
    assert status["command"] == [str(language_root / DEBUG)]
    assert status["binary"] == (language_root / DEBUG).as_posix()
    observed = datetime.fromisoformat(str(status["binary_modified_at"]))
    assert int(observed.timestamp() * 1_000_000_000) == stamp


def test_missing_language_root_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    language_root = _fake_language_root(tmp_path / "lang", release_ns=None, debug_ns=None)
    adapter = _adapter(tmp_path, monkeypatch, language_root)
    adapter.language_root = None

    with pytest.raises(ForgeError) as excinfo:
        adapter._command()

    assert excinfo.value.code == "NATIVE_UNAVAILABLE"
