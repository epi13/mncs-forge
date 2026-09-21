"""Explicit migration-only readers for historical Forge state."""

from __future__ import annotations

import hashlib
from pathlib import Path

from .ledger import Ledger
from .records import LedgerEntry
from .store_record_store import StoreBackedRecordStore


def import_legacy_state(
    source_state_dir: Path,
    destination: StoreBackedRecordStore,
) -> list[LedgerEntry]:
    """Import one historical Forge JSONL state into current Store state.

    This function is intentionally outside the active ``RecordStore`` path.
    It validates the old hash-linked history, derives one source
    representation identity, and then hands typed records to the Store
    adapter.  After import, ordinary Forge reads and writes use Store only.
    """

    source = Path(source_state_dir)
    ledger = Ledger(source)
    ledger.verify()
    ledger_bytes = ledger.path.read_bytes() if ledger.path.is_file() else b""
    source_identity = f"forge-legacy-jsonl-sha256:{hashlib.sha256(ledger_bytes).hexdigest()}"
    return destination.import_legacy_entries(
        ledger.records(),
        source_representation_identity=source_identity,
    )
