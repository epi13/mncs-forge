"""Warm native runtime: single-flight admission and bounded resident state."""

import os
import threading
import time
from pathlib import Path
from typing import ClassVar
from unittest.mock import patch

import pytest

from mncs_forge import mncs_native
from mncs_forge.errors import ForgeError
from mncs_forge.mncs_native import NATIVE_ARTIFACT_CACHE_KEEP, NativeForgeAdapter
from mncs_forge.retained_embed import CALL_SECONDS_RETAINED, RetainedEmbedSession


class _FakeSession:
    instances: ClassVar = []

    def __init__(self, library, artifact):
        self.library = library
        self.artifact = artifact
        self.closed = False
        type(self).instances.append(self)

    def info(self):
        return {"artifact_identity": "fake-artifact"}

    def close(self):
        self.closed = True


@pytest.fixture
def adapter(tmp_path):
    handle = NativeForgeAdapter(tmp_path, language_root=None)
    yield handle
    handle.close()


def _prime(adapter, identity="fixed-identity"):
    adapter.semantic_input_identity = lambda: identity
    lib = Path(adapter.forge_root) / "libmncs_embed.so"
    lib.write_bytes(b"fake")
    adapter._embed_library = lambda *args: lib
    return lib


def test_concurrent_ensure_session_compiles_once(adapter):
    _FakeSession.instances.clear()
    _prime(adapter)
    adapter._read_cached_artifact = lambda identity: None
    calls = {"count": 0}

    def slow_compile(identity):
        calls["count"] += 1
        time.sleep(0.2)
        return b"artifact"

    adapter._compile_fresh_artifact = slow_compile
    with patch.object(mncs_native, "RetainedEmbedSession", _FakeSession):
        results = []
        errors = []

        def worker():
            try:
                results.append(adapter.ensure_session())
            except Exception as error:  # pragma: no cover - coordination failure
                errors.append(error)

        threads = [threading.Thread(target=worker) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
    assert not errors
    assert calls["count"] == 1
    assert len(results) == 6
    assert all(session is results[0] for session in results)


def test_identity_change_closes_prior_session(adapter):
    _FakeSession.instances.clear()
    _prime(adapter)
    adapter._read_cached_artifact = lambda identity: None
    adapter._compile_fresh_artifact = lambda identity: b"artifact"
    with patch.object(mncs_native, "RetainedEmbedSession", _FakeSession):
        first = adapter.ensure_session()
        adapter.semantic_input_identity = lambda: "changed-identity"
        second = adapter.ensure_session()
    assert second is not first
    assert first.closed
    assert not second.closed


def test_cache_hit_opens_exactly_once(adapter):
    _FakeSession.instances.clear()
    _prime(adapter)
    adapter._read_cached_artifact = lambda identity: b"cached-artifact"

    def unexpected_compile(identity):
        raise AssertionError("cache hit must not compile")

    adapter._compile_fresh_artifact = unexpected_compile
    with patch.object(mncs_native, "RetainedEmbedSession", _FakeSession):
        session = adapter.ensure_session()
    assert len(_FakeSession.instances) == 1
    assert session is _FakeSession.instances[0]
    assert session.artifact == b"cached-artifact"
    assert not session.closed


def test_corrupt_cache_entry_recompiles_fresh(adapter):
    from mncs_forge.retained_embed import RetainedEmbedError

    _prime(adapter)
    adapter._read_cached_artifact = lambda identity: b"corrupt-artifact"
    compiled = {"count": 0}

    def fresh_compile(identity):
        compiled["count"] += 1
        return b"fresh-artifact"

    adapter._compile_fresh_artifact = fresh_compile
    attempts = {"count": 0}
    adopted = _FakeSession.__new__(_FakeSession)
    adopted.closed = False
    adopted.artifact = b"fresh-artifact"

    def routing_session(library, artifact):
        attempts["count"] += 1
        if artifact == b"corrupt-artifact":
            raise RetainedEmbedError("present-but-invalid entry")
        adopted.info = lambda: {"artifact_identity": "fresh-id"}
        return adopted

    with patch.object(mncs_native, "RetainedEmbedSession", routing_session):
        session = adapter.ensure_session()
    assert compiled["count"] == 1
    assert attempts["count"] == 2
    assert session is adopted


def test_failed_admission_retains_nothing(adapter):
    _prime(adapter)
    adapter._read_cached_artifact = lambda identity: None

    def failing_compile(identity):
        raise ForgeError("NATIVE_ADMISSION_FAILED", "boom")

    adapter._compile_fresh_artifact = failing_compile
    with (
        patch.object(mncs_native, "RetainedEmbedSession", _FakeSession),
        pytest.raises(ForgeError, match="boom"),
    ):
        adapter.ensure_session()
    assert adapter._retained_session is None
    assert adapter._retained_identity is None


def test_prune_keeps_newest_entries_and_just_written(tmp_path):
    cache = tmp_path / "native-applications"
    cache.mkdir()
    now = time.time()
    for index in range(NATIVE_ARTIFACT_CACHE_KEEP + 4):
        path = cache / f"forge-core-{index:04d}.json"
        path.write_text("{}")
        os.utime(path, (now - 100 + index, now - 100 + index))
    unrelated = cache / "other.json"
    unrelated.write_text("{}")
    inflight = cache / ".forge-core-next.json.123.tmp"
    inflight.write_text("{}")
    keep = "forge-core-0000.json"
    NativeForgeAdapter._prune_artifact_cache(cache, keep=keep)
    survivors = sorted(path.name for path in cache.iterdir())
    assert keep in survivors
    assert unrelated.name in survivors
    assert inflight.name in survivors
    assert len([name for name in survivors if name.startswith("forge-core-")]) == (
        NATIVE_ARTIFACT_CACHE_KEEP + 1
    )


def test_prune_tolerates_missing_directory(tmp_path):
    NativeForgeAdapter._prune_artifact_cache(tmp_path / "absent", keep="forge-core-x.json")


def test_call_seconds_window_is_bounded():
    session = RetainedEmbedSession.__new__(RetainedEmbedSession)
    session.call_seconds = []
    for index in range(CALL_SECONDS_RETAINED + 500):
        session._record_call_seconds(float(index))
    assert len(session.call_seconds) == CALL_SECONDS_RETAINED
    assert session.call_seconds[0] == 500.0
    assert session.call_seconds[-1] == float(CALL_SECONDS_RETAINED + 499)
