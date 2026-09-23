"""Bounded no-shell subprocess and Provider Protocol execution."""

from __future__ import annotations

import json
import os
import selectors
import signal
import subprocess
import time
from pathlib import Path
from typing import Any

from .errors import ForgeError
from .execution_windows import collect_windows_pipes
from .ports import ExecutionObservationSink, ExecutionResult

STATUSES = {"PASS", "FAIL", "UNKNOWN"}


def validate_argv(command: object) -> list[str]:
    if not isinstance(command, list) or not command:
        raise ForgeError("INVALID_COMMAND", "command must be a non-empty argument array")
    if not all(isinstance(value, str) and value and "\x00" not in value for value in command):
        raise ForgeError(
            "INVALID_COMMAND", "every command argument must be a non-empty NUL-free string"
        )
    return list(command)


def _kill_process_group(pid: int, sig: int) -> None:
    """Invoke the POSIX-only process-group primitive without Windows stub errors."""

    killpg = getattr(os, "killpg", None)
    if killpg is None:
        raise OSError("process-group termination is unavailable")
    killpg(pid, sig)


def _terminate(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    try:
        if os.name == "posix":
            _kill_process_group(process.pid, signal.SIGTERM)
        else:
            process.terminate()
        process.wait(timeout=1)
    except (OSError, subprocess.TimeoutExpired):
        try:
            if os.name == "posix":
                kill_signal = int(getattr(signal, "SIGKILL", signal.SIGTERM))
                _kill_process_group(process.pid, kill_signal)
            else:
                process.kill()
        except OSError:
            pass
        process.wait(timeout=2)


def validate_limits(timeout: float, output_cap: int, stderr_cap: int | None) -> None:
    if timeout <= 0 or output_cap <= 0 or (stderr_cap is not None and stderr_cap <= 0):
        raise ForgeError("INVALID_LIMIT", "timeout and output cap must be positive")


def run_bounded(
    command: object,
    *,
    cwd: Path,
    timeout: float,
    output_cap: int,
    stderr_cap: int | None = None,
    environment: dict[str, str],
    stdin: bytes = b"",
    _observation: ExecutionObservationSink | None = None,
    resource_envelope: Any | None = None,
) -> ExecutionResult:
    """Run inside a declared aggregate envelope when the caller requires one."""

    argv = validate_argv(command)
    validate_limits(timeout, output_cap, stderr_cap)
    if resource_envelope is None:
        return _run_bounded_process(
            argv,
            cwd=cwd,
            timeout=timeout,
            output_cap=output_cap,
            stderr_cap=stderr_cap,
            environment=environment,
            stdin=stdin,
            _observation=_observation,
        )
    with resource_envelope.execution_lock():
        prepared = resource_envelope.prepare_execution(
            argv, cwd=cwd, environment=environment, timeout=timeout
        )
        if prepared is None:
            return _run_bounded_process(
                argv,
                cwd=cwd,
                timeout=timeout,
                output_cap=output_cap,
                stderr_cap=stderr_cap,
                environment=environment,
                stdin=stdin,
                _observation=_observation,
            )
        budget = resource_envelope.budget.to_dict()
        launcher_environment = resource_envelope.launcher_environment(environment)
        if _observation is not None:
            _observation.resource_started(budget)  # type: ignore[attr-defined]
        result: ExecutionResult | None = None
        failure: ForgeError | None = None
        try:
            result = _run_bounded_process(
                list(prepared.argv),
                cwd=cwd,
                timeout=prepared.timeout_seconds,
                output_cap=output_cap,
                stderr_cap=stderr_cap,
                environment=launcher_environment,
                stdin=stdin,
                _observation=_observation,
                _poll_callback=lambda: resource_envelope.observe_execution(prepared),
            )
        except ForgeError as error:
            failure = error
        except BaseException:
            # Cancellation, KeyboardInterrupt, and unexpected runner failures
            # must not abandon a transient systemd tree or its active-job slot.
            try:
                resource_envelope.finish_execution(prepared)
            except Exception:
                pass
            raise
        facts = resource_envelope.finish_execution(
            prepared,
            timed_out=failure is not None and failure.code == "TIMEOUT",
            failure=failure,
            wrapper_returncode=result.returncode if result is not None else None,
        )
        evidence = {
            **facts,
            "configured_limits": budget,
        }
        if _observation is not None:
            _observation.resource_finished(evidence)  # type: ignore[attr-defined]
        if bool(facts.get("resource_exhausted")) and failure is None:
            metric = facts.get("resource_metric") or "declared-limit"
            bound = facts.get("resource_bound")
            observed = facts.get("resource_observed")
            raise ForgeError(
                "RESOURCE_LIMIT",
                f"verifier exceeded its {metric} envelope (observed={observed}, bound={bound})",
                details={"resource_evidence": evidence},
            ) from failure
        if failure is not None:
            prior_evidence = failure.details.get("resource_evidence")
            raise ForgeError(
                failure.code,
                failure.message,
                details={
                    **failure.details,
                    "resource_evidence": {
                        **(dict(prior_evidence) if isinstance(prior_evidence, dict) else {}),
                        **evidence,
                    },
                },
            ) from failure
        assert result is not None
        command_returncode = facts.get("command_returncode")
        if not isinstance(command_returncode, int):
            raise ForgeError(
                "RESOURCE_EXECUTION_UNKNOWN",
                "systemd completed the unit but did not preserve its command exit status",
                details={
                    "resource_evidence": {
                        **evidence,
                        "resource_exhausted": False,
                        "deferred": True,
                        "limitation": "the transient unit's command exit status was unavailable",
                    }
                },
            )
        return ExecutionResult(
            argv=list(argv),
            returncode=command_returncode,
            stdout=result.stdout,
            stderr=result.stderr,
            duration_seconds=result.duration_seconds,
            resource_envelope=budget,
            resource_observations=evidence,
        )


def _run_bounded_process(
    command: object,
    *,
    cwd: Path,
    timeout: float,
    output_cap: int,
    stderr_cap: int | None = None,
    environment: dict[str, str],
    stdin: bytes = b"",
    _observation: ExecutionObservationSink | None = None,
    _poll_callback: Any | None = None,
) -> ExecutionResult:
    """Run one already-bounded direct process (or the declared envelope launcher)."""

    argv = validate_argv(command)
    validate_limits(timeout, output_cap, stderr_cap)
    caps = {"stdout": output_cap, "stderr": stderr_cap or output_cap}
    started = time.monotonic()
    try:
        process = subprocess.Popen(
            argv,
            cwd=cwd,
            env=environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            start_new_session=os.name == "posix",
        )
    except OSError as exc:
        raise ForgeError("COMMAND_START", f"cannot start declared command: {exc}") from exc
    if _observation is not None:
        _observation.process_started()
    if _poll_callback is not None:
        _poll_callback()
    assert process.stdin is not None
    assert process.stdout is not None
    assert process.stderr is not None
    try:
        process.stdin.write(stdin)
        process.stdin.close()
    except BrokenPipeError:
        pass
    if os.name == "nt":
        returncode, stdout, stderr = collect_windows_pipes(
            process,
            timeout=timeout,
            stdout_cap=caps["stdout"],
            stderr_cap=caps["stderr"],
            observation=_observation,
        )
        return ExecutionResult(
            argv=argv,
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
            duration_seconds=round(time.monotonic() - started, 6),
        )
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ, "stdout")
    selector.register(process.stderr, selectors.EVENT_READ, "stderr")
    chunks: dict[str, bytearray] = {"stdout": bytearray(), "stderr": bytearray()}
    deadline = started + timeout
    try:
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _terminate(process)
                raise ForgeError(
                    "TIMEOUT",
                    f"declared command exceeded {timeout:g} seconds",
                    details={
                        "resource_evidence": {
                            "resource_metric": "wall-duration",
                            "resource_bound": timeout,
                            "resource_observed": round(time.monotonic() - started, 6),
                            "resource_exhausted": True,
                        }
                    },
                )
            events = selector.select(min(remaining, 0.005 if _poll_callback else 0.1))
            if _poll_callback is not None:
                _poll_callback()
            if not events and process.poll() is not None:
                events = [(key, selectors.EVENT_READ) for key in selector.get_map().values()]
            for key, _ in events:
                data = os.read(key.fd, 65536)
                if not data:
                    selector.unregister(key.fileobj)
                    continue
                target = chunks[str(key.data)]
                if _observation is not None:
                    _observation.feed(str(key.data), data)
                target.extend(data)
                cap = caps[str(key.data)]
                if len(target) > cap:
                    if _observation is not None:
                        _observation.mark_limit(str(key.data), cap)
                    _terminate(process)
                    raise ForgeError(
                        "OUTPUT_LIMIT",
                        f"{key.data} exceeded the {cap}-byte cap",
                        details={
                            "resource_evidence": {
                                "resource_metric": "output-bytes",
                                "resource_bound": cap,
                                "resource_observed": len(target),
                                "resource_exhausted": True,
                            }
                        },
                    )
        returncode = process.wait(timeout=max(0.1, deadline - time.monotonic()))
    except subprocess.TimeoutExpired as exc:
        _terminate(process)
        raise ForgeError(
            "TIMEOUT",
            f"declared command exceeded {timeout:g} seconds",
            details={
                "resource_evidence": {
                    "resource_metric": "wall-duration",
                    "resource_bound": timeout,
                    "resource_observed": round(time.monotonic() - started, 6),
                    "resource_exhausted": True,
                }
            },
        ) from exc
    finally:
        selector.close()
        if process.poll() is None:
            _terminate(process)
    return ExecutionResult(
        argv=argv,
        returncode=returncode,
        stdout=bytes(chunks["stdout"]),
        stderr=bytes(chunks["stderr"]),
        duration_seconds=round(time.monotonic() - started, 6),
    )


def run_provider(
    command: object,
    *,
    cwd: Path,
    timeout: float,
    output_cap: int,
    environment: dict[str, str],
    stdin: bytes = b"",
) -> ExecutionResult:
    """Run a declared provider through the canonical bounded execution path."""

    return run_bounded(
        command,
        cwd=cwd,
        timeout=timeout,
        output_cap=output_cap,
        stderr_cap=output_cap,
        environment=environment,
        stdin=stdin,
    )


def parse_provider_response(stdout: bytes) -> dict[str, Any]:
    try:
        text = stdout.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ForgeError("PROVIDER_FRAMING", "provider stdout is not UTF-8") from exc
    lines = text.splitlines()
    if len(lines) != 1 or not lines[0]:
        raise ForgeError(
            "PROVIDER_FRAMING", "provider must emit exactly one non-empty JSON Lines response"
        )
    try:
        value = json.loads(lines[0])
    except json.JSONDecodeError as exc:
        raise ForgeError("PROVIDER_MALFORMED", f"invalid provider JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ForgeError("PROVIDER_MALFORMED", "provider response must be an object")
    if value.get("protocol_version") != "0.1":
        raise ForgeError("PROVIDER_UNSUPPORTED", "only Provider Protocol 0.1 is supported")
    if value.get("type") not in {
        "analysis_response",
        "capabilities",
        "health_response",
        "error",
        "cancelled",
    }:
        raise ForgeError("PROVIDER_MALFORMED", "provider response type is invalid")
    if not isinstance(value.get("provider"), dict):
        raise ForgeError("PROVIDER_MALFORMED", "provider identity must be an object")
    if not isinstance(value.get("extensions"), dict):
        raise ForgeError("PROVIDER_MALFORMED", "provider extensions must be an object")
    if value.get("type") == "analysis_response" and (
        not isinstance(value.get("status"), str) or value.get("status") not in STATUSES
    ):
        raise ForgeError("PROVIDER_MALFORMED", "analysis result status must be PASS/FAIL/UNKNOWN")
    return value


def parse_provider_capabilities(stdout: bytes) -> dict[str, Any]:
    value = parse_provider_response(stdout)
    if value.get("type") != "capabilities":
        raise ForgeError(
            "PROVIDER_MALFORMED", "capability probe must return a capabilities response"
        )
    analyses = value.get("analyses")
    statuses = value.get("statuses")
    if (
        not isinstance(analyses, list)
        or not all(isinstance(item, str) and item for item in analyses)
        or len(set(analyses)) != len(analyses)
    ):
        raise ForgeError("PROVIDER_MALFORMED", "provider analyses must be unique non-empty strings")
    if (
        not isinstance(statuses, list)
        or not statuses
        or not all(isinstance(item, str) and item in STATUSES for item in statuses)
    ):
        raise ForgeError(
            "PROVIDER_MALFORMED", "provider statuses must contain only PASS/FAIL/UNKNOWN"
        )
    if not isinstance(value.get("cancellation"), bool):
        raise ForgeError("PROVIDER_MALFORMED", "provider cancellation must be boolean")
    if not isinstance(value.get("health_checks"), bool):
        raise ForgeError("PROVIDER_MALFORMED", "provider health_checks must be boolean")
    extensions = value["extensions"]
    for key in ("supported_constructs", "unsupported_constructs", "limitations"):
        if key in extensions and (
            not isinstance(extensions[key], list)
            or not all(isinstance(item, str) and item for item in extensions[key])
        ):
            raise ForgeError(
                "PROVIDER_MALFORMED", f"provider extension {key} must be a string array"
            )
    return value
