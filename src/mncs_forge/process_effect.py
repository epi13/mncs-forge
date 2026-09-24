"""Generic MNCS process capability transport used by Forge's local Runner.

The module only marshals the canonical ``mncs.std.process.v1`` records across
the retained embed boundary. Resource selection, stale-work meaning, and
verification status remain owned by MNCS Forge.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path

from .errors import ForgeError
from .retained_embed import RetainedEmbedError, RetainedEmbedSession

PROCESS_MODULE = "mncs.std.process.v1"
PROCESS_CAPABILITY = "process_capability"
MAX_TYPED_OUTPUT = 1024


def _integer(value: int) -> dict[str, object]:
    return {"integer": {"value": value}}


def _boolean(value: bool) -> dict[str, object]:
    return {"boolean": {"value": value}}


def _bytes(value: bytes) -> dict[str, object]:
    return {"sequence": {"values": [{"byte": {"value": byte}} for byte in value]}}


def _record(type_name: str, fields: dict[str, object]) -> dict[str, object]:
    return {"record": {"type": type_name, "fields": fields}}


def _record_fields(value: object, name: str) -> dict[str, object]:
    if not isinstance(value, dict) or not isinstance(value.get("record"), dict):
        raise ForgeError("PROCESS_EFFECT_ABI", f"MNCS {name} result is not a record")
    record = value["record"]
    raw_fields = record.get("fields")
    identity = record.get("type_identity")
    prefix = f"mncs:0.2:record-type:mncs.std.process.v1::{name}::"
    if (
        record.get("name") != name
        or not isinstance(identity, str)
        or not identity.startswith(prefix)
        or not isinstance(raw_fields, list)
    ):
        raise ForgeError("PROCESS_EFFECT_ABI", f"MNCS {name} result has the wrong identity")
    fields: dict[str, object] = {}
    for pair in raw_fields:
        if (
            isinstance(pair, list)
            and len(pair) == 2
            and isinstance(pair[0], str)
        ):
            fields[pair[0]] = pair[1]
    return fields


def _scalar(value: object, kind: str) -> object:
    if not isinstance(value, dict) or not isinstance(value.get(kind), dict):
        raise ForgeError("PROCESS_EFFECT_ABI", f"MNCS process observation field is not {kind}")
    return value[kind].get("value")


def _bytes_value(value: object) -> bytes:
    if not isinstance(value, dict) or not isinstance(value.get("sequence"), dict):
        raise ForgeError("PROCESS_EFFECT_ABI", "MNCS process output is not a byte sequence")
    values = value["sequence"].get("values")
    if not isinstance(values, list):
        raise ForgeError("PROCESS_EFFECT_ABI", "MNCS process output sequence is malformed")
    result = bytearray()
    for item in values:
        raw = _scalar(item, "byte")
        if not isinstance(raw, int) or not 0 <= raw <= 255:
            raise ForgeError("PROCESS_EFFECT_ABI", "MNCS process output contains an invalid byte")
        result.append(raw)
    return bytes(result)


def decode_observation(value: object) -> dict[str, object]:
    fields = _record_fields(value, "ProcessObservation")
    status_value = fields.get("status")
    finite = status_value.get("finite") if isinstance(status_value, dict) else None
    discriminant = finite.get("discriminant") if isinstance(finite, dict) else None
    if not isinstance(finite, dict) or finite.get("type_identity") != (
        "mncs:0.2:finite-type:mncs.std.process.v1::ProcessStatus"
    ):
        raise ForgeError("PROCESS_EFFECT_ABI", "MNCS process status has the wrong nominal identity")
    statuses = (
        "Running",
        "Exited",
        "Cancelled",
        "TimedOut",
        "ResourceExhausted",
        "OutputExhausted",
        "Unsupported",
        "Unknown",
    )
    if not isinstance(discriminant, int) or not 0 <= discriminant < len(statuses):
        raise ForgeError("PROCESS_EFFECT_ABI", "MNCS process status has an unknown finite identity")
    result: dict[str, object] = {
        "status": statuses[discriminant],
        "stdout": _bytes_value(fields.get("stdout")),
        "stderr": _bytes_value(fields.get("stderr")),
    }
    for key in (
        "success",
        "has_exit_code",
        "stdout_truncated",
        "stderr_truncated",
        "output_exhausted",
        "deadline_exceeded",
        "cancellation_requested",
        "cancellation_complete",
        "containment_supported",
        "tree_empty",
        "has_tree_empty",
        "launcher_reaped",
        "cleanup_complete",
        "has_cleanup_result",
        "observation_complete",
    ):
        result[key] = _scalar(fields.get(key), "boolean")
    for key in (
        "exit_code",
        "duration_ms",
        "memory_high_events",
        "memory_max_events",
        "oom_events",
        "oom_kill_events",
        "process_limit_events",
        "memory_peak_bytes",
        "swap_peak_bytes",
        "process_peak",
    ):
        result[key] = _scalar(fields.get(key), "integer")
    return result


@dataclass(frozen=True, slots=True)
class OwnedProcess:
    """Canonical source value for a provider-issued, runtime-scoped handle."""

    value: dict[str, object]
    program: str


class ProcessEffectClient:
    """Transport for the one generic retained MNCS process capability."""

    def __init__(self, session: RetainedEmbedSession) -> None:
        self._session = session

    @staticmethod
    def _grant(program: str) -> list[dict[str, object]]:
        return [{"capability": PROCESS_CAPABILITY, "locator": program, "bytes": []}]

    def _call(
        self,
        function: str,
        *,
        program: str,
        typed_arguments: list[dict[str, object]] | None = None,
        arguments: list[dict[str, object]] | None = None,
    ) -> object:
        try:
            if typed_arguments is not None:
                output, _elapsed = self._session.call_typed(
                    PROCESS_MODULE,
                    function,
                    typed_arguments,
                    step_budget=32_000,
                    grants=self._grant(program),
                )
            else:
                output, _elapsed = self._session.call(
                    PROCESS_MODULE,
                    function,
                    arguments or [],
                    step_budget=32_000,
                    grants=self._grant(program),
                )
        except RetainedEmbedError as error:
            raise ForgeError("PROCESS_EFFECT_UNKNOWN", str(error)) from error
        if output.get("status") != "returned":
            raise ForgeError(
                "PROCESS_EFFECT_UNKNOWN",
                str(output.get("failure_reason") or output.get("status") or "MNCS process call failed"),
                details={"process_effect_status": output.get("status")},
            )
        returned = output.get("returned")
        if not isinstance(returned, list) or len(returned) != 1:
            raise ForgeError("PROCESS_EFFECT_ABI", "MNCS process call returned an invalid arity")
        return returned[0]

    def start(
        self,
        argv: list[str],
        *,
        cwd: Path,
        environment: dict[str, str],
        stdin: bytes,
        stdout_limit: int,
        stderr_limit: int,
        deadline_ms: int,
        memory_high_bytes: int = 0,
        memory_max_bytes: int = 0,
        swap_max_bytes: int = 0,
        has_swap_max: bool = False,
        process_max: int = 0,
    ) -> tuple[OwnedProcess, dict[str, object]]:
        if not argv or not argv[0]:
            raise ForgeError("COMMAND_START", "the process effect requires an explicit program")
        program = argv[0]
        bounded_stdout = min(MAX_TYPED_OUTPUT, max(1, stdout_limit))
        bounded_stderr = min(MAX_TYPED_OUTPUT, max(1, stderr_limit))
        env = [
            _record("EnvironmentEntry", {"key": _bytes(key.encode()), "value": _bytes(value.encode())})
            for key, value in sorted(environment.items())
        ]
        request = _record(
            "ProcessRequest",
            {
                "program": _bytes(program.encode()),
                "argv": {
                    "sequence": {"values": [_bytes(item.encode()) for item in argv[1:]]}
                },
                "argv_count": _integer(len(argv) - 1),
                "current_dir": _bytes(os.fsencode(cwd)),
                "environment": {"sequence": {"values": env}},
                "environment_count": _integer(len(env)),
                "resources": _record(
                    "ProcessResourceEnvelope",
                    {
                        "memory_high_bytes": _integer(memory_high_bytes),
                        "memory_max_bytes": _integer(memory_max_bytes),
                        "swap_max_bytes": _integer(swap_max_bytes),
                        "has_swap_max": _boolean(has_swap_max),
                        "process_max": _integer(process_max),
                    },
                ),
                "stdin": _bytes(stdin),
                "stdout_limit": _integer(bounded_stdout),
                "stderr_limit": _integer(bounded_stderr),
                "deadline_ms": _integer(max(1, deadline_ms)),
            },
        )
        raw = self._call("start", program=program, typed_arguments=[request])
        fields = _record_fields(raw, "ProcessStartResult")
        has_handle = _scalar(fields.get("has_handle"), "boolean")
        handle = fields.get("handle")
        if has_handle is not True or not isinstance(handle, dict):
            initial = decode_observation(fields.get("observation"))
            raise ForgeError(
                "PROCESS_START_UNKNOWN",
                f"MNCS process provider did not issue an owned handle ({initial.get('status')})",
                details={"process_observation": initial},
            )
        owned = OwnedProcess(value=handle, program=program)
        return owned, decode_observation(fields.get("observation"))

    def observe(self, process: OwnedProcess) -> dict[str, object]:
        return decode_observation(
            self._call("observe", program=process.program, arguments=[process.value])
        )

    def cancel(self, process: OwnedProcess) -> dict[str, object]:
        return decode_observation(
            self._call("cancel", program=process.program, arguments=[process.value])
        )

    def reap(self, process: OwnedProcess) -> dict[str, object]:
        return decode_observation(
            self._call("reap", program=process.program, arguments=[process.value])
        )

    def wait(self, process: OwnedProcess, *, poll_seconds: float = 0.01) -> dict[str, object]:
        while True:
            observation = self.observe(process)
            if observation.get("status") != "Running":
                return self.reap(process)
            time.sleep(poll_seconds)
