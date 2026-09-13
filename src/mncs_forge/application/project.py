"""Project inspection and lifecycle presentation application service."""

from __future__ import annotations

import os

from .. import __version__
from ..config import ForgeConfig
from ..errors import ForgeError
from ..ports import ProjectObserver, RecordReader, Runner, VerifierCatalog
from .lifecycle import LifecycleContext
from .providers import ProviderService
from .support import PUBLIC_LIMITATIONS, redact


class ProjectService:
    def __init__(
        self,
        *,
        config: ForgeConfig,
        mode: str,
        records: RecordReader,
        executor: Runner,
        observer: ProjectObserver,
        lifecycle: LifecycleContext,
        providers: ProviderService,
        verifiers: VerifierCatalog,
    ) -> None:
        self.config = config
        self.mode = mode
        self.records = records
        self.executor = executor
        self.observer = observer
        self.lifecycle = lifecycle
        self.providers = providers
        self.verifiers = verifiers

    def _command_version(self, command: list[str]) -> str:
        try:
            result = self.executor.execute(
                [*command, "version"],
                cwd=self.config.root,
                timeout=min(self.config.timeout, 10),
                output_cap=min(self.config.output_cap, 65536),
                environment={"PATH": os.environ.get("PATH", "")},
            )
            if result.returncode != 0:
                result = self.executor.execute(
                    [*command, "--version"],
                    cwd=self.config.root,
                    timeout=min(self.config.timeout, 10),
                    output_cap=min(self.config.output_cap, 65536),
                    environment={"PATH": os.environ.get("PATH", "")},
                )
            return redact(result.stdout.decode("utf-8", errors="replace").strip(), 512) or "UNKNOWN"
        except ForgeError:
            return "UNKNOWN"

    def doctor(self) -> dict[str, object]:
        try:
            ledger = self.records.verify()
        except ForgeError as exc:
            ledger = exc.as_dict()
        commands = {
            name: {"argv": value, "executable": self.observer.command_path(value)}
            for name, value in self.config.public_commands().items()
        }
        return {
            "ok": bool(ledger.get("ok", False)),
            "forge_version": __version__,
            "config": str(self.config.config_path),
            "project_root": str(self.config.root),
            "mode": self.mode,
            "ledger": ledger,
            "commands": commands,
            "native_execution": self.lifecycle.native_status(),
            "network_required": False,
            "limitations": PUBLIC_LIMITATIONS,
        }

    def state_inspect(self) -> dict[str, object]:
        return self.lifecycle.machine().inspect()

    def inspect(self) -> dict[str, object]:
        state_machine = self.lifecycle.machine()
        lifecycle = state_machine.inspect()
        epoch = state_machine.projection.active_epoch
        candidate = state_machine.projection.current_candidate
        commands = self.config.public_commands()
        providers = self.providers.inventory()
        return {
            "project": {
                "name": self.config.project_name,
                "identity": self.config.project_identity,
                "root": str(self.config.root),
            },
            "mode": self.mode,
            "native_execution": self.lifecycle.native_status(),
            "configured_paths": self.config.path_values,
            "candidate_write_boundaries": [
                str(path) for path in self.config.relative_scopes("candidates", "generated")
            ],
            "protected_authorities": [
                str(path)
                for path in self.config.relative_scopes(
                    "contracts",
                    "references",
                    "evaluators",
                    "acceptance_policies",
                    "protected",
                )
            ],
            "declared_workflows": [
                {
                    "name": item.name,
                    "category": item.category,
                    "mode": item.mode,
                    "provider_protocol": item.provider_protocol,
                    "subject": item.subject,
                }
                for item in self.config.workflows.values()
            ],
            "current_epoch": epoch.to_object_dict() if epoch is not None else None,
            "active_candidate": candidate.to_object_dict() if candidate is not None else None,
            "lifecycle": lifecycle,
            "available_public_commands": list(commands),
            "detected_versions": {
                name: self._command_version(command) for name, command in commands.items()
            },
            "configured_providers": providers["providers"],
            "required_capabilities": self.config.required_capabilities,
            "provider_discovery_status": providers["status"],
            "declared_micro_verifiers": self.verifiers.list_declared()["verifiers"],
            "limitations": PUBLIC_LIMITATIONS,
        }
