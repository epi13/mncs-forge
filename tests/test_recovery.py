from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_record_store import candidate

from mncs_forge.config import ForgeConfig
from mncs_forge.engine import Forge
from mncs_forge.errors import ForgeError
from mncs_forge.ledger import Ledger
from mncs_forge.record_store import LocalRecordStore


class InjectedFailure(RuntimeError):
    pass


@pytest.mark.parametrize(
    ("failpoint", "committed"),
    [
        ("before_prepare", False),
        ("after_record_staged", False),
        ("after_ledger_staged", False),
        ("after_prepared", True),
        ("after_record_published", True),
        ("before_ledger_published", True),
        ("after_ledger_published", True),
        ("before_committed", True),
        ("after_committed", True),
        ("during_index_update", True),
    ],
)
def test_every_commit_failpoint_recovers_to_complete_old_or_new_state(
    tmp_path: Path, failpoint: str, committed: bool
) -> None:
    def inject(name: str) -> None:
        if name == failpoint:
            raise InjectedFailure(name)

    store = LocalRecordStore(tmp_path, failpoint=inject)
    with pytest.raises(InjectedFailure):
        store.commit("candidates", "candidate", candidate(1))

    recovered = LocalRecordStore(tmp_path)
    verification = Ledger(tmp_path).verify()
    assert verification["entries"] == (1 if committed else 0)
    assert recovered.recover() == {"completed": 0, "abandoned": 0}
    record_files = list((tmp_path / "records" / "candidates").glob("*.json"))
    assert len(record_files) == (1 if committed else 0)


def test_malformed_and_truncated_journals_fail_closed(tmp_path: Path) -> None:
    tx_dir = tmp_path / "transactions" / ("a" * 32)
    tx_dir.mkdir(parents=True)
    (tx_dir / "journal.json").write_text('{"state":', encoding="utf-8")

    with pytest.raises(ForgeError) as issue:
        LocalRecordStore(tmp_path)
    assert issue.value.code == "RECOVERY_JOURNAL_MALFORMED"
    assert (tx_dir / "journal.json").exists()


def test_staged_payload_substitution_fails_closed(tmp_path: Path) -> None:
    def inject(name: str) -> None:
        if name == "after_prepared":
            raise InjectedFailure(name)

    with pytest.raises(InjectedFailure):
        LocalRecordStore(tmp_path, failpoint=inject).commit("candidates", "candidate", candidate(1))
    with pytest.raises(ForgeError) as unresolved:
        Ledger(tmp_path).records()
    assert unresolved.value.code == "RECOVERY_REQUIRED"
    tx_dir = next((tmp_path / "transactions").iterdir())
    (tx_dir / "record.stage").write_bytes(b"substituted\n")

    with pytest.raises(ForgeError) as issue:
        LocalRecordStore(tmp_path)
    assert issue.value.code == "RECOVERY_STAGE_MISMATCH"


def test_prepared_transaction_against_unexpected_ledger_fails_closed(tmp_path: Path) -> None:
    def inject(name: str) -> None:
        if name == "after_prepared":
            raise InjectedFailure(name)

    with pytest.raises(InjectedFailure):
        LocalRecordStore(tmp_path, failpoint=inject).commit("candidates", "candidate", candidate(1))
    Ledger(tmp_path).append("candidate", candidate(2))

    with pytest.raises(ForgeError) as issue:
        LocalRecordStore(tmp_path)
    assert issue.value.code == "RECOVERY_LEDGER_CONFLICT"


@pytest.mark.parametrize("index_value", [b"not-json\n", b'{"schema_version":1}\n'])
def test_stale_or_corrupt_index_is_rebuilt(tmp_path: Path, index_value: bytes) -> None:
    store = LocalRecordStore(tmp_path)
    store.commit("candidates", "candidate", candidate(1))
    index_path = tmp_path / "ledger-index.json"
    index_path.write_bytes(index_value)

    LocalRecordStore(tmp_path)

    rebuilt = json.loads(index_path.read_bytes())
    assert rebuilt["entry_count"] == 1
    assert rebuilt["ledger_head"] == Ledger(tmp_path).records()[-1].entry_hash


def test_corrupt_index_is_never_authoritative_for_reads(tmp_path: Path) -> None:
    store = LocalRecordStore(tmp_path)
    store.commit("candidates", "candidate", candidate(1))
    index_path = tmp_path / "ledger-index.json"
    index = json.loads(index_path.read_bytes())
    index["sequences_by_kind"] = {"candidate": [999]}
    index_path.write_text(json.dumps(index) + "\n", encoding="utf-8")

    records = Ledger(tmp_path).records("candidate")

    assert len(records) == 1
    assert records[0].payload["candidate_id"] == candidate(1)["candidate_id"]


def test_durable_verifier_action_is_recovered_once_as_terminal_unknown(
    config: ForgeConfig,
) -> None:
    armed = False

    def inject(name: str) -> None:
        if armed and name == "after_committed":
            raise InjectedFailure(name)

    store = LocalRecordStore(config.state_dir, failpoint=inject)
    forge = Forge(config, record_store=store)
    forge.epoch_begin(
        generator_identity="generator-v1",
        evaluator_identity="evaluator-v1",
    )
    forge.candidate_register(
        changed_files=["candidate/main.py"],
        hypothesis="fixture",
        generator_identity="generator-v1",
        generator_config_identity="configuration-v1",
    )
    armed = True
    with pytest.raises(InjectedFailure):
        forge.verifier_run("verify-pass", changed_paths=["candidate/main.py"], scope="file")

    reopened = Forge(config, record_store=LocalRecordStore(config.state_dir))
    results = reopened.ledger.records("verifier_result")
    assert len(results) == 1
    assert results[0].payload["status"] == "UNKNOWN"
    assert results[0].payload["operational_error"]["code"] == "VERIFIER_ACTION_STRANDED"  # type: ignore[index]
    assert len(Forge(config, record_store=LocalRecordStore(config.state_dir)).ledger.records("verifier_result")) == 1
