"""Provider discovery, probing, and capability-blocker application service."""

from __future__ import annotations

import importlib.util
import os
from collections.abc import Callable, Iterable
from pathlib import Path

from ..config import ForgeConfig, Provider
from ..errors import ForgeError
from ..execution import STATUSES, parse_provider_capabilities
from ..mncs_native import NativeProviderCapabilityDecision
from ..ports import ProjectObserver, RecordCommitter, RecordReader, Runner
from ..records import ForgeRecord, RecordType, new_record
from ..serialization import canonical_bytes, local_json_identity
from .support import aggregate_status, now, redact


class ProviderService:
    def __init__(
        self,
        *,
        config: ForgeConfig,
        mode: str,
        records: RecordReader,
        record_store: RecordCommitter,
        executor: Runner,
        observer: ProjectObserver,
        native_status_aggregate: Callable[[list[str]], str] | None = None,
        native_capability_policy: Callable[..., NativeProviderCapabilityDecision] | None = None,
    ) -> None:
        self.config = config
        self.mode = mode
        self.records = records
        self.record_store = record_store
        self.executor = executor
        self.observer = observer
        self.native_status_aggregate = native_status_aggregate
        self.native_capability_policy = native_capability_policy

    def _aggregate_status(self, statuses: Iterable[str]) -> str:
        # Preserve the compatibility helper's treatment of non-status values
        # at this internal boundary; native code owns the status fold itself.
        values = [status for status in statuses if status in STATUSES]
        if self.native_status_aggregate is None:
            return aggregate_status(values)
        result = self.native_status_aggregate(values)
        if result not in STATUSES:
            raise ForgeError(
                "NATIVE_PROVIDER_STATUS_UNKNOWN", "native provider status is not recognized"
            )
        return result

    def _require_development(self) -> None:
        if self.mode != "development":
            raise ForgeError(
                "MODE_FORBIDDEN",
                f"operation requires development mode; current mode is {self.mode}",
            )

    def _latest_probe(self, provider_id: str) -> ForgeRecord | None:
        for entry in reversed(self.records.records("provider_probe")):
            if entry.payload.get("provider_id") == provider_id:
                return entry.payload
        return None

    @staticmethod
    def _declared_model(provider: Provider) -> dict[str, object]:
        return {
            "provider_id": provider.provider_id,
            "name": provider.name,
            "declared_identity": provider.identity,
            "declared_version": provider.version,
            "command": [redact(item, 1024) for item in provider.command],
            "transport": provider.transport,
            "required": provider.required,
            "declared_capabilities": provider.capabilities,
            "supported_constructs": provider.supported_constructs,
            "unsupported_constructs": provider.unsupported_constructs,
            "limitations": provider.limitations,
            "expected_executable_identity": provider.executable_identity,
            "descriptor": provider.descriptor,
        }

    @staticmethod
    def _package_importable(name: str, environment: dict[str, str]) -> bool:
        pythonpath = environment.get("PYTHONPATH", "")
        for root in pythonpath.split(os.pathsep):
            if not root:
                continue
            package = Path(root) / name
            if (package / "__init__.py").is_file() or package.with_suffix(".py").is_file():
                return True
        try:
            return importlib.util.find_spec(name) is not None
        except (ImportError, ModuleNotFoundError, ValueError):
            return False

    def _inventory_item(self, provider: Provider) -> dict[str, object]:
        item = self._declared_model(provider)
        latest = self._latest_probe(provider.provider_id)
        item.update(
            {
                "availability": "UNKNOWN",
                "status": "UNKNOWN",
                "executable": None,
                "executable_identity": None,
                "last_probe_result": latest.to_object_dict() if latest is not None else None,
                "probe_stale": False,
            }
        )
        try:
            executable, identity = self.observer.provider_executable(provider)
        except ForgeError as exc:
            item.update(
                {
                    "availability": "UNAVAILABLE",
                    "limitations": [*provider.limitations, exc.message],
                    "availability_error": exc.code,
                }
            )
            return item
        probe_stale = bool(latest and latest.get("executable_identity") != identity)
        item.update(
            {
                "availability": "AVAILABLE",
                "executable": str(executable),
                "executable_identity": identity,
                "status": (
                    "PASS"
                    if latest and latest.get("status") == "PASS" and not probe_stale
                    else "UNKNOWN"
                ),
                "probe_stale": probe_stale,
            }
        )
        return item

    def inventory(self) -> dict[str, object]:
        providers = [self._inventory_item(provider) for provider in self.config.providers.values()]
        return {
            "providers": providers,
            "configured_count": len(providers),
            "required_capabilities": self.config.required_capabilities,
            "status": self._aggregate_status(str(item["status"]) for item in providers),
            "limitations": (
                [
                    "no providers are configured; Forge does not infer structural capability "
                    "from source reading or command availability"
                ]
                if not providers
                else [
                    "availability is not analysis evidence; a recognized explicit capability "
                    "probe is required before a capability can satisfy policy"
                ]
            ),
            "dominance": "FAIL > UNKNOWN > PASS",
        }

    def _record_probe(self, fields: dict[str, object]) -> ForgeRecord:
        record = new_record(RecordType.PROVIDER_PROBE, fields)
        self.record_store.commit("provider-probes", "provider_probe", record)
        return record

    def probe(self, provider_id: str) -> dict[str, object]:
        self._require_development()
        try:
            provider = self.config.providers[provider_id]
        except KeyError as exc:
            raise ForgeError(
                "PROVIDER_NOT_CONFIGURED", f"provider is not configured: {provider_id}"
            ) from exc
        started_at = now()
        executable_identity: str | None = None
        executable: Path | None = None
        try:
            executable, executable_identity = self.observer.provider_executable(provider)
            request = {
                "protocol_version": "0.1",
                "type": "capabilities",
                "request_id": "forge-capabilities-"
                + local_json_identity(
                    {
                        "provider": provider.provider_id,
                        "executable": executable_identity,
                        "at": started_at,
                    }
                ).split(":", 1)[1][:24],
                "extensions": {},
            }
            runtime_modules = [
                {
                    "declared": declared,
                    "path": str(path),
                    "identity": identity,
                    "mechanism": "mncs-forge.family-module-roots.v0.1",
                }
                for declared, (path, identity) in zip(
                    provider.module_roots,
                    self.config.resolved_module_roots(provider),
                    strict=True,
                )
            ]
            missing_packages = [
                name
                for name in provider.python_packages
                if not self._package_importable(name, self.config.provider_environment(provider))
            ]
            if missing_packages:
                raise ForgeError(
                    "PROVIDER_RUNTIME_UNAVAILABLE",
                    "provider python_packages are not importable: " + ", ".join(missing_packages),
                )
            with self.observer.provider_workspace() as workspace:
                execution = self.executor.execute(
                    [str(executable), *provider.command[1:]],
                    cwd=Path(workspace),
                    timeout=self.config.timeout,
                    output_cap=self.config.output_cap,
                    environment=self.config.provider_environment(provider),
                    stdin=canonical_bytes(request) + b"\n",
                )
            if execution.returncode != 0:
                raise ForgeError(
                    "PROVIDER_EXIT",
                    f"provider exited {execution.returncode}: "
                    + redact(execution.stderr.decode("utf-8", errors="replace")),
                )
            response = parse_provider_capabilities(execution.stdout)
            response_identity = dict(response["provider"])
            if not any(
                isinstance(response_identity.get(key), str) and response_identity.get(key)
                for key in ("id", "name")
            ) or not any(
                isinstance(response_identity.get(key), str) and response_identity.get(key)
                for key in ("identity", "version")
            ):
                raise ForgeError(
                    "PROVIDER_MALFORMED",
                    "provider probe requires a name/id and an identity/version",
                )
            if (
                provider.identity is not None
                and response_identity.get("identity") != provider.identity
            ):
                raise ForgeError(
                    "PROVIDER_IDENTITY_DRIFT",
                    f"provider {provider.provider_id} reported a different identity",
                )
            if (
                provider.version is not None
                and response_identity.get("version") != provider.version
            ):
                raise ForgeError(
                    "PROVIDER_IDENTITY_DRIFT",
                    f"provider {provider.provider_id} reported a different version",
                )
            extensions = dict(response["extensions"])
            unsupported = list(extensions.get("unsupported_constructs", []))
            record: dict[str, object] = {
                **self._declared_model(provider),
                "availability": "AVAILABLE",
                "status": "PASS",
                "probe_kind": "provider-protocol-capabilities",
                "provider_identity": response_identity,
                "executable": str(executable),
                "executable_identity": executable_identity,
                "extensions": {
                    **extensions,
                    "mncs_forge": {
                        **dict(extensions.get("mncs_forge") or {}),
                        "family_module_roots": runtime_modules,
                        "python_packages": list(provider.python_packages),
                    },
                },
                "probed_capabilities": list(response["analyses"]),
                "supported_constructs": list(
                    extensions.get("supported_constructs", provider.supported_constructs)
                ),
                "unsupported_constructs": sorted(
                    set([*provider.unsupported_constructs, *unsupported])
                ),
                "limitations": [
                    *provider.limitations,
                    *list(extensions.get("limitations", [])),
                    "capability-probe PASS is not analysis or conformance PASS",
                ],
                "protocol_statuses": list(response["statuses"]),
                "cancellation": bool(response["cancellation"]),
                "health_checks": bool(response["health_checks"]),
                "duration_seconds": execution.duration_seconds,
                "stderr_diagnostic": redact(execution.stderr.decode("utf-8", errors="replace")),
                "returncode": execution.returncode,
                "recorded_at": now(),
            }
            return self._record_probe(record).to_object_dict()
        except ForgeError as exc:
            record = {
                **self._declared_model(provider),
                "availability": (
                    "UNAVAILABLE"
                    if exc.code in {"PROVIDER_UNAVAILABLE", "COMMAND_START"}
                    else "UNKNOWN"
                ),
                "status": "UNKNOWN",
                "probe_kind": "provider-protocol-capabilities",
                "provider_identity": None,
                "executable": str(executable) if executable else None,
                "executable_identity": executable_identity,
                "probed_capabilities": [],
                "limitations": [*provider.limitations, exc.message],
                "error_code": exc.code,
                "recorded_at": now(),
            }
            return self._record_probe(record).to_object_dict()

    def capability_blockers(
        self, required_capabilities: list[str] | None = None
    ) -> dict[str, object]:
        required = sorted(set([*self.config.required_capabilities, *(required_capabilities or [])]))
        inventory = {
            provider.provider_id: self._inventory_item(provider)
            for provider in self.config.providers.values()
        }
        blockers: list[dict[str, object]] = []
        satisfied: list[dict[str, object]] = []
        informational: list[dict[str, object]] = []
        if self.native_capability_policy is None:
            for provider in self.config.providers.values():
                item = inventory[provider.provider_id]
                if provider.required and item["status"] != "PASS":
                    blockers.append(
                        {
                            "kind": "required_provider",
                            "provider_id": provider.provider_id,
                            "status": "UNKNOWN",
                            "problem": (
                                "required provider is unavailable, unprobed, stale, or inconclusive"
                            ),
                        }
                    )
                elif not provider.required and item["availability"] != "AVAILABLE":
                    informational.append(
                        {
                            "kind": "optional_provider",
                            "provider_id": provider.provider_id,
                            "status": "UNKNOWN",
                            "problem": "optional provider is unavailable",
                        }
                    )
        else:
            for provider in self.config.providers.values():
                item = inventory[provider.provider_id]
                decision = self.native_capability_policy(
                    None,
                    declared_capabilities=(),
                    probed_capabilities=(),
                    unsupported_constructs=(),
                    provider_status=str(item["status"]),
                    provider_required=provider.required,
                    provider_available=item["availability"] == "AVAILABLE",
                )
                if decision.required_blocker:
                    blockers.append(
                        {
                            "kind": "required_provider",
                            "provider_id": provider.provider_id,
                            "status": "UNKNOWN",
                            "problem": (
                                "required provider is unavailable, unprobed, stale, or inconclusive"
                            ),
                        }
                    )
                elif decision.optional_unavailable:
                    informational.append(
                        {
                            "kind": "optional_provider",
                            "provider_id": provider.provider_id,
                            "status": "UNKNOWN",
                            "problem": "optional provider is unavailable",
                        }
                    )
        for capability in required:
            if self.native_capability_policy is None:
                candidates = [
                    item
                    for provider_id, item in inventory.items()
                    if capability in self.config.providers[provider_id].capabilities
                ]
                established = [
                    item
                    for item in candidates
                    if item["status"] == "PASS"
                    and isinstance(item["last_probe_result"], dict)
                    and capability in item["last_probe_result"].get("probed_capabilities", [])
                    and capability
                    not in item["last_probe_result"].get("unsupported_constructs", [])
                ]
                candidate_provider_ids = sorted(str(item["provider_id"]) for item in candidates)
                established_provider_ids = sorted(
                    str(item["provider_id"]) for item in established
                )
            else:
                policy_results: dict[str, NativeProviderCapabilityDecision] = {}
                for provider_id, item in inventory.items():
                    provider = self.config.providers[provider_id]
                    probe = item["last_probe_result"]
                    probed = (
                        probe.get("probed_capabilities", []) if isinstance(probe, dict) else []
                    )
                    unsupported = (
                        probe.get("unsupported_constructs", []) if isinstance(probe, dict) else []
                    )
                    policy_results[provider_id] = self.native_capability_policy(
                        capability,
                        declared_capabilities=provider.capabilities,
                        probed_capabilities=probed,
                        unsupported_constructs=unsupported,
                        provider_status=str(item["status"]),
                        provider_required=provider.required,
                        provider_available=item["availability"] == "AVAILABLE",
                    )
                candidate_provider_ids = sorted(
                    provider_id
                    for provider_id, decision in policy_results.items()
                    if decision.candidate
                )
                established_provider_ids = sorted(
                    provider_id
                    for provider_id, decision in policy_results.items()
                    if decision.established
                )
            if established_provider_ids:
                satisfied.append(
                    {
                        "capability": capability,
                        "status": "PASS",
                        "providers": established_provider_ids,
                        "scope": "validated capability discovery only",
                    }
                )
            else:
                blockers.append(
                    {
                        "kind": "required_capability",
                        "capability": capability,
                        "status": "UNKNOWN",
                        "providers": candidate_provider_ids,
                        "problem": (
                            "no current recognized provider probe established this capability"
                        ),
                    }
                )
        return {
            "required_capabilities": required,
            "satisfied": satisfied,
            "blockers": blockers,
            "informational_limitations": informational,
            "blocked": bool(blockers),
            "status": (
                self._aggregate_status(str(item["status"]) for item in [*satisfied, *blockers])
                if required or blockers
                else "PASS"
            ),
            "no_requirement_note": (
                "PASS with no required capabilities means only that no capability policy "
                "is blocked; it is not structural-analysis evidence"
                if not required and not blockers
                else None
            ),
            "missing_is_pass": False,
            "dominance": "FAIL > UNKNOWN > PASS",
        }
