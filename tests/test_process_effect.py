from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from mncs_forge.errors import ForgeError
from mncs_forge.process_effect import (
    PROCESS_STDIN_CHUNK_BYTES,
    PROCESS_STDIN_INITIAL_BYTES,
    PROCESS_STDIN_MAX_BYTES,
    ProcessEffectClient,
)


class CapturingSession:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def call_typed(self, module, function, arguments, *, step_budget, grants):
        self.calls.append(
            {
                "module": module,
                "function": function,
                "arguments": arguments,
                "step_budget": step_budget,
                "grants": grants,
            }
        )
        return {"status": "failed", "failure_reason": "captured request"}, 0.0


def _decode_byte_string(value: object) -> bytes:
    assert isinstance(value, dict)
    byte_string = value.get("byte_string")
    assert isinstance(byte_string, dict)
    encoded = byte_string.get("base64")
    assert isinstance(encoded, str)
    return base64.b64decode(encoded, validate=True)


def _capture_request(payload: bytes) -> tuple[CapturingSession, dict[str, object]]:
    session = CapturingSession()
    client = ProcessEffectClient(session)  # type: ignore[arg-type]
    with pytest.raises(ForgeError) as error:
        client.start(
            ["/usr/bin/python3", "-c", "pass"],
            cwd=Path("/tmp"),
            environment={"LANG": "C"},
            stdin=payload,
            stdout_limit=64,
            stderr_limit=64,
            deadline_ms=1000,
        )
    assert error.value.code == "PROCESS_EFFECT_UNKNOWN"
    assert len(session.calls) == 1
    arguments = session.calls[0]["arguments"]
    assert isinstance(arguments, list) and len(arguments) == 1
    request = arguments[0]
    assert isinstance(request, dict) and isinstance(request.get("record"), dict)
    fields = request["record"].get("fields")
    assert isinstance(fields, dict)
    return session, fields


@pytest.mark.parametrize(
    "length",
    [0, PROCESS_STDIN_INITIAL_BYTES, PROCESS_STDIN_INITIAL_BYTES + 1, PROCESS_STDIN_MAX_BYTES],
)
def test_process_request_uses_bounded_compact_stdin_chunks(length: int) -> None:
    payload = bytes(index % 251 for index in range(length))
    session, fields = _capture_request(payload)

    initial = _decode_byte_string(fields["stdin"])
    assert initial == payload[:PROCESS_STDIN_INITIAL_BYTES]
    chunks_value = fields["stdin_chunks"]
    assert isinstance(chunks_value, dict)
    sequence = chunks_value.get("sequence")
    assert isinstance(sequence, dict)
    chunks = sequence.get("values")
    assert isinstance(chunks, list)
    decoded_chunks = [_decode_byte_string(chunk) for chunk in chunks]
    assert all(0 < len(chunk) <= PROCESS_STDIN_CHUNK_BYTES for chunk in decoded_chunks)
    assert b"".join([initial, *decoded_chunks]) == payload
    count = fields["stdin_chunk_count"]
    assert count == {"integer": {"value": len(decoded_chunks)}}

    if length == PROCESS_STDIN_MAX_BYTES:
        encoded_request = json.dumps(session.calls[0]["arguments"], separators=(",", ":")).encode(
            "utf-8"
        )
        assert len(encoded_request) < 1_500_000
        assert len(decoded_chunks) == 256


def test_process_request_rejects_input_above_native_contract_before_invocation() -> None:
    session = CapturingSession()
    client = ProcessEffectClient(session)  # type: ignore[arg-type]

    with pytest.raises(ForgeError) as error:
        client.start(
            ["/usr/bin/python3"],
            cwd=Path("/tmp"),
            environment={},
            stdin=b"x" * (PROCESS_STDIN_MAX_BYTES + 1),
            stdout_limit=64,
            stderr_limit=64,
            deadline_ms=1000,
        )

    assert error.value.code == "PROCESS_INPUT_LIMIT"
    assert session.calls == []
