# Forge storage boundary

Ordinary Forge persistence is provided by `StoreBackedRecordStore` over the
supported `mncs_store.EmbeddedStore` package. Forge authorizes and constructs
typed domain records; Store decides durable object identity, content integrity,
generation publication, recovery, and stale-writer outcomes.

The adapter preserves the Forge-facing `RecordReader`/`RecordCommitter` ports:
records are projected from the current Store generation in ordinal order,
duplicates remain idempotent, and a same-identity content change is rejected.
Forge's derived `ledger-index.json` is disposable and rebuilt from Store state. It records the
native Store commit-feed identity alongside the generation so a later `mncs-index` projection can
detect whether its snapshot is current without becoming persistence authority.

The legacy `LocalRecordStore`/`Ledger` JSONL implementation is not constructed
by the normal `Forge` composition root. It remains isolated for differential
tests and the explicit `migration.import_legacy_state` boundary so historical
fixtures can be authenticated and imported with source representation and
transformation provenance. It is not a second active persistence authority.

Per-action file locks remain in the adapter because serializing one verifier
action is Forge workflow semantics. They do not replace Store's generation
compare-and-transition publication lock.
