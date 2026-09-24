"""Generic ctypes transport for the language-owned ``mncs-embed`` session.

This module deliberately knows nothing about Forge records or entrypoints.  It
only opens an already-admitted backend artifact and carries canonical MNCS
values across the stable C ABI.  Application policy stays in the MNCS artifact
and in the caller that constructs those values.
"""

from __future__ import annotations

import ctypes
import json
import threading
import time
from pathlib import Path
from typing import Any


class RetainedEmbedError(RuntimeError):
    """The language-owned retained embedding boundary rejected a request."""


class RetainedEmbedSession:
    """One verified artifact retained across typed named-entrypoint calls."""

    _libraries: dict[str, ctypes.CDLL] = {}

    @classmethod
    def _library(cls, path: Path) -> ctypes.CDLL:
        key = str(path.resolve())
        library = cls._libraries.get(key)
        if library is not None:
            return library
        if not path.is_file():
            raise RetainedEmbedError(f"mncs-embed library not found: {path}")
        library = ctypes.CDLL(key)
        library.mncs_session_open.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
        library.mncs_session_open.restype = ctypes.c_void_p
        library.mncs_session_call_batch.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
        library.mncs_session_call_batch.restype = ctypes.c_void_p
        library.mncs_session_close.argtypes = [ctypes.c_void_p]
        library.mncs_session_close.restype = None
        library.mncs_session_info.argtypes = [ctypes.c_void_p]
        library.mncs_session_info.restype = ctypes.c_void_p
        library.mncs_response_text.argtypes = [ctypes.c_void_p]
        library.mncs_response_text.restype = ctypes.c_char_p
        library.mncs_response_free.argtypes = [ctypes.c_void_p]
        library.mncs_response_free.restype = None
        library.mncs_last_error.argtypes = []
        library.mncs_last_error.restype = ctypes.c_char_p
        cls._libraries[key] = library
        return library

    def __init__(self, library_path: Path, artifact: bytes) -> None:
        self.library_path = library_path.resolve()
        self._library_handle = self._library(self.library_path)
        self._handle: int | None = None
        self._call_lock = threading.RLock()
        self.closed = False
        self.call_count = 0
        self.call_seconds: list[float] = []
        self._artifact = artifact
        self._open(artifact)

    def _error(self, fallback: str) -> RetainedEmbedError:
        raw = self._library_handle.mncs_last_error()
        detail = raw.decode("utf-8", errors="replace") if raw else fallback
        return RetainedEmbedError(detail)

    def _response_json(self, response: int, *, fallback: str) -> Any:
        if not response:
            raise self._error(fallback)
        try:
            raw = self._library_handle.mncs_response_text(response)
            if not raw:
                raise RetainedEmbedError(fallback)
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RetainedEmbedError(f"mncs-embed returned invalid JSON: {exc}") from exc
        finally:
            self._library_handle.mncs_response_free(response)

    def _open(self, artifact: bytes) -> None:
        buffer = ctypes.create_string_buffer(artifact)
        handle = self._library_handle.mncs_session_open(buffer, len(artifact))
        if not handle:
            raise self._error("mncs-embed session admission failed")
        self._handle = int(handle)

    def info(self) -> dict[str, object]:
        if self.closed or self._handle is None:
            raise RetainedEmbedError("retained mncs-embed session is closed")
        response = self._library_handle.mncs_session_info(self._handle)
        value = self._response_json(response, fallback="mncs-embed session info failed")
        if not isinstance(value, dict):
            raise RetainedEmbedError("mncs-embed session info is not an object")
        return value

    def call(
        self,
        module: str,
        function: str,
        arguments: list[dict[str, object]],
        *,
        step_budget: int,
        grants: list[dict[str, object]] | None = None,
    ) -> tuple[dict[str, object], float]:
        return self._call_payload(
            {
                "module": module,
                "function": function,
                "args": arguments,
                "grants": list(grants or []),
                "step_budget": step_budget,
            }
        )

    def call_typed(
        self,
        module: str,
        function: str,
        typed_arguments: list[dict[str, object]],
        *,
        step_budget: int,
        grants: list[dict[str, object]] | None = None,
    ) -> tuple[dict[str, object], float]:
        """Call a retained entrypoint using language-resolved nominal names."""

        return self._call_payload(
            {
                "module": module,
                "function": function,
                "typed_args": typed_arguments,
                "grants": list(grants or []),
                "step_budget": step_budget,
            }
        )

    def _call_payload(
        self, call: dict[str, object]
    ) -> tuple[dict[str, object], float]:
        if self.closed or self._handle is None:
            raise RetainedEmbedError("retained mncs-embed session is closed")
        request = [call]
        started = time.perf_counter()
        with self._call_lock:
            if self.closed or self._handle is None:
                raise RetainedEmbedError("retained mncs-embed session is closed")
            response = self._library_handle.mncs_session_call_batch(
                self._handle,
                json.dumps(request, separators=(",", ":")).encode("utf-8"),
            )
        elapsed = time.perf_counter() - started
        value = self._response_json(response, fallback="mncs-embed retained call failed")
        if not isinstance(value, list) or len(value) != 1 or not isinstance(value[0], dict):
            raise RetainedEmbedError("mncs-embed returned an invalid call result")
        self.call_count += 1
        self.call_seconds.append(elapsed)
        return value[0], elapsed

    def close(self) -> None:
        if self.closed:
            return
        with self._call_lock:
            if self._handle is not None:
                self._library_handle.mncs_session_close(self._handle)
                self._handle = None
            self.closed = True

    def __enter__(self) -> RetainedEmbedSession:
        return self

    def __exit__(self, _exc_type: object, _exc: object, _traceback: object) -> None:
        self.close()
