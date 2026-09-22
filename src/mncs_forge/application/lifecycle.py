"""Construct one lifecycle projection from verified history and current observations."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from ..mncs_native import NativeForgeAdapter
from ..ports import ProjectObserver, RecordReader, record_by_id
from ..records import ForgeRecord, LedgerEntry
from ..state_machine import ForgeStateMachine


class LifecycleContext:
    """Shared application collaborator; transition policy remains in `ForgeStateMachine`."""

    def __init__(
        self,
        *,
        mode: str,
        records: RecordReader,
        observer: ProjectObserver,
        native: NativeForgeAdapter | None = None,
        native_mode: str = "prefer",
        root: Path | None = None,
    ) -> None:
        self.mode = mode
        self.records = records
        self.observer = observer
        self.native = native
        self.native_mode = native_mode
        self.root = root
        self._machine_cache_key: tuple[object, ...] | None = None
        self._machine_cache: ForgeStateMachine | None = None

    def native_status(self) -> dict[str, object]:
        if self.native is None:
            if self.native_mode == "off":
                return {
                    "mode": self.native_mode,
                    "selected": False,
                    "available": False,
                    "reason": "disabled",
                }
            if self.root is None:
                return {
                    "mode": self.native_mode,
                    "selected": False,
                    "available": False,
                    "reason": "NATIVE_UNAVAILABLE",
                }
            return NativeForgeAdapter(self.root).status(self.native_mode)
        return self.native.status(self.native_mode)

    def machine(
        self,
        *,
        observe_epoch_authority: bool = True,
        observe_freeze_bindings: bool = True,
        observe_policy: bool = True,
        history_kinds: frozenset[str] | None = None,
    ) -> ForgeStateMachine:
        policy_identity, required_evidence, policy_error = (
            self.observer.selection_evidence_policy() if observe_policy else ("", (), None)
        )
        environment_keys, environment_identities, policy_identities = (
            self.observer.evidence_envelopes() if observe_policy else ({}, {}, {})
        )
        current_candidate_identity = self.observer.current_candidate_identity()
        history = (
            self.records.records_for(history_kinds)
            if history_kinds is not None
            else self.records.records()
        )
        current_authority_identities = (
            self.observer.current_authority_identities() if observe_epoch_authority else {}
        )
        current_freeze = next(
            (entry.payload for entry in reversed(history) if entry.kind == "freeze"), None
        )
        current_freeze_bindings = (
            self.observer.current_freeze_bindings(
                current_candidate_identity,
                current_freeze if isinstance(current_freeze, Mapping) else None,
            )
            if observe_freeze_bindings
            else {}
        )
        native_identity = (
            self.native.semantic_input_identity() if self.native is not None else None
        )
        cache_key = (
            self.mode,
            observe_epoch_authority,
            observe_freeze_bindings,
            observe_policy,
            history_kinds,
            tuple(
                (
                    entry.sequence,
                    entry.entry_hash,
                    entry.kind,
                    entry.payload.record_type.value,
                )
                for entry in history
            ),
            current_candidate_identity,
            tuple(sorted(current_authority_identities.items())),
            tuple(sorted(current_freeze_bindings.items())),
            policy_identity,
            required_evidence,
            policy_error,
            tuple(sorted(environment_keys.items())),
            tuple(sorted(environment_identities.items())),
            tuple(sorted(policy_identities.items())),
            native_identity,
        )
        if self._machine_cache_key == cache_key and self._machine_cache is not None:
            return self._machine_cache
        machine = ForgeStateMachine(
            mode=self.mode,
            history=history,
            current_candidate_identity=current_candidate_identity,
            current_authority_identities=(
                self.observer.current_authority_identities() if observe_epoch_authority else {}
            ),
            current_freeze_bindings=(
                self.observer.current_freeze_bindings(
                    current_candidate_identity,
                    current_freeze if isinstance(current_freeze, Mapping) else None,
                )
                if observe_freeze_bindings
                else {}
            ),
            selection_policy_identity=policy_identity,
            required_evidence=required_evidence,
            selection_policy_error=policy_error,
            evidence_environment_keys=environment_keys,
            evidence_environment_identities=environment_identities,
            evidence_policy_identities=policy_identities,
            native=self.native,
        )
        self._machine_cache_key = cache_key
        self._machine_cache = machine
        return machine

    def record_by_id(self, kind: str, identity: str, key: str) -> ForgeRecord:
        return record_by_id(self.records, kind, identity, key)

    def records_of(self, kind: str) -> list[LedgerEntry]:
        return self.records.records(kind)

    def verify_freeze(self, freeze: Mapping[str, object]) -> None:
        from ..errors import ForgeError

        current, _ = self.machine(observe_epoch_authority=False).authorize_evaluator_entry()
        if current["freeze_id"] != freeze.get("freeze_id"):
            raise ForgeError("FREEZE_SUPERSEDED", "freeze is not the current lifecycle freeze")
