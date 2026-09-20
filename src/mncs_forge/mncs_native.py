"""Explicit host adapter for the MNCS-native Forge slice.

The adapter is intentionally narrow. It locates a sibling ``mncs-language``
checkout (or an explicitly configured one), invokes the language-owned CLI
through Forge's bounded runner, and returns the language's structured result.
It does not turn a compiler result into an assurance claim or implement a
second execution or hashing authority.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from collections.abc import Mapping, Sequence
from contextlib import ExitStack, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib.resources import as_file, files
from pathlib import Path
from typing import Any, TypedDict

from .errors import ForgeError
from .execution import run_bounded
from .serialization import reject_duplicate_keys

NATIVE_SCHEMA_VERSION = "0.1"
NATIVE_STATUS_CODES = {"PASS": 1, "FAIL": 2, "UNKNOWN": 3}
DEFAULT_TIMEOUT_SECONDS = 120.0
DEFAULT_OUTPUT_BYTES = 1_000_000
_MNCS_TYPE_PREFIX = "mncs:0.2:finite-type:"
_MNCS_VARIANT_PREFIX = "mncs:0.2:finite-variant:"
_STATUS_VARIANTS = {"PASS": 0, "FAIL": 1, "UNKNOWN": 2}
_LIFECYCLE_STAGES = {
    "NoEpoch": 0,
    "EpochActive": 1,
    "CandidateRegistered": 2,
    "EvidenceIncomplete": 3,
    "CandidateReady": 4,
    "CandidateSelected": 5,
    "CandidateRejected": 6,
    "CandidateFrozen": 7,
    "EvaluationComplete": 8,
    "AmbiguousHistory": 9,
}
_LIFECYCLE_OPERATIONS = {
    "BeginEpoch": 0,
    "RegisterCandidate": 1,
    "AddEvidence": 2,
    "SelectCandidate": 3,
    "RejectCandidate": 4,
    "FreezeCandidate": 5,
    "RecordEvaluation": 6,
}
_LIFECYCLE_MODULE = "mncs.forge.lifecycle.v1"
_RECONCILIATION_MODULE = "mncs.forge.reconciliation.v1"
_CORE_MODULE = "mncs.forge.core.v1"
_IDENTITY_MODULE = "mncs.core.identity.v1"


def _encode_identity_component(value: str) -> str:
    return "".join(
        chr(byte)
        if (chr(byte).isalnum() and byte < 128) or byte in (ord("_"), ord("-"), ord("."))
        else f"%{byte:02X}"
        for byte in value.encode("utf-8")
    )


def _semantic_identity(kind: str, *components: str) -> str:
    encoded = "::".join(_encode_identity_component(component) for component in components)
    return f"mncs:0.2:{kind}:{encoded}"


def _finite_type_identity(module: str, name: str) -> str:
    return _semantic_identity("finite-type", module, name)


def _finite_variant_identity(module: str, type_name: str, variant: str) -> str:
    return _semantic_identity("finite-variant", module, type_name, variant)


def _record_type_identity(module: str, name: str, fields: Mapping[str, str]) -> str:
    canonical = "".join(
        f"{field_name}:{field_type};" for field_name, field_type in sorted(fields.items())
    )
    return _semantic_identity("record-type", module, name, canonical)


_STAGE_TYPE = _finite_type_identity(_LIFECYCLE_MODULE, "Stage")
_OPERATION_TYPE = _finite_type_identity(_LIFECYCLE_MODULE, "Operation")
_EVENT_KIND_TYPE = _finite_type_identity(_LIFECYCLE_MODULE, "EventKind")
_DISPOSITION_TYPE = _finite_type_identity(_LIFECYCLE_MODULE, "Disposition")
_FRESHNESS_TYPE = _finite_type_identity(_LIFECYCLE_MODULE, "Freshness")
_STATUS_TYPE = _finite_type_identity("mncs.core.status.v1", "Status")
_DIGEST_TYPE = _record_type_identity(_IDENTITY_MODULE, "Digest32", {"bytes": "[byte; 32]"})
_HISTORY_EVENT_TYPE = _record_type_identity(
    _LIFECYCLE_MODULE,
    "HistoryEvent",
    {
        "candidate": _DIGEST_TYPE,
        "epoch": _DIGEST_TYPE,
        "kind": "EventKind",
        "parent_candidate": _DIGEST_TYPE,
        "parent_epoch": _DIGEST_TYPE,
        "status": _STATUS_TYPE,
    },
)
_PROJECTION_INPUT_TYPE = _record_type_identity(
    _LIFECYCLE_MODULE,
    "ProjectionInput",
    {
        "current_candidate": _DIGEST_TYPE,
        "event_count": "byte",
        "events": "[HistoryEvent; 32]",
        "required_evidence": "byte",
    },
)
_PROJECTION_STATE_TYPE = _record_type_identity(
    _LIFECYCLE_MODULE,
    "ProjectionState",
    {
        "active_epoch": _DIGEST_TYPE,
        "candidate_count": "i64",
        "current_candidate": _DIGEST_TYPE,
        "disposition": "Disposition",
        "epoch_count": "i64",
        "evaluated": "bool",
        "evidence": _STATUS_TYPE,
        "evidence_count": "i64",
        "frozen": "bool",
        "freshness": "Freshness",
        "lineage_ok": "bool",
        "parent_candidate": _DIGEST_TYPE,
        "parent_epoch": _DIGEST_TYPE,
        "stage": "Stage",
    },
)
_PROJECTION_RESULT_TYPE = _record_type_identity(
    _LIFECYCLE_MODULE,
    "ProjectionResult",
    {"projection": "ProjectionState", "reason": "byte", "status": _STATUS_TYPE},
)
_CATEGORY_INPUT_TYPE = _record_type_identity(
    _RECONCILIATION_MODULE,
    "CategoryInput",
    {
        "category": _DIGEST_TYPE,
        "count": "byte",
        "statuses": f"[{_STATUS_TYPE}; 8]",
        "unsupported_count": "byte",
    },
)
_CATEGORY_PROJECTION_TYPE = _record_type_identity(
    _RECONCILIATION_MODULE,
    "CategoryProjection",
    {
        "category": _DIGEST_TYPE,
        "conflict": "bool",
        "fail_count": "i64",
        "observed_count": "i64",
        "pass_count": "i64",
        "status": _STATUS_TYPE,
        "unknown_count": "i64",
        "unsupported_count": "i64",
        "valid": "bool",
    },
)
_RECONCILIATION_INPUT_TYPE = _record_type_identity(
    _RECONCILIATION_MODULE,
    "ReconciliationInput",
    {"categories": "[CategoryInput; 16]", "category_count": "byte"},
)
_RECONCILIATION_STATE_TYPE = _record_type_identity(
    _RECONCILIATION_MODULE,
    "ReconciliationState",
    {
        "categories": "[CategoryProjection; 16]",
        "category_count": "i64",
        "conflicting_category_count": "i64",
        "observed_count": "i64",
        "status": _STATUS_TYPE,
        "unsupported_count": "i64",
        "valid": "bool",
    },
)
_RECONCILIATION_RESULT_TYPE = _record_type_identity(
    _RECONCILIATION_MODULE,
    "ReconciliationResult",
    {"reason": "byte", "state": "ReconciliationState", "status": _STATUS_TYPE},
)
_FINITE_VARIANTS: dict[str, dict[str, int]] = {
    _STATUS_TYPE: _STATUS_VARIANTS,
    _STAGE_TYPE: _LIFECYCLE_STAGES,
    _OPERATION_TYPE: _LIFECYCLE_OPERATIONS,
    _EVENT_KIND_TYPE: {
        "Empty": 0,
        "EpochStarted": 1,
        "CandidateRegistered": 2,
        "EvidenceObserved": 3,
        "CandidateSelected": 4,
        "CandidateRejected": 5,
        "CandidateFrozen": 6,
        "EvaluationRecorded": 7,
    },
    _DISPOSITION_TYPE: {"Undisposed": 0, "Selected": 1, "Rejected": 2, "Conflict": 3},
    _FRESHNESS_TYPE: {
        "NotApplicable": 0,
        "Current": 1,
        "Stale": 2,
        "Unknown": 3,
    },
}
NATIVE_EXECUTION_CONTRACT = "mncs-forge.native-execution.v1"
NATIVE_LIFECYCLE_PROJECTION_CONTRACT = "mncs-forge.lifecycle-projection.v1"
NATIVE_RECONCILIATION_CONTRACT = "mncs-forge.reconciliation-projection.v1"
NATIVE_READINESS_CONTRACT = "mncs-forge.readiness-projection.v1"
NATIVE_BUNDLE_CONTRACT = "mncs-forge.bundle-preconditions.v1"
NATIVE_ASSURANCE_CONTRACT = "mncs-forge.assurance-loop.v1"
_ASSURANCE_WIRE_LENGTH = 24
_ASSURANCE_DECISIONS = {
    0: "StopPass",
    1: "StopFail",
    2: "StopUnknown",
    3: "RequestDiagnosis",
    4: "RequestRepair",
    5: "RequireReboundPlan",
    6: "RunSelectedVerification",
    7: "RequireFamilyProof",
    8: "Finalize",
}
_ASSURANCE_NEXT_ACTIONS = {
    0: "None",
    1: "Diagnose",
    2: "Repair",
    3: "ReboundPlan",
    4: "SelectedVerification",
    5: "FamilyProof",
}
_ASSURANCE_REASONS = {
    0: "None",
    1: "InitialPass",
    2: "InitialFail",
    3: "InitialEvidenceMissing",
    4: "FailingExecutionMissing",
    5: "DiagnosisMissing",
    6: "DiagnosisInsufficient",
    7: "RepairNotRequested",
    8: "RepairArgumentsIncomplete",
    9: "RepairInadmissible",
    10: "PlanStale",
    11: "SourceChangeMissing",
    12: "ReboundPlanMissing",
    13: "ReboundPlanStale",
    14: "PostRepairVerificationMissing",
    15: "PostRepairVerificationFail",
    16: "PostRepairVerificationUnknown",
    17: "FamilyProofMissing",
    18: "FamilyProofFail",
    19: "FamilyProofUnknown",
    20: "FamilyProofInsufficient",
    21: "IdentityBindingInvalid",
    22: "EvidenceStale",
    23: "ProviderEvidenceMissing",
    24: "AssuranceComplete",
    25: "InvalidInput",
}
_ASSURANCE_PHASES = {0: "InitialVerification", 1: "AfterRepair"}


@dataclass(frozen=True, slots=True)
class NativeInvocation:
    """One bounded language-owned invocation and its untrusted JSON payload."""

    command: tuple[str, ...]
    returncode: int
    stdout: bytes
    stderr: bytes
    payload: dict[str, Any] | None
    duration_seconds: float = 0.0

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and self.payload is not None


@dataclass(frozen=True, slots=True)
class NativeAssuranceInput:
    """Normalized, bounded facts crossing the Forge assurance membrane."""

    phase: str
    test_status: str
    post_repair_test_status: str
    debug_status: str
    family_proof_status: str
    test_present: bool
    failing_execution_present: bool
    post_repair_test_present: bool
    family_proof_present: bool
    plan_present: bool
    plan_current: bool
    repair_requested: bool
    repair_arguments_complete: bool
    repair_admissible: bool
    source_changed: bool
    rebound_plan_present: bool
    rebound_plan_current: bool
    proof_required: bool
    proof_sufficient: bool
    identity_binding_valid: bool
    evidence_fresh: bool
    provider_evidence_present: bool


@dataclass(frozen=True, slots=True)
class NativeAssuranceDecision:
    """Typed Forge-owned assurance result returned by the native application."""

    status: str
    decision: str
    next_action: str
    stop_reason: str
    phase: str
    terminal: bool
    valid: bool
    duration_seconds: float


@dataclass(frozen=True, slots=True)
class NativeLifecycleResult:
    """The validated result of one language-owned lifecycle preflight."""

    stage: str
    operation: str
    next_stage: str
    status: str
    reason: int


@dataclass(frozen=True, slots=True)
class NativeLifecycleProjection:
    """Typed MNCS projection of bounded Forge lifecycle history."""

    stage: str
    active_epoch: bytes
    parent_epoch: bytes
    current_candidate: bytes
    parent_candidate: bytes
    evidence: str
    disposition: str
    freshness: str
    lineage_ok: bool
    epoch_count: int
    candidate_count: int
    evidence_count: int
    frozen: bool
    evaluated: bool
    status: str
    reason: int


@dataclass(frozen=True, slots=True)
class NativeReconciliationCategory:
    """Typed MNCS projection for one bounded technical evidence category."""

    category: bytes
    status: str
    pass_count: int
    fail_count: int
    unknown_count: int
    observed_count: int
    conflict: bool
    unsupported_count: int


@dataclass(frozen=True, slots=True)
class NativeReconciliationProjection:
    """Typed MNCS projection of a bounded technical evidence envelope."""

    categories: tuple[NativeReconciliationCategory, ...]
    status: str
    category_count: int
    conflicting_category_count: int
    unsupported_count: int
    observed_count: int
    valid: bool
    reason: int


@dataclass(frozen=True, slots=True)
class NativeReadinessRequirement:
    """Typed MNCS readiness classification for one host-normalized requirement."""

    identity: bytes
    classification: str
    pass_count: int
    fail_count: int
    unknown_count: int
    observed_count: int
    stale: bool
    noncomparable: bool
    valid: bool


@dataclass(frozen=True, slots=True)
class NativeReadinessProjection:
    """Typed MNCS projection of the bounded evidence-readiness envelope."""

    requirements: tuple[NativeReadinessRequirement, ...]
    status: str
    reason: str
    present_count: int
    missing_count: int
    failed_count: int
    unknown_count: int
    stale_count: int
    noncomparable_count: int
    ready: bool
    valid: bool


@dataclass(frozen=True, slots=True)
class NativeBundlePreconditionProjection:
    """Typed MNCS projection of deterministic bundle authorization inputs."""

    ready: bool
    status: str
    reason: str
    evidence_status: str
    evidence_ready: bool
    valid: bool


class AbiParameterContract(TypedDict, total=False):
    """One compiler-emitted function parameter contract."""

    scalar: Mapping[str, object]
    finite: Mapping[str, object]
    record: Mapping[str, object]
    sequence: Mapping[str, object]
    view: Mapping[str, object]
    vector: Mapping[str, object]
    mask: Mapping[str, object]


class AbiFunctionContract(TypedDict):
    """The typed subset of a compiler-emitted function contract we consume."""

    function_identity: str
    declaring_module: str
    name: str
    inputs: list[AbiParameterContract]
    outputs: list[AbiParameterContract]


class NormalizedReadiness(TypedDict):
    """Validated host observations crossing the readiness ABI boundary."""

    records: Sequence[Mapping[str, object]]
    freshness: str
    comparable: bool
    environment_match: bool
    policy_match: bool
    authority_match: bool


@dataclass(frozen=True, slots=True)
class NativeAbi:
    """Language-emitted ABI metadata consumed at the Forge boundary."""

    source_artifact_identity: str
    module: str
    functions: Mapping[str, AbiFunctionContract]
    composites: Mapping[str, Mapping[str, object]]


_LIFECYCLE_CACHE: dict[tuple[object, ...], NativeLifecycleResult] = {}
_LIFECYCLE_PROJECTION_CACHE: dict[tuple[object, ...], NativeLifecycleProjection] = {}
_RECONCILIATION_CACHE: dict[tuple[object, ...], NativeReconciliationProjection] = {}
_READINESS_CACHE: dict[tuple[object, ...], NativeReadinessProjection] = {}
_BUNDLE_CACHE: dict[tuple[object, ...], NativeBundlePreconditionProjection] = {}
_ABI_CACHE: dict[tuple[object, ...], NativeAbi] = {}


def canonical_candidate_material(
    parent_digest: bytes,
    source_digest: bytes,
    status: str,
    changed_files: bytes,
) -> bytes:
    """Mirror the MNCS chunk contract for host-side differential checks.

    The byte order is declared by the packaged Forge MNCS serialization module. This
    helper only materializes bytes; SHA-256 remains an explicit host boundary.
    """

    if len(parent_digest) != 32 or len(source_digest) != 32:
        raise ValueError("parent_digest and source_digest must each be 32 bytes")
    if len(changed_files) != 4:
        raise ValueError("changed_files must be exactly 4 bytes")
    try:
        status_code = NATIVE_STATUS_CODES[status]
    except KeyError as exc:
        raise ValueError("status must be PASS, FAIL, or UNKNOWN") from exc
    return bytes((67, 1, status_code)) + parent_digest + source_digest + changed_files


def canonical_candidate_digest(
    parent_digest: bytes,
    source_digest: bytes,
    status: str,
    changed_files: bytes,
) -> bytes:
    """Hash host-materialized candidate bytes at the declared boundary."""

    return hashlib.sha256(
        canonical_candidate_material(parent_digest, source_digest, status, changed_files)
    ).digest()


class NativeForgeAdapter:
    """Invoke MNCS Language without changing the Forge compatibility surface."""

    def __init__(
        self,
        forge_root: Path,
        *,
        language_root: Path | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        output_bytes: int = DEFAULT_OUTPUT_BYTES,
    ) -> None:
        self.forge_root = forge_root.resolve()
        self.language_root = self._discover_language_root(language_root)
        self.timeout_seconds = timeout_seconds
        self.output_bytes = output_bytes
        self._assurance_cache_option_supported: bool | None = None
        self._resource_stack = ExitStack()
        configured_source = os.environ.get("MNCS_FORGE_NATIVE_SOURCE")
        if configured_source:
            self.native_source = Path(configured_source).expanduser().resolve()
            self.native_root = self.native_source.parent
        else:
            resource_root = files("mncs_forge.resources").joinpath("native", "forge")
            self.native_root = self._resource_stack.enter_context(as_file(resource_root))
            self.native_source = self.native_root / "core.mncs"
        self.assurance_descriptor = self.native_root / "assurance-application.json"

    def __del__(self) -> None:
        # ``as_file`` normally resolves to the installed filesystem.  The
        # context is still closed for zip-backed importers and test fixtures.
        stack = getattr(self, "_resource_stack", None)
        if stack is not None:
            with suppress(Exception):
                stack.close()

    @staticmethod
    def _discover_language_root(explicit: Path | None) -> Path | None:
        candidates: list[Path] = []
        configured = os.environ.get("MNCS_LANGUAGE_ROOT")
        if configured:
            candidates.append(Path(configured))
        if explicit is not None:
            candidates.append(explicit)
        candidates.append(Path(__file__).resolve().parents[3] / "mncs-language")
        for candidate in candidates:
            root = candidate.resolve()
            if (root / "Cargo.toml").is_file() and (root / "library").is_dir():
                return root
        return None

    @property
    def available(self) -> bool:
        return self.language_root is not None

    @property
    def source_available(self) -> bool:
        """Whether the packaged Forge MNCS entrypoint is available."""

        return self.native_source.is_file()

    @property
    def forge_modules_available(self) -> bool:
        return all(
            (self.native_root / name).is_file()
            for name in (
                "assurance.mncs",
                "assurance_application.mncs",
                "core.mncs",
                "bundle.mncs",
                "identity.mncs",
                "lifecycle.mncs",
                "reconciliation.mncs",
                "readiness.mncs",
                "records.mncs",
                "serialization.mncs",
            )
        ) and self.assurance_descriptor.is_file()

    def ensure_available(self) -> None:
        """Fail closed when required native execution cannot be selected."""

        if self.language_root is None:
            raise ForgeError("NATIVE_UNAVAILABLE", "mncs-language checkout is unavailable")
        if not self.forge_modules_available:
            raise ForgeError("NATIVE_UNAVAILABLE", "packaged Forge MNCS modules are unavailable")
        self._command()

    def status(self, mode: str) -> dict[str, object]:
        """Return an observable, non-authoritative native selection status."""

        if mode == "off":
            return {"mode": mode, "selected": False, "available": False, "reason": "disabled"}
        available = self.language_root is not None and self.forge_modules_available
        if available:
            try:
                command = self._command()
            except ForgeError as exc:
                return {
                    "mode": mode,
                    "selected": False,
                    "available": False,
                    "reason": exc.code,
                }
            return {
                "mode": mode,
                "selected": True,
                "available": True,
                "command": list(command),
                **self._selected_binary_observation(),
            }
        return {
            "mode": mode,
            "selected": False,
            "available": False,
            "reason": "NATIVE_UNAVAILABLE",
        }

    def _selected_binary_observation(self) -> dict[str, object]:
        """Describe the selected prebuilt binary for provenance inspection.

        The observation is best-effort host metadata, never semantic input:
        cache keys already bind the binary content through
        :meth:`semantic_input_identity`.
        """

        assert self.language_root is not None
        try:
            selected = self._command()[0]
        except (IndexError, ForgeError):
            return {}
        for relative in (Path("target/release/mncs"), Path("target/debug/mncs")):
            binary = self.language_root / relative
            try:
                if os.path.samefile(binary, selected):
                    modified = datetime.fromtimestamp(
                        binary.stat().st_mtime_ns / 1_000_000_000, tz=UTC
                    ).isoformat()
                    return {
                        "binary": binary.as_posix(),
                        "binary_modified_at": modified,
                    }
            except OSError:
                continue
        return {}

    def _candidate_binaries(self) -> list[Path]:
        """Return usable prebuilt CLI binaries, newest build first.

        A stale release build predating the current checkout or library
        sources fails elaboration of newer standard-library modules, so
        release builds do not unconditionally win over newer debug builds.
        Ordering is total (mtime, then path) to keep selection deterministic.
        """

        assert self.language_root is not None
        candidates: list[tuple[int, str, Path]] = []
        for relative in (Path("target/release/mncs"), Path("target/debug/mncs")):
            binary = self.language_root / relative
            try:
                if not (binary.is_file() and os.access(binary, os.X_OK)):
                    continue
                mtime_ns = binary.stat().st_mtime_ns
            except OSError:
                continue
            candidates.append((mtime_ns, binary.as_posix(), binary))
        candidates.sort(reverse=True)
        return [binary for _, _, binary in candidates]

    def _command(self) -> list[str]:
        configured = os.environ.get("MNCS_CLI")
        if configured:
            if "\x00" in configured:
                raise ForgeError("NATIVE_CONFIG_INVALID", "MNCS_CLI contains NUL")
            return [configured]
        if self.language_root is None:
            raise ForgeError(
                "NATIVE_UNAVAILABLE",
                "mncs-language sibling checkout is unavailable; native Forge is UNKNOWN",
            )
        cargo = shutil.which("cargo")
        if cargo is None:
            raise ForgeError("NATIVE_UNAVAILABLE", "cargo is not available")
        if self.source_available:
            candidates = self._candidate_binaries()
            if candidates:
                # The CLI loads Forge source at invocation time. Its
                # timestamp therefore does not need to track the source
                # checkout, and using the built binary keeps lifecycle
                # preflights bounded without a cargo rebuild per call.
                return [str(candidates[0])]
        return [
            cargo,
            "run",
            "--quiet",
            "--manifest-path",
            str(self.language_root / "Cargo.toml"),
            "-p",
            "mncs-cli",
            "--",
        ]

    def invoke(self, arguments: list[str], *, stdin: bytes = b"") -> NativeInvocation:
        if self.language_root is None:
            raise ForgeError(
                "NATIVE_UNAVAILABLE",
                "mncs-language sibling checkout is unavailable; native Forge is UNKNOWN",
            )
        if not self.forge_root.is_dir():
            raise ForgeError("NATIVE_CONFIG_INVALID", "Forge root is not a directory")
        command = [*self._command(), *arguments]
        environment = dict(os.environ)
        library_path = os.pathsep.join((str(self.language_root / "library"), str(self.native_root)))
        environment["MNCS_LIBRARY_PATH"] = library_path
        result = run_bounded(
            command,
            cwd=self.forge_root,
            timeout=self.timeout_seconds,
            output_cap=self.output_bytes,
            stderr_cap=self.output_bytes,
            environment=environment,
            stdin=stdin,
        )
        payload: dict[str, Any] | None = None
        try:
            decoded = result.stdout.decode("utf-8")
            value = json.loads(decoded, object_pairs_hook=reject_duplicate_keys)
            if isinstance(value, dict):
                payload = value
        except (UnicodeDecodeError, ValueError):
            payload = None
        return NativeInvocation(
            command=tuple(command),
            returncode=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
            payload=payload,
            duration_seconds=result.duration_seconds,
        )

    @staticmethod
    def _content_identity(paths: list[Path]) -> str:
        digest = hashlib.sha256()
        for path in sorted(path for path in paths if path.is_file()):
            relative = path.as_posix().encode("utf-8")
            digest.update(len(relative).to_bytes(4, "big"))
            digest.update(relative)
            content = path.read_bytes()
            digest.update(len(content).to_bytes(8, "big"))
            digest.update(content)
        return digest.hexdigest()

    def semantic_input_identity(self) -> str:
        """Identify every source/runtime input that can affect a native result."""

        self.ensure_available()
        assert self.language_root is not None
        forge_sources = list(self.native_root.glob("*.mncs"))
        library_sources = list((self.language_root / "library").rglob("*.mncs"))
        compiler_sources = list((self.language_root / "crates").rglob("*.rs"))
        manifest_sources = [
            self.language_root / "Cargo.toml",
            self.language_root / "Cargo.lock",
        ]
        command = self._command()
        command_identity: list[Path] = []
        if command and Path(command[0]).is_file():
            command_identity.append(Path(command[0]))
        identity = {
            "contract": NATIVE_EXECUTION_CONTRACT,
            "forge_sources": self._content_identity(forge_sources),
            "library_sources": self._content_identity(library_sources),
            "compiler_sources": self._content_identity(compiler_sources + manifest_sources),
            "command": command,
            "command_content": self._content_identity(command_identity),
        }
        return hashlib.sha256(json.dumps(identity, sort_keys=True).encode("utf-8")).hexdigest()

    def source_study(self, source: Path, *, node_id: str = "forge-native") -> NativeInvocation:
        return self.invoke(["source-study", str(source.resolve()), "--node-id", node_id])

    def execute(self, source: Path, request: Path, *, backend: bool = False) -> NativeInvocation:
        command = "execute-backend" if backend else "execute"
        return self.invoke([command, str(source.resolve()), str(request.resolve())])

    @staticmethod
    def _assurance_wire(value: NativeAssuranceInput) -> bytes:
        """Encode the fixed transport membrane for the generic run-app entrypoint."""

        if value.phase not in _ASSURANCE_PHASES.values():
            raise ForgeError("NATIVE_ASSURANCE_INPUT", "assurance phase is invalid")
        status_codes = _STATUS_VARIANTS
        statuses = (
            value.test_status,
            value.post_repair_test_status,
            value.debug_status,
            value.family_proof_status,
        )
        if any(status not in status_codes for status in statuses):
            raise ForgeError("NATIVE_ASSURANCE_INPUT", "assurance status is invalid")
        flags = (
            value.test_present,
            value.failing_execution_present,
            value.post_repair_test_present,
            value.family_proof_present,
            value.plan_present,
            value.plan_current,
            value.repair_requested,
            value.repair_arguments_complete,
            value.repair_admissible,
            value.source_changed,
            value.rebound_plan_present,
            value.rebound_plan_current,
            value.proof_required,
            value.proof_sufficient,
            value.identity_binding_valid,
            value.evidence_fresh,
            value.provider_evidence_present,
        )
        if not all(isinstance(flag, bool) for flag in flags):
            raise ForgeError("NATIVE_ASSURANCE_INPUT", "assurance presence flags are invalid")
        phase_code = 0 if value.phase == "InitialVerification" else 1
        wire = bytes(
            (
                70,
                1,
                phase_code,
                status_codes[value.test_status],
                status_codes[value.post_repair_test_status],
                status_codes[value.debug_status],
                status_codes[value.family_proof_status],
                *(1 if flag else 0 for flag in flags),
            )
        )
        if len(wire) != _ASSURANCE_WIRE_LENGTH:
            raise ForgeError("NATIVE_ASSURANCE_INPUT", "assurance wire shape is invalid")
        return wire

    def _assurance_cache_dir(self) -> Path:
        configured = os.environ.get("MNCS_NATIVE_APPLICATION_CACHE_DIR")
        if configured:
            path = Path(configured).expanduser()
            return path if path.is_absolute() else self.forge_root / path
        return self.forge_root / ".mncs" / "cache" / "native-applications"

    def assurance_loop(self, value: NativeAssuranceInput) -> NativeAssuranceDecision:
        """Run the canonical Forge assurance state machine through ``mncs run-app``."""

        if not isinstance(value, NativeAssuranceInput):
            raise ForgeError("NATIVE_ASSURANCE_INPUT", "assurance input is not typed")
        self.ensure_available()
        wire = self._assurance_wire(value)
        cache_dir = self._assurance_cache_dir()
        arguments = [
            "run-app",
            str(self.assurance_descriptor.resolve()),
            "--library",
            str((self.language_root / "library").resolve()),
            "--library",
            str(self.native_root.resolve()),
        ]
        if self._assurance_cache_option_supported is not False:
            arguments.extend(("--cache-dir", str(cache_dir)))
        invocation = self.invoke(arguments, stdin=wire)
        if (
            self._assurance_cache_option_supported is not False
            and invocation.returncode == 2
            and not invocation.stdout
            and b"unknown launcher option" in invocation.stderr
            and b"--cache-dir" in invocation.stderr
        ):
            # Older already-built CLIs predate the generic cache flag but still
            # expose the same run-app contract. Keep this compatibility path
            # bounded; current CLIs take the cache-enabled path above.
            self._assurance_cache_option_supported = False
            invocation = self.invoke(arguments[:6], stdin=wire)
        elif invocation.returncode == 0:
            self._assurance_cache_option_supported = True
        if invocation.returncode != 0 or len(invocation.stdout) != 6:
            raise ForgeError(
                "NATIVE_ASSURANCE_UNKNOWN",
                "native Forge assurance application did not return its bounded decision "
                f"(returncode {invocation.returncode}, bytes {len(invocation.stdout)})",
            )
        result = invocation.stdout
        if result[0] != 1:
            raise ForgeError(
                "NATIVE_ASSURANCE_UNKNOWN", "native assurance result schema is invalid"
            )
        status = {0: "PASS", 1: "FAIL", 2: "UNKNOWN"}.get(result[1])
        decision = _ASSURANCE_DECISIONS.get(result[2])
        next_action = _ASSURANCE_NEXT_ACTIONS.get(result[3])
        stop_reason = _ASSURANCE_REASONS.get(result[4])
        phase = _ASSURANCE_PHASES.get(result[5])
        if None in (status, decision, next_action, stop_reason, phase):
            raise ForgeError(
                "NATIVE_ASSURANCE_UNKNOWN",
                "native assurance result contains an unknown code",
            )
        terminal = decision in {"StopPass", "StopFail", "StopUnknown", "Finalize"}
        return NativeAssuranceDecision(
            status=str(status),
            decision=str(decision),
            next_action=str(next_action),
            stop_reason=str(stop_reason),
            phase=str(phase),
            terminal=terminal,
            valid=True,
            duration_seconds=invocation.duration_seconds,
        )

    @staticmethod
    def _abi_shape(abi: NativeAbi, kind: str, name: str, *, context: str) -> Mapping[str, object]:
        for key, contract in abi.composites.items():
            shape = contract.get(kind)
            if isinstance(shape, Mapping) and (
                shape.get("name") == name or (kind == "finite" and key == name)
            ):
                return shape
        raise ForgeError("NATIVE_ABI_UNKNOWN", f"{context} is absent from language-owned ABI")

    @staticmethod
    def _abi_object_mapping(value: object, *, context: str) -> dict[str, object]:
        """Narrow one untrusted JSON object to a string-keyed object map."""

        if not isinstance(value, Mapping):
            raise ForgeError("NATIVE_ABI_UNKNOWN", f"{context} is not an object")
        result: dict[str, object] = {}
        for key, child in value.items():
            if not isinstance(key, str):
                raise ForgeError("NATIVE_ABI_UNKNOWN", f"{context} has a non-string key")
            result[key] = child
        return result

    @classmethod
    def _abi_parameters(cls, value: object, *, context: str) -> list[AbiParameterContract]:
        """Validate and type compiler-emitted function value contracts."""

        if not isinstance(value, list):
            raise ForgeError("NATIVE_ABI_UNKNOWN", f"{context} are not a list")
        parameters: list[AbiParameterContract] = []
        for index, item in enumerate(value):
            parameter = cls._abi_object_mapping(item, context=f"{context}[{index}]")
            parameter_contract: AbiParameterContract = {}
            shape_count = 0
            for kind in ("scalar", "finite", "record", "sequence", "view", "vector", "mask"):
                raw_shape = parameter.get(kind)
                if raw_shape is None:
                    continue
                shape = cls._abi_object_mapping(raw_shape, context=f"{context}[{index}].{kind}")
                shape_count += 1
                if kind == "scalar":
                    parameter_contract["scalar"] = shape
                elif kind == "finite":
                    parameter_contract["finite"] = shape
                elif kind == "record":
                    parameter_contract["record"] = shape
                elif kind == "sequence":
                    parameter_contract["sequence"] = shape
                elif kind == "view":
                    parameter_contract["view"] = shape
                elif kind == "vector":
                    parameter_contract["vector"] = shape
                else:
                    parameter_contract["mask"] = shape
            if shape_count != 1:
                raise ForgeError(
                    "NATIVE_ABI_UNKNOWN", f"{context}[{index}] has an invalid value contract"
                )
            parameters.append(parameter_contract)
        return parameters

    @classmethod
    def _abi_record_type(cls, abi: NativeAbi, name: str, *, context: str) -> str:
        shape = cls._abi_shape(abi, "record", name, context=context)
        identity = shape.get("type_identity")
        if not isinstance(identity, str) or not identity:
            raise ForgeError("NATIVE_ABI_UNKNOWN", f"{context} has no record identity")
        return identity

    @classmethod
    def _abi_finite_value(
        cls, abi: NativeAbi, name: str, variant: str, *, context: str
    ) -> dict[str, object]:
        shape = cls._abi_shape(abi, "finite", name, context=context)
        type_identity = shape.get("type_identity")
        variants = shape.get("variants")
        if not isinstance(type_identity, str) or not isinstance(variants, Mapping):
            raise ForgeError("NATIVE_ABI_UNKNOWN", f"{context} has malformed finite metadata")
        for discriminant_text, variant_identity in variants.items():
            if not isinstance(discriminant_text, str) or not isinstance(variant_identity, str):
                continue
            if variant_identity.endswith(f"::{variant}"):
                try:
                    discriminant = int(discriminant_text)
                except ValueError as exc:
                    raise ForgeError(
                        "NATIVE_ABI_UNKNOWN", f"{context} has an invalid discriminant"
                    ) from exc
                return {
                    "finite": {
                        "type_identity": type_identity,
                        "variant_identity": variant_identity,
                        "discriminant": discriminant,
                    }
                }
        raise ForgeError("NATIVE_ABI_UNKNOWN", f"{context} variant {variant!r} is absent")

    @classmethod
    def _abi_finite_variant(cls, value: object, abi: NativeAbi, name: str, *, context: str) -> str:
        shape = cls._abi_shape(abi, "finite", name, context=context)
        type_identity = shape.get("type_identity")
        variants = shape.get("variants")
        if (
            not isinstance(type_identity, str)
            or not isinstance(variants, Mapping)
            or not isinstance(value, Mapping)
            or not isinstance(value.get("finite"), Mapping)
        ):
            raise ForgeError("NATIVE_ABI_UNKNOWN", f"{context} is malformed")
        finite = value["finite"]
        if finite.get("type_identity") != type_identity:
            raise ForgeError("NATIVE_ABI_UNKNOWN", f"{context} has an invalid type")
        variant_identity = finite.get("variant_identity")
        discriminant = finite.get("discriminant")
        if (
            not isinstance(variant_identity, str)
            or not isinstance(discriminant, int)
            or isinstance(discriminant, bool)
        ):
            raise ForgeError("NATIVE_ABI_UNKNOWN", f"{context} is malformed")
        expected = variants.get(str(discriminant))
        if expected != variant_identity:
            raise ForgeError("NATIVE_ABI_UNKNOWN", f"{context} has an invalid variant")
        return variant_identity.rsplit("::", 1)[-1]

    def language_owned_abi(self) -> NativeAbi:
        """Load and validate ABI metadata emitted by the language compiler."""

        self.ensure_available()
        cache_key = ("language-owned-abi", self.semantic_input_identity())
        cached = _ABI_CACHE.get(cache_key)
        if cached is not None:
            return cached
        invocation = self.invoke(["abi", str(self.native_source.resolve())])
        if not invocation.ok or invocation.payload is None:
            raise ForgeError(
                "NATIVE_ABI_UNKNOWN",
                "language-owned ABI metadata did not return valid JSON",
            )
        payload = invocation.payload
        if payload.get("schema_version") != "0.1" or payload.get("module") != _CORE_MODULE:
            raise ForgeError(
                "NATIVE_ABI_UNKNOWN", "language-owned ABI metadata has an invalid header"
            )
        source_identity = payload.get("source_artifact_identity")
        functions = payload.get("functions")
        composites = payload.get("composites")
        if (
            not isinstance(source_identity, str)
            or not isinstance(functions, Mapping)
            or not isinstance(composites, Mapping)
        ):
            raise ForgeError("NATIVE_ABI_UNKNOWN", "language-owned ABI metadata is incomplete")
        raw_functions = self._abi_object_mapping(functions, context="ABI functions")
        typed_functions: dict[str, AbiFunctionContract] = {}
        for function_name, raw_function in raw_functions.items():
            function_object = self._abi_object_mapping(
                raw_function, context=f"ABI function {function_name}"
            )
            function_identity = function_object.get("function_identity")
            declaring_module = function_object.get("declaring_module")
            exported_name = function_object.get("name")
            if (
                not isinstance(function_identity, str)
                or not function_identity
                or not isinstance(declaring_module, str)
                or not declaring_module
                or not isinstance(exported_name, str)
                or not exported_name
            ):
                raise ForgeError(
                    "NATIVE_ABI_UNKNOWN",
                    f"ABI function {function_name} is missing declaration identity",
                )
            typed_functions[function_name] = {
                "function_identity": function_identity,
                "declaring_module": declaring_module,
                "name": exported_name,
                "inputs": self._abi_parameters(
                    function_object.get("inputs"), context=f"ABI function {function_name} inputs"
                ),
                "outputs": self._abi_parameters(
                    function_object.get("outputs"), context=f"ABI function {function_name} outputs"
                ),
            }
        typed_composites = {
            composite_name: self._abi_object_mapping(
                composite, context=f"ABI composite {composite_name}"
            )
            for composite_name, composite in self._abi_object_mapping(
                composites, context="ABI composites"
            ).items()
        }
        if "evidence_readiness" not in typed_functions:
            raise ForgeError("NATIVE_ABI_UNKNOWN", "readiness function is absent from language ABI")
        result = NativeAbi(
            source_artifact_identity=source_identity,
            module=str(payload["module"]),
            functions=typed_functions,
            composites=typed_composites,
        )
        readiness_contract = result.functions["evidence_readiness"]
        inputs = readiness_contract["inputs"]
        outputs = readiness_contract["outputs"]
        if len(inputs) != 1 or len(outputs) != 1:
            raise ForgeError("NATIVE_ABI_UNKNOWN", "readiness ABI has an invalid arity")
        for value, context in ((inputs[0], "readiness input"), (outputs[0], "readiness result")):
            record = value.get("record")
            if not isinstance(record, Mapping):
                raise ForgeError("NATIVE_ABI_UNKNOWN", f"{context} is not a record contract")
            if not isinstance(record.get("name"), str) or not record["name"]:
                raise ForgeError("NATIVE_ABI_UNKNOWN", f"{context} has no record name")
            if not isinstance(record.get("type_identity"), str) or not record["type_identity"]:
                raise ForgeError("NATIVE_ABI_UNKNOWN", f"{context} has no record identity")
        _ABI_CACHE[cache_key] = result
        return result

    @staticmethod
    def _finite_argument(type_name: str, variant: str, discriminant: int) -> dict[str, object]:
        type_identity = (
            type_name
            if type_name.startswith(_MNCS_TYPE_PREFIX)
            else f"{_MNCS_TYPE_PREFIX}{type_name}"
        )
        variant_type_name = type_identity[len(_MNCS_TYPE_PREFIX) :]
        return {
            "finite": {
                "type_identity": type_identity,
                "variant_identity": f"{_MNCS_VARIANT_PREFIX}{variant_type_name}::{variant}",
                "discriminant": discriminant,
            }
        }

    @staticmethod
    def _finite_value(
        type_identity: str, module: str, type_name: str, variant: str
    ) -> dict[str, object]:
        try:
            discriminant = _FINITE_VARIANTS[type_identity][variant]
        except KeyError as exc:
            raise ForgeError(
                "NATIVE_CONFIG_INVALID", f"unknown native finite variant: {variant}"
            ) from exc
        return {
            "finite": {
                "type_identity": type_identity,
                "variant_identity": _finite_variant_identity(module, type_name, variant),
                "discriminant": discriminant,
            }
        }

    @staticmethod
    def _record_value(
        type_identity: str, name: str, fields: Mapping[str, object]
    ) -> dict[str, object]:
        return {
            "record": {
                "type_identity": type_identity,
                "name": name,
                "fields": [[field_name, fields[field_name]] for field_name in sorted(fields)],
            }
        }

    @staticmethod
    def _sequence_value(values: Sequence[object]) -> dict[str, object]:
        return {"sequence": {"values": list(values)}}

    @staticmethod
    def _digest_value(
        value: object, *, context: str, type_identity: str = _DIGEST_TYPE
    ) -> dict[str, object]:
        if value is None or value == "":
            raw = bytes(32)
        elif isinstance(value, bytes):
            raw = value
        elif isinstance(value, str):
            encoded = value.rsplit(":", 1)[-1]
            try:
                raw = bytes.fromhex(encoded)
            except ValueError as exc:
                raise ForgeError(
                    "NATIVE_LIFECYCLE_UNKNOWN", f"{context} is not a digest identity"
                ) from exc
        else:
            raise ForgeError("NATIVE_LIFECYCLE_UNKNOWN", f"{context} is not a digest identity")
        if len(raw) != 32:
            raise ForgeError("NATIVE_LIFECYCLE_UNKNOWN", f"{context} is not a 32-byte digest")
        return NativeForgeAdapter._record_value(
            type_identity,
            "Digest32",
            {
                "bytes": NativeForgeAdapter._sequence_value(
                    [{"byte": {"value": item}} for item in raw]
                )
            },
        )

    @staticmethod
    def _history_event_value(event: Mapping[str, object]) -> dict[str, object]:
        kind = str(event.get("kind", "Empty"))
        if kind not in _FINITE_VARIANTS[_EVENT_KIND_TYPE]:
            raise ForgeError("NATIVE_LIFECYCLE_UNKNOWN", f"unknown native history event: {kind}")
        status = str(event.get("status", "UNKNOWN"))
        if status not in _STATUS_VARIANTS:
            raise ForgeError("NATIVE_LIFECYCLE_UNKNOWN", f"unknown native event status: {status}")
        return NativeForgeAdapter._record_value(
            _HISTORY_EVENT_TYPE,
            "HistoryEvent",
            {
                "candidate": NativeForgeAdapter._digest_value(
                    event.get("candidate"), context="history candidate"
                ),
                "epoch": NativeForgeAdapter._digest_value(
                    event.get("epoch"), context="history epoch"
                ),
                "kind": NativeForgeAdapter._finite_value(
                    _EVENT_KIND_TYPE, _LIFECYCLE_MODULE, "EventKind", kind
                ),
                "parent_candidate": NativeForgeAdapter._digest_value(
                    event.get("parent_candidate"), context="history parent candidate"
                ),
                "parent_epoch": NativeForgeAdapter._digest_value(
                    event.get("parent_epoch"), context="history parent epoch"
                ),
                "status": NativeForgeAdapter._finite_value(
                    _STATUS_TYPE, "mncs.core.status.v1", "Status", status
                ),
            },
        )

    @staticmethod
    def reconciliation_category_identity(category: str) -> bytes:
        """Bind a host category label without making strings part of the ABI."""

        if not isinstance(category, str) or not category:
            raise ForgeError("NATIVE_RECONCILIATION_UNKNOWN", "evidence category is malformed")
        return hashlib.sha256(
            b"mncs-forge.reconciliation.category.v1\0" + category.encode("utf-8")
        ).digest()

    @classmethod
    def _reconciliation_category_value(
        cls, category: str, records: Sequence[Mapping[str, object]]
    ) -> dict[str, object]:
        if len(records) == 0 or len(records) > 8:
            raise ForgeError(
                "NATIVE_RECONCILIATION_UNKNOWN",
                "native evidence category must contain between 1 and 8 records",
            )
        statuses: list[dict[str, object]] = []
        unsupported_count = 0
        for record in records:
            if not isinstance(record, Mapping):
                raise ForgeError(
                    "NATIVE_RECONCILIATION_UNKNOWN",
                    f"native evidence record is malformed for category {category}",
                )
            status = record.get("status")
            if not isinstance(status, str) or status not in _STATUS_VARIANTS:
                raise ForgeError(
                    "NATIVE_RECONCILIATION_UNKNOWN",
                    f"native evidence status is invalid for category {category}",
                )
            statuses.append(
                cls._finite_value(_STATUS_TYPE, "mncs.core.status.v1", "Status", str(status))
            )
            unsupported = record.get("unsupported_constructs", [])
            if isinstance(unsupported, Sequence) and not isinstance(unsupported, (str, bytes)):
                unsupported_count += len(unsupported)
            elif unsupported is not None:
                raise ForgeError(
                    "NATIVE_RECONCILIATION_UNKNOWN",
                    f"unsupported construct list is malformed for category {category}",
                )
        if unsupported_count > 255:
            raise ForgeError(
                "NATIVE_RECONCILIATION_UNKNOWN",
                f"unsupported construct count exceeds the byte bound for category {category}",
            )
        statuses.extend(
            cls._finite_value(_STATUS_TYPE, "mncs.core.status.v1", "Status", "UNKNOWN")
            for _ in range(8 - len(statuses))
        )
        return cls._record_value(
            _CATEGORY_INPUT_TYPE,
            "CategoryInput",
            {
                "category": cls._digest_value(
                    cls.reconciliation_category_identity(category),
                    context="reconciliation category",
                ),
                "count": {"byte": {"value": len(records)}},
                "statuses": cls._sequence_value(statuses),
                "unsupported_count": {"byte": {"value": unsupported_count}},
            },
        )

    @staticmethod
    def readiness_requirement_identity(requirement: str) -> bytes:
        """Bind a host requirement label without putting the label in the ABI."""

        if not isinstance(requirement, str) or not requirement:
            raise ForgeError("NATIVE_READINESS_UNKNOWN", "evidence requirement is malformed")
        return hashlib.sha256(
            b"mncs-forge.readiness.requirement.v1\0" + requirement.encode("utf-8")
        ).digest()

    @classmethod
    def _readiness_requirement_value(
        cls,
        requirement: str,
        normalized: NormalizedReadiness,
        *,
        abi: NativeAbi,
    ) -> dict[str, object]:
        records = normalized.get("records")
        if not isinstance(records, Sequence) or isinstance(records, (str, bytes)):
            raise ForgeError(
                "NATIVE_READINESS_UNKNOWN",
                f"native readiness records are malformed for requirement {requirement}",
            )
        if len(records) > 8:
            raise ForgeError(
                "NATIVE_READINESS_UNKNOWN",
                f"native readiness exceeds the eight-record bound for requirement {requirement}",
            )
        statuses: list[dict[str, object]] = []
        for record in records:
            if not isinstance(record, Mapping):
                raise ForgeError(
                    "NATIVE_READINESS_UNKNOWN",
                    f"native readiness record is malformed for requirement {requirement}",
                )
            status = record.get("status")
            if not isinstance(status, str) or status not in _STATUS_VARIANTS:
                raise ForgeError(
                    "NATIVE_READINESS_UNKNOWN",
                    f"native readiness status is invalid for requirement {requirement}",
                )
            statuses.append(
                cls._abi_finite_value(abi, "Status", status, context="readiness status")
            )
        statuses.extend(
            cls._abi_finite_value(abi, "Status", "UNKNOWN", context="readiness status")
            for _ in range(8 - len(statuses))
        )
        freshness = normalized.get("freshness")
        freshness_variants = _FINITE_VARIANTS[_FRESHNESS_TYPE]
        if not isinstance(freshness, str):
            raise ForgeError(
                "NATIVE_READINESS_UNKNOWN",
                f"native readiness freshness is invalid for requirement {requirement}",
            )
        canonical_freshness = next(
            (variant for variant in freshness_variants if variant.upper() == freshness.upper()),
            None,
        )
        if canonical_freshness is None:
            raise ForgeError(
                "NATIVE_READINESS_UNKNOWN",
                f"native readiness freshness is invalid for requirement {requirement}",
            )
        freshness = canonical_freshness
        flags: dict[str, bool] = {}
        for name in ("comparable", "environment_match", "policy_match", "authority_match"):
            value = normalized.get(name)
            if not isinstance(value, bool):
                raise ForgeError(
                    "NATIVE_READINESS_UNKNOWN",
                    f"native readiness {name} flag is invalid for requirement {requirement}",
                )
            flags[name] = value
        return cls._record_value(
            cls._abi_record_type(abi, "RequirementInput", context="RequirementInput"),
            "RequirementInput",
            {
                "authority_match": {"boolean": {"value": flags["authority_match"]}},
                "comparable": {"boolean": {"value": flags["comparable"]}},
                "count": {"byte": {"value": len(records)}},
                "environment_match": {"boolean": {"value": flags["environment_match"]}},
                "freshness": cls._abi_finite_value(
                    abi, "Freshness", freshness, context="readiness freshness"
                ),
                "identity": cls._digest_value(
                    cls.readiness_requirement_identity(requirement),
                    context="readiness requirement identity",
                    type_identity=cls._abi_record_type(abi, "Digest32", context="Digest32"),
                ),
                "policy_match": {"boolean": {"value": flags["policy_match"]}},
                "statuses": cls._sequence_value(statuses),
            },
        )

    @staticmethod
    def _record_fields(payload: dict[str, Any], *, context: str) -> dict[str, Any]:
        if payload.get("status") != "returned":
            raise ForgeError("NATIVE_LIFECYCLE_UNKNOWN", f"{context} did not return a value")
        returned = payload.get("returned")
        if not isinstance(returned, list) or len(returned) != 1:
            raise ForgeError(
                "NATIVE_LIFECYCLE_UNKNOWN",
                f"{context} returned an unexpected value count",
            )
        value = returned[0]
        if not isinstance(value, dict) or not isinstance(value.get("record"), dict):
            raise ForgeError("NATIVE_LIFECYCLE_UNKNOWN", f"{context} did not return a record")
        fields = value["record"].get("fields")
        if not isinstance(fields, list):
            raise ForgeError("NATIVE_LIFECYCLE_UNKNOWN", f"{context} record fields are malformed")
        result: dict[str, Any] = {}
        for field in fields:
            if (
                not isinstance(field, list)
                or len(field) != 2
                or not isinstance(field[0], str)
                or field[0] in result
            ):
                raise ForgeError("NATIVE_LIFECYCLE_UNKNOWN", f"{context} has malformed fields")
            result[field[0]] = field[1]
        return result

    @staticmethod
    def _finite_variant(value: object, type_name: str, *, context: str) -> str:
        if not isinstance(value, dict) or not isinstance(value.get("finite"), dict):
            raise ForgeError("NATIVE_LIFECYCLE_UNKNOWN", f"{context} is not a finite value")
        finite = value["finite"]
        type_identity = (
            type_name
            if type_name.startswith(_MNCS_TYPE_PREFIX)
            else f"{_MNCS_TYPE_PREFIX}{type_name}"
        )
        if finite.get("type_identity") != type_identity:
            raise ForgeError("NATIVE_LIFECYCLE_UNKNOWN", f"{context} has an invalid type")
        variant_type_name = type_identity[len(_MNCS_TYPE_PREFIX) :]
        expected_prefix = f"{_MNCS_VARIANT_PREFIX}{variant_type_name}::"
        variant_identity = finite.get("variant_identity")
        if not isinstance(variant_identity, str) or not variant_identity.startswith(
            expected_prefix
        ):
            raise ForgeError("NATIVE_LIFECYCLE_UNKNOWN", f"{context} has an invalid type")
        variant = variant_identity[len(expected_prefix) :]
        discriminant = finite.get("discriminant")
        if not isinstance(discriminant, int) or isinstance(discriminant, bool):
            raise ForgeError("NATIVE_LIFECYCLE_UNKNOWN", f"{context} has no discriminant")
        expected_values = _FINITE_VARIANTS[type_identity]
        expected = expected_values.get(variant)
        if expected is None or discriminant != expected:
            raise ForgeError("NATIVE_LIFECYCLE_UNKNOWN", f"{context} has an invalid variant")
        return variant

    @staticmethod
    def _record_value_fields(value: object, expected_type: str, *, context: str) -> dict[str, Any]:
        if not isinstance(value, dict) or not isinstance(value.get("record"), dict):
            raise ForgeError("NATIVE_LIFECYCLE_UNKNOWN", f"{context} is not a record")
        record = value["record"]
        if record.get("type_identity") != expected_type:
            raise ForgeError("NATIVE_LIFECYCLE_UNKNOWN", f"{context} has an invalid type")
        fields = record.get("fields")
        if not isinstance(fields, list):
            raise ForgeError("NATIVE_LIFECYCLE_UNKNOWN", f"{context} has malformed fields")
        result: dict[str, Any] = {}
        for field in fields:
            if (
                not isinstance(field, list)
                or len(field) != 2
                or not isinstance(field[0], str)
                or field[0] in result
            ):
                raise ForgeError("NATIVE_LIFECYCLE_UNKNOWN", f"{context} has malformed fields")
            result[field[0]] = field[1]
        return result

    @staticmethod
    def _boolean(value: object, *, context: str) -> bool:
        if not isinstance(value, dict) or not isinstance(value.get("boolean"), dict):
            raise ForgeError("NATIVE_LIFECYCLE_UNKNOWN", f"{context} is not a boolean")
        result = value["boolean"].get("value")
        if not isinstance(result, bool):
            raise ForgeError("NATIVE_LIFECYCLE_UNKNOWN", f"{context} has an invalid value")
        return result

    @staticmethod
    def _integer(value: object, *, context: str) -> int:
        if not isinstance(value, dict) or not isinstance(value.get("integer"), dict):
            raise ForgeError("NATIVE_LIFECYCLE_UNKNOWN", f"{context} is not an integer")
        result = value["integer"].get("value")
        if not isinstance(result, int) or isinstance(result, bool):
            raise ForgeError("NATIVE_LIFECYCLE_UNKNOWN", f"{context} has an invalid value")
        return result

    @classmethod
    def _digest_bytes(
        cls, value: object, *, context: str, type_identity: str = _DIGEST_TYPE
    ) -> bytes:
        fields = cls._record_value_fields(value, type_identity, context=context)
        sequence = fields.get("bytes")
        if not isinstance(sequence, dict) or not isinstance(sequence.get("sequence"), dict):
            raise ForgeError("NATIVE_LIFECYCLE_UNKNOWN", f"{context} bytes are malformed")
        values = sequence["sequence"].get("values")
        if not isinstance(values, list) or len(values) != 32:
            raise ForgeError("NATIVE_LIFECYCLE_UNKNOWN", f"{context} bytes are malformed")
        output = bytearray()
        for item in values:
            output.append(cls._byte(item, context=context))
        return bytes(output)

    @staticmethod
    def _byte(value: object, *, context: str) -> int:
        if not isinstance(value, dict) or not isinstance(value.get("byte"), dict):
            raise ForgeError("NATIVE_LIFECYCLE_UNKNOWN", f"{context} is not a byte")
        result = value["byte"].get("value")
        if not isinstance(result, int) or isinstance(result, bool) or not 0 <= result <= 255:
            raise ForgeError("NATIVE_LIFECYCLE_UNKNOWN", f"{context} has an invalid value")
        return result

    def lifecycle_preflight(
        self, stage: str, operation: str, evidence: str = "UNKNOWN"
    ) -> NativeLifecycleResult:
        """Run the typed MNCS lifecycle kernel for a covered Forge transition.

        The request is created in a bounded temporary directory and carries
        finite values only. Forge identities are deliberately excluded from
        this preflight because the MNCS kernel is checking transition
        semantics, while identity production remains a host boundary.
        """

        if stage not in _LIFECYCLE_STAGES:
            raise ForgeError("NATIVE_CONFIG_INVALID", f"unknown native lifecycle stage: {stage}")
        if operation not in _LIFECYCLE_OPERATIONS:
            raise ForgeError(
                "NATIVE_CONFIG_INVALID", f"unknown native lifecycle operation: {operation}"
            )
        if evidence not in _STATUS_VARIANTS:
            raise ForgeError("NATIVE_CONFIG_INVALID", f"unknown native status: {evidence}")
        if not self.available:
            raise ForgeError("NATIVE_UNAVAILABLE", "mncs-language checkout is unavailable")
        if not self.forge_modules_available:
            raise ForgeError(
                "NATIVE_UNAVAILABLE", "packaged Forge MNCS lifecycle source is unavailable"
            )
        command = tuple(self._command())
        semantic_identity = self.semantic_input_identity()
        cache_key = (
            NATIVE_EXECUTION_CONTRACT,
            semantic_identity,
            *command,
            stage,
            operation,
            evidence,
        )
        cached = _LIFECYCLE_CACHE.get(cache_key)
        if cached is not None:
            return cached
        request = {
            "schema_version": NATIVE_SCHEMA_VERSION,
            "target": {
                "module": "mncs.forge.core.v1",
                "function": "lifecycle_preflight",
            },
            "arguments": [
                self._finite_argument(_STAGE_TYPE, stage, _LIFECYCLE_STAGES[stage]),
                self._finite_argument(_OPERATION_TYPE, operation, _LIFECYCLE_OPERATIONS[operation]),
                self._finite_argument(_STATUS_TYPE, evidence, _STATUS_VARIANTS[evidence]),
            ],
            "step_budget": 4096,
        }
        with tempfile.TemporaryDirectory(prefix=".mncs-native-", dir=self.forge_root) as directory:
            request_path = Path(directory) / "lifecycle-request.json"
            request_path.write_text(json.dumps(request), encoding="utf-8")
            invocation = self.execute(self.native_source, request_path)
        if not invocation.ok or invocation.payload is None:
            raise ForgeError(
                "NATIVE_LIFECYCLE_UNKNOWN",
                "language-owned lifecycle preflight did not return valid JSON "
                f"(returncode {invocation.returncode})",
            )
        fields = self._record_fields(invocation.payload, context="lifecycle preflight")
        state = fields.get("state")
        if not isinstance(state, dict) or not isinstance(state.get("record"), dict):
            raise ForgeError("NATIVE_LIFECYCLE_UNKNOWN", "lifecycle result state is malformed")
        state_fields = state["record"].get("fields")
        if not isinstance(state_fields, list):
            raise ForgeError("NATIVE_LIFECYCLE_UNKNOWN", "lifecycle result state has no fields")
        state_map: dict[str, Any] = {}
        for field in state_fields:
            if (
                not isinstance(field, list)
                or len(field) != 2
                or not isinstance(field[0], str)
                or field[0] in state_map
            ):
                raise ForgeError("NATIVE_LIFECYCLE_UNKNOWN", "lifecycle result state is malformed")
            state_map[field[0]] = field[1]
        next_stage = self._finite_variant(
            state_map.get("stage"), _STAGE_TYPE, context="lifecycle next stage"
        )
        status = self._finite_variant(
            fields.get("status"), _STATUS_TYPE, context="lifecycle result status"
        )
        reason = self._byte(fields.get("reason"), context="lifecycle result reason")
        result = NativeLifecycleResult(
            stage=stage,
            operation=operation,
            next_stage=next_stage,
            status=status,
            reason=reason,
        )
        _LIFECYCLE_CACHE[cache_key] = result
        return result

    def lifecycle_projection(
        self,
        events: Sequence[Mapping[str, object]],
        *,
        current_candidate: str | None,
        required_evidence: int,
    ) -> NativeLifecycleProjection:
        """Project bounded typed lifecycle history in the MNCS runtime.

        The host supplies only normalized record observations and digest
        identities.  The native module owns parentage, disposition, freshness,
        status, and stage projection; persistence and digest production remain
        outside the language boundary.
        """

        if len(events) > 32:
            raise ForgeError(
                "NATIVE_LIFECYCLE_UNKNOWN", "native history exceeds the 32-event bound"
            )
        if not isinstance(required_evidence, int) or isinstance(required_evidence, bool):
            raise ForgeError("NATIVE_LIFECYCLE_UNKNOWN", "native evidence bound is not an integer")
        if not 0 <= required_evidence <= 255:
            raise ForgeError(
                "NATIVE_LIFECYCLE_UNKNOWN", "native evidence bound is outside byte range"
            )
        self.ensure_available()
        event_list = [dict(event) for event in events]
        cache_key = (
            NATIVE_LIFECYCLE_PROJECTION_CONTRACT,
            self.semantic_input_identity(),
            json.dumps(event_list, sort_keys=True),
            current_candidate or "",
            required_evidence,
        )
        cached = _LIFECYCLE_PROJECTION_CACHE.get(cache_key)
        if cached is not None:
            return cached
        event_values = [self._history_event_value(event) for event in event_list]
        event_values.extend(self._history_event_value({}) for _ in range(32 - len(event_values)))
        request_value = self._record_value(
            _PROJECTION_INPUT_TYPE,
            "ProjectionInput",
            {
                "current_candidate": self._digest_value(
                    current_candidate, context="current candidate"
                ),
                "event_count": {"byte": {"value": len(event_list)}},
                "events": self._sequence_value(event_values),
                "required_evidence": {"byte": {"value": required_evidence}},
            },
        )
        request = {
            "schema_version": NATIVE_SCHEMA_VERSION,
            "target": {"module": "mncs.forge.core.v1", "function": "lifecycle_project"},
            "arguments": [request_value],
            "step_budget": 200_000,
        }
        with tempfile.TemporaryDirectory(prefix=".mncs-native-", dir=self.forge_root) as directory:
            request_path = Path(directory) / "lifecycle-projection-request.json"
            request_path.write_text(json.dumps(request), encoding="utf-8")
            invocation = self.execute(self.native_source, request_path)
        if not invocation.ok or invocation.payload is None:
            raise ForgeError(
                "NATIVE_LIFECYCLE_UNKNOWN",
                "language-owned lifecycle projection did not return valid JSON "
                f"(returncode {invocation.returncode})",
            )
        result_fields = self._record_fields(invocation.payload, context="lifecycle projection")
        projection_fields = self._record_value_fields(
            result_fields.get("projection"),
            _PROJECTION_STATE_TYPE,
            context="lifecycle projection state",
        )
        stage = self._finite_variant(
            projection_fields.get("stage"), _STAGE_TYPE, context="lifecycle projection stage"
        )
        evidence = self._finite_variant(
            projection_fields.get("evidence"), _STATUS_TYPE, context="lifecycle projection evidence"
        )
        disposition = self._finite_variant(
            projection_fields.get("disposition"),
            _DISPOSITION_TYPE,
            context="lifecycle projection disposition",
        )
        freshness = self._finite_variant(
            projection_fields.get("freshness"),
            _FRESHNESS_TYPE,
            context="lifecycle projection freshness",
        )
        status = self._finite_variant(
            result_fields.get("status"), _STATUS_TYPE, context="lifecycle projection status"
        )
        result = NativeLifecycleProjection(
            stage=stage,
            active_epoch=self._digest_bytes(
                projection_fields.get("active_epoch"), context="active epoch"
            ),
            parent_epoch=self._digest_bytes(
                projection_fields.get("parent_epoch"), context="parent epoch"
            ),
            current_candidate=self._digest_bytes(
                projection_fields.get("current_candidate"), context="current candidate"
            ),
            parent_candidate=self._digest_bytes(
                projection_fields.get("parent_candidate"), context="parent candidate"
            ),
            evidence=evidence,
            disposition=disposition,
            freshness=freshness,
            lineage_ok=self._boolean(
                projection_fields.get("lineage_ok"), context="lifecycle lineage flag"
            ),
            epoch_count=self._integer(
                projection_fields.get("epoch_count"), context="lifecycle epoch count"
            ),
            candidate_count=self._integer(
                projection_fields.get("candidate_count"), context="lifecycle candidate count"
            ),
            evidence_count=self._integer(
                projection_fields.get("evidence_count"), context="lifecycle evidence count"
            ),
            frozen=self._boolean(projection_fields.get("frozen"), context="lifecycle frozen flag"),
            evaluated=self._boolean(
                projection_fields.get("evaluated"), context="lifecycle evaluated flag"
            ),
            status=status,
            reason=self._byte(result_fields.get("reason"), context="lifecycle projection reason"),
        )
        _LIFECYCLE_PROJECTION_CACHE[cache_key] = result
        return result

    def reconciliation_projection(
        self, categories: Mapping[str, Sequence[Mapping[str, object]]]
    ) -> NativeReconciliationProjection:
        """Project bounded technical evidence categories through MNCS.

        Category labels, record identities, and disclosure-shaped values remain
        host concerns. The native kernel owns the bounded status fold, per
        category conflict classification, and aggregate technical status.
        """

        if not isinstance(categories, Mapping):
            raise ForgeError(
                "NATIVE_RECONCILIATION_UNKNOWN",
                "native reconciliation categories are malformed",
            )
        for category, records in categories.items():
            if not isinstance(category, str) or not category:
                raise ForgeError(
                    "NATIVE_RECONCILIATION_UNKNOWN",
                    "native reconciliation category is malformed",
                )
            if not isinstance(records, Sequence) or isinstance(records, (str, bytes)):
                raise ForgeError(
                    "NATIVE_RECONCILIATION_UNKNOWN",
                    f"native evidence records are malformed for category {category}",
                )
        ordered = sorted(categories.items())
        if len(ordered) > 16:
            raise ForgeError(
                "NATIVE_RECONCILIATION_UNKNOWN",
                "native reconciliation exceeds the 16-category bound",
            )
        self.ensure_available()
        category_values = [
            self._reconciliation_category_value(category, records) for category, records in ordered
        ]
        empty = self._reconciliation_category_value("__unused__", [{"status": "UNKNOWN"}])
        category_values.extend(empty for _ in range(16 - len(category_values)))
        try:
            serialized = json.dumps(
                {
                    "categories": [
                        {
                            "category": category,
                            "records": [dict(record) for record in records],
                        }
                        for category, records in ordered
                    ]
                },
                sort_keys=True,
            )
        except (TypeError, ValueError) as exc:
            raise ForgeError(
                "NATIVE_RECONCILIATION_UNKNOWN",
                "native reconciliation input cannot be serialized",
            ) from exc
        cache_key = (
            NATIVE_RECONCILIATION_CONTRACT,
            self.semantic_input_identity(),
            serialized,
        )
        cached = _RECONCILIATION_CACHE.get(cache_key)
        if cached is not None:
            return cached
        request_value = self._record_value(
            _RECONCILIATION_INPUT_TYPE,
            "ReconciliationInput",
            {
                "categories": self._sequence_value(category_values),
                "category_count": {"byte": {"value": len(ordered)}},
            },
        )
        request = {
            "schema_version": NATIVE_SCHEMA_VERSION,
            "target": {
                "module": "mncs.forge.core.v1",
                "function": "evidence_reconcile",
            },
            "arguments": [request_value],
            "step_budget": 500_000,
        }
        with tempfile.TemporaryDirectory(prefix=".mncs-native-", dir=self.forge_root) as directory:
            request_path = Path(directory) / "reconciliation-request.json"
            request_path.write_text(json.dumps(request), encoding="utf-8")
            invocation = self.execute(self.native_source, request_path)
        if not invocation.ok or invocation.payload is None:
            raise ForgeError(
                "NATIVE_RECONCILIATION_UNKNOWN",
                "language-owned reconciliation did not return valid JSON "
                f"(returncode {invocation.returncode})",
            )
        result_fields = self._record_fields(invocation.payload, context="reconciliation")
        state_fields = self._record_value_fields(
            result_fields.get("state"),
            _RECONCILIATION_STATE_TYPE,
            context="reconciliation state",
        )
        category_sequence = state_fields.get("categories")
        if not isinstance(category_sequence, dict) or not isinstance(
            category_sequence.get("sequence"), dict
        ):
            raise ForgeError(
                "NATIVE_RECONCILIATION_UNKNOWN", "reconciliation categories are malformed"
            )
        category_items = category_sequence["sequence"].get("values")
        if not isinstance(category_items, list) or len(category_items) != 16:
            raise ForgeError(
                "NATIVE_RECONCILIATION_UNKNOWN", "reconciliation categories are malformed"
            )
        category_count = self._integer(
            state_fields.get("category_count"), context="reconciliation category count"
        )
        if category_count != len(ordered) or not 0 <= category_count <= 16:
            raise ForgeError(
                "NATIVE_RECONCILIATION_UNKNOWN",
                "reconciliation category count disagrees with the request",
            )
        result_status = self._finite_variant(
            result_fields.get("status"),
            _STATUS_TYPE,
            context="reconciliation result status",
        )
        state_status = self._finite_variant(
            state_fields.get("status"),
            _STATUS_TYPE,
            context="reconciliation state status",
        )
        valid = self._boolean(state_fields.get("valid"), context="reconciliation validity")
        reason = self._byte(result_fields.get("reason"), context="reconciliation reason")
        if not valid or reason != 0 or state_status != result_status:
            raise ForgeError(
                "NATIVE_RECONCILIATION_UNKNOWN",
                "language-owned reconciliation reported an invalid bounded projection",
            )
        projected: list[NativeReconciliationCategory] = []
        for index in range(category_count):
            fields = self._record_value_fields(
                category_items[index],
                _CATEGORY_PROJECTION_TYPE,
                context="reconciliation category projection",
            )
            category_digest = self._digest_bytes(
                fields.get("category"), context="reconciliation category identity"
            )
            expected_digest = self.reconciliation_category_identity(ordered[index][0])
            if category_digest != expected_digest:
                raise ForgeError(
                    "NATIVE_RECONCILIATION_MISMATCH",
                    "native reconciliation category identity disagrees with the request",
                )
            projected.append(
                NativeReconciliationCategory(
                    category=category_digest,
                    status=self._finite_variant(
                        fields.get("status"),
                        _STATUS_TYPE,
                        context="reconciliation category status",
                    ),
                    pass_count=self._integer(
                        fields.get("pass_count"), context="reconciliation pass count"
                    ),
                    fail_count=self._integer(
                        fields.get("fail_count"), context="reconciliation fail count"
                    ),
                    unknown_count=self._integer(
                        fields.get("unknown_count"), context="reconciliation unknown count"
                    ),
                    observed_count=self._integer(
                        fields.get("observed_count"), context="reconciliation observed count"
                    ),
                    conflict=self._boolean(
                        fields.get("conflict"), context="reconciliation conflict flag"
                    ),
                    unsupported_count=self._integer(
                        fields.get("unsupported_count"),
                        context="reconciliation unsupported count",
                    ),
                )
            )
        result = NativeReconciliationProjection(
            categories=tuple(projected),
            status=result_status,
            category_count=category_count,
            conflicting_category_count=self._integer(
                state_fields.get("conflicting_category_count"),
                context="reconciliation conflict count",
            ),
            unsupported_count=self._integer(
                state_fields.get("unsupported_count"),
                context="reconciliation unsupported count",
            ),
            observed_count=self._integer(
                state_fields.get("observed_count"), context="reconciliation observed count"
            ),
            valid=valid,
            reason=reason,
        )
        if result.conflicting_category_count != sum(item.conflict for item in projected):
            raise ForgeError(
                "NATIVE_RECONCILIATION_MISMATCH",
                "native reconciliation conflict count is inconsistent",
            )
        if result.observed_count != sum(item.observed_count for item in projected):
            raise ForgeError(
                "NATIVE_RECONCILIATION_MISMATCH",
                "native reconciliation observed count is inconsistent",
            )
        if result.unsupported_count != sum(item.unsupported_count for item in projected):
            raise ForgeError(
                "NATIVE_RECONCILIATION_MISMATCH",
                "native reconciliation unsupported count is inconsistent",
            )
        _RECONCILIATION_CACHE[cache_key] = result
        return result

    def readiness_projection(
        self,
        requirements: Mapping[str, Mapping[str, object]],
        *,
        candidate_present: bool,
        policy_valid: bool,
    ) -> NativeReadinessProjection:
        """Project normalized evidence readiness through the MNCS kernel.

        The host supplies records and comparison observations. MNCS owns the
        bounded fold and the precedence of the readiness classification; it
        never sees requirement labels, provider payloads, or custody records.
        """

        if not isinstance(requirements, Mapping):
            raise ForgeError(
                "NATIVE_READINESS_UNKNOWN", "native readiness requirements are malformed"
            )
        if not isinstance(candidate_present, bool) or not isinstance(policy_valid, bool):
            raise ForgeError("NATIVE_READINESS_UNKNOWN", "native readiness flags are malformed")
        raw_ordered = sorted(requirements.items())
        if len(raw_ordered) > 16:
            raise ForgeError(
                "NATIVE_READINESS_UNKNOWN", "native readiness exceeds the 16-requirement bound"
            )
        normalized_inputs: list[tuple[str, NormalizedReadiness]] = []
        for requirement, raw_normalized in raw_ordered:
            if not isinstance(requirement, str) or not requirement:
                raise ForgeError(
                    "NATIVE_READINESS_UNKNOWN", "native readiness requirement is malformed"
                )
            if not isinstance(raw_normalized, Mapping):
                raise ForgeError(
                    "NATIVE_READINESS_UNKNOWN", "native readiness requirement is malformed"
                )
            raw_records = raw_normalized.get("records")
            if not isinstance(raw_records, Sequence) or isinstance(raw_records, (str, bytes)):
                raise ForgeError(
                    "NATIVE_READINESS_UNKNOWN",
                    f"native readiness records are malformed for requirement {requirement}",
                )
            records: list[Mapping[str, object]] = []
            for index, raw_record in enumerate(raw_records):
                if not isinstance(raw_record, Mapping):
                    raise ForgeError(
                        "NATIVE_READINESS_UNKNOWN",
                        f"native readiness record is malformed for requirement {requirement}",
                    )
                records.append(raw_record)
                if index >= 8:
                    raise ForgeError(
                        "NATIVE_READINESS_UNKNOWN",
                        "native readiness exceeds the eight-record bound for "
                        f"requirement {requirement}",
                    )
            freshness = raw_normalized.get("freshness")
            comparable = raw_normalized.get("comparable")
            environment_match = raw_normalized.get("environment_match")
            policy_match = raw_normalized.get("policy_match")
            authority_match = raw_normalized.get("authority_match")
            if (
                not isinstance(freshness, str)
                or not isinstance(comparable, bool)
                or not isinstance(environment_match, bool)
                or not isinstance(policy_match, bool)
                or not isinstance(authority_match, bool)
            ):
                raise ForgeError(
                    "NATIVE_READINESS_UNKNOWN",
                    "native readiness comparison envelope is malformed for "
                    f"requirement {requirement}",
                )
            normalized_value: NormalizedReadiness = {
                "records": records,
                "freshness": freshness,
                "comparable": comparable,
                "environment_match": environment_match,
                "policy_match": policy_match,
                "authority_match": authority_match,
            }
            normalized_inputs.append((requirement, normalized_value))
        ordered: Sequence[tuple[str, NormalizedReadiness]] = normalized_inputs
        self.ensure_available()
        abi = self.language_owned_abi()
        function_contract = abi.functions["evidence_readiness"]
        input_contract = function_contract["inputs"][0]
        output_contract = function_contract["outputs"][0]
        if (
            not isinstance(input_contract, Mapping)
            or not isinstance(input_contract.get("record"), Mapping)
            or not isinstance(output_contract, Mapping)
            or not isinstance(output_contract.get("record"), Mapping)
        ):
            raise ForgeError("NATIVE_ABI_UNKNOWN", "readiness ABI record contracts are malformed")
        readiness_input_type = input_contract["record"].get("type_identity")
        readiness_result_type = output_contract["record"].get("type_identity")
        if not isinstance(readiness_input_type, str) or not isinstance(readiness_result_type, str):
            raise ForgeError("NATIVE_ABI_UNKNOWN", "readiness ABI record identities are malformed")
        values = [
            self._readiness_requirement_value(requirement, normalized, abi=abi)
            for requirement, normalized in ordered
        ]
        empty = self._readiness_requirement_value(
            "__unused__",
            {
                "records": [],
                "freshness": "NotApplicable",
                "comparable": True,
                "environment_match": True,
                "policy_match": True,
                "authority_match": True,
            },
            abi=abi,
        )
        values.extend(empty for _ in range(16 - len(values)))
        cache_material = [
            {
                "requirement": requirement,
                "records": [record.get("status") for record in normalized["records"]],
                "freshness": normalized["freshness"],
                "comparable": normalized["comparable"],
                "environment_match": normalized["environment_match"],
                "policy_match": normalized["policy_match"],
                "authority_match": normalized["authority_match"],
            }
            for requirement, normalized in ordered
        ]
        cache_key = (
            NATIVE_READINESS_CONTRACT,
            self.semantic_input_identity(),
            json.dumps(
                {
                    "requirements": cache_material,
                    "candidate_present": candidate_present,
                    "policy_valid": policy_valid,
                },
                sort_keys=True,
            ),
        )
        cached = _READINESS_CACHE.get(cache_key)
        if cached is not None:
            return cached
        request_value = self._record_value(
            readiness_input_type,
            "ReadinessInput",
            {
                "candidate_present": {"boolean": {"value": candidate_present}},
                "policy_valid": {"boolean": {"value": policy_valid}},
                "requirement_count": {"byte": {"value": len(ordered)}},
                "requirements": self._sequence_value(values),
            },
        )
        request = {
            "schema_version": NATIVE_SCHEMA_VERSION,
            "target": {"module": "mncs.forge.core.v1", "function": "evidence_readiness"},
            "arguments": [request_value],
            "step_budget": 700_000,
        }
        with tempfile.TemporaryDirectory(prefix=".mncs-native-", dir=self.forge_root) as directory:
            request_path = Path(directory) / "readiness-request.json"
            request_path.write_text(json.dumps(request), encoding="utf-8")
            invocation = self.execute(self.native_source, request_path)
        if not invocation.ok or invocation.payload is None:
            raise ForgeError(
                "NATIVE_READINESS_UNKNOWN",
                "language-owned readiness did not return valid JSON "
                f"(returncode {invocation.returncode})",
            )
        result_fields = self._record_fields(invocation.payload, context="readiness")
        returned = invocation.payload.get("returned")
        if (
            not isinstance(returned, list)
            or len(returned) != 1
            or not isinstance(returned[0], Mapping)
            or not isinstance(returned[0].get("record"), Mapping)
            or returned[0]["record"].get("type_identity") != readiness_result_type
        ):
            raise ForgeError(
                "NATIVE_ABI_MISMATCH", "readiness result type disagrees with language ABI"
            )
        state_type = self._abi_record_type(abi, "ReadinessState", context="ReadinessState")
        projection_type = self._abi_record_type(
            abi, "RequirementProjection", context="RequirementProjection"
        )
        state_fields = self._record_value_fields(
            result_fields.get("state"), state_type, context="readiness state"
        )
        requirement_sequence = state_fields.get("requirements")
        if not isinstance(requirement_sequence, dict) or not isinstance(
            requirement_sequence.get("sequence"), dict
        ):
            raise ForgeError("NATIVE_READINESS_UNKNOWN", "readiness requirements are malformed")
        requirement_items = requirement_sequence["sequence"].get("values")
        if not isinstance(requirement_items, list) or len(requirement_items) != 16:
            raise ForgeError("NATIVE_READINESS_UNKNOWN", "readiness requirements are malformed")
        requirement_count = self._integer(
            state_fields.get("requirement_count"), context="readiness requirement count"
        )
        if requirement_count != len(ordered) or not 0 <= requirement_count <= 16:
            raise ForgeError(
                "NATIVE_READINESS_UNKNOWN",
                "readiness requirement count disagrees with the request",
            )
        result_status = self._abi_finite_variant(
            result_fields.get("status"), abi, "Status", context="readiness result status"
        )
        state_status = self._abi_finite_variant(
            state_fields.get("status"), abi, "Status", context="readiness state status"
        )
        reason = self._abi_finite_variant(
            result_fields.get("reason"), abi, "ReadinessReason", context="readiness reason"
        )
        valid = self._boolean(state_fields.get("valid"), context="readiness validity")
        ready = self._boolean(state_fields.get("ready"), context="readiness ready flag")
        if not valid or reason == "Invalid" or state_status != result_status:
            raise ForgeError(
                "NATIVE_READINESS_UNKNOWN",
                "language-owned readiness reported an invalid bounded projection",
            )
        expected_by_identity: dict[bytes, tuple[str, NormalizedReadiness]] = {
            self.readiness_requirement_identity(requirement): (requirement, normalized)
            for requirement, normalized in ordered
        }
        if len(expected_by_identity) != len(ordered):
            raise ForgeError(
                "NATIVE_READINESS_UNKNOWN",
                "host readiness requirements have duplicate identities",
            )
        zero_identity = bytes(32)
        projected_by_identity: dict[bytes, NativeReadinessRequirement] = {}
        for index, item in enumerate(requirement_items):
            fields = self._record_value_fields(
                item,
                projection_type,
                context="readiness requirement projection",
            )
            identity = self._digest_bytes(
                fields.get("identity"),
                context="readiness requirement identity",
                type_identity=self._abi_record_type(abi, "Digest32", context="Digest32"),
            )
            pass_count = self._integer(fields.get("pass_count"), context="readiness pass count")
            fail_count = self._integer(fields.get("fail_count"), context="readiness fail count")
            unknown_count = self._integer(
                fields.get("unknown_count"), context="readiness unknown count"
            )
            observed_count = self._integer(
                fields.get("observed_count"), context="readiness observed count"
            )
            classification = self._abi_finite_variant(
                fields.get("classification"),
                abi,
                "RequirementClass",
                context="readiness requirement classification",
            )
            stale = self._boolean(fields.get("stale"), context="readiness stale flag")
            noncomparable = self._boolean(
                fields.get("noncomparable"), context="readiness comparability flag"
            )
            valid_item = self._boolean(
                fields.get("valid"), context="readiness requirement validity"
            )
            if index >= requirement_count:
                if identity != zero_identity:
                    raise ForgeError(
                        "NATIVE_READINESS_MISMATCH",
                        "native readiness padding contains an unexpected identity",
                    )
                continue
            if identity == zero_identity:
                raise ForgeError(
                    "NATIVE_READINESS_MISMATCH",
                    "native readiness active requirement has a zero identity",
                )
            if identity not in expected_by_identity:
                raise ForgeError(
                    "NATIVE_READINESS_MISMATCH",
                    "native readiness returned an unknown requirement identity",
                )
            if identity in projected_by_identity:
                raise ForgeError(
                    "NATIVE_READINESS_MISMATCH",
                    "native readiness returned a duplicate requirement identity",
                )
            requirement, normalized = expected_by_identity[identity]
            statuses = [record.get("status") for record in normalized["records"]]
            if (pass_count, fail_count, unknown_count, observed_count) != (
                statuses.count("PASS"),
                statuses.count("FAIL"),
                statuses.count("UNKNOWN"),
                len(statuses),
            ):
                raise ForgeError(
                    "NATIVE_READINESS_MISMATCH",
                    f"native readiness status counts disagree for requirement {requirement}",
                )
            freshness = normalized["freshness"].upper()
            expected_stale = freshness == "STALE"
            expected_noncomparable = (
                freshness in {"NOTAPPLICABLE", "UNKNOWN"}
                or not normalized["comparable"]
                or not normalized["environment_match"]
                or not normalized["policy_match"]
                or not normalized["authority_match"]
            )
            if stale != expected_stale or noncomparable != expected_noncomparable:
                raise ForgeError(
                    "NATIVE_READINESS_MISMATCH",
                    f"native readiness comparison flags disagree for requirement {requirement}",
                )
            projected_by_identity[identity] = NativeReadinessRequirement(
                identity=identity,
                classification=classification,
                pass_count=pass_count,
                fail_count=fail_count,
                unknown_count=unknown_count,
                observed_count=observed_count,
                stale=stale,
                noncomparable=noncomparable,
                valid=valid_item,
            )
        if len(projected_by_identity) != requirement_count:
            raise ForgeError(
                "NATIVE_READINESS_MISMATCH",
                "native readiness returned missing requirement identities",
            )
        projected = [
            projected_by_identity[self.readiness_requirement_identity(requirement)]
            for requirement, _ in ordered
        ]
        if not all(item.valid for item in projected):
            raise ForgeError(
                "NATIVE_READINESS_UNKNOWN",
                "native readiness contains an invalid requirement projection",
            )
        aggregate_fields = {
            "present_count": self._integer(
                state_fields.get("present_count"), context="readiness present count"
            ),
            "missing_count": self._integer(
                state_fields.get("missing_count"), context="readiness missing count"
            ),
            "failed_count": self._integer(
                state_fields.get("failed_count"), context="readiness failed count"
            ),
            "unknown_count": self._integer(
                state_fields.get("unknown_count"), context="readiness unknown count"
            ),
            "stale_count": self._integer(
                state_fields.get("stale_count"), context="readiness stale count"
            ),
            "noncomparable_count": self._integer(
                state_fields.get("noncomparable_count"), context="readiness noncomparable count"
            ),
        }
        expected_aggregate = {
            "present_count": sum(item.observed_count > 0 for item in projected),
            "missing_count": sum(item.classification == "Missing" for item in projected),
            "failed_count": sum(item.classification == "Failed" for item in projected),
            "unknown_count": sum(item.classification == "Unknown" for item in projected),
            "stale_count": sum(item.stale for item in projected),
            "noncomparable_count": sum(item.noncomparable for item in projected),
        }
        if aggregate_fields != expected_aggregate:
            raise ForgeError(
                "NATIVE_READINESS_MISMATCH",
                "native readiness aggregate counts are inconsistent",
            )
        result = NativeReadinessProjection(
            requirements=tuple(projected),
            status=result_status,
            reason=reason,
            present_count=aggregate_fields["present_count"],
            missing_count=aggregate_fields["missing_count"],
            failed_count=aggregate_fields["failed_count"],
            unknown_count=aggregate_fields["unknown_count"],
            stale_count=aggregate_fields["stale_count"],
            noncomparable_count=aggregate_fields["noncomparable_count"],
            ready=ready,
            valid=valid,
        )
        _READINESS_CACHE[cache_key] = result
        return result

    def bundle_precondition_projection(
        self,
        *,
        requested_candidate: str | None,
        current_candidate: str | None,
        candidate_present: bool,
        candidate_matches: bool,
        candidate_current: bool,
        selected: bool,
        frozen: bool,
        freeze_current: bool,
        mode: str,
        request_valid: bool,
        evidence_status: str,
        evidence_ready: bool,
    ) -> NativeBundlePreconditionProjection:
        """Project deterministic bundle authorization through the MNCS kernel."""

        if mode not in {"development", "evaluator"}:
            raise ForgeError("NATIVE_BUNDLE_UNKNOWN", "bundle mode is malformed")
        if evidence_status not in _STATUS_VARIANTS:
            raise ForgeError("NATIVE_BUNDLE_UNKNOWN", "bundle evidence status is malformed")
        flags = {
            "candidate_present": candidate_present,
            "candidate_matches": candidate_matches,
            "candidate_current": candidate_current,
            "selected": selected,
            "frozen": frozen,
            "freeze_current": freeze_current,
            "request_valid": request_valid,
            "evidence_ready": evidence_ready,
        }
        if not all(isinstance(value, bool) for value in flags.values()):
            raise ForgeError("NATIVE_BUNDLE_UNKNOWN", "bundle precondition flags are malformed")
        self.ensure_available()
        abi = self.language_owned_abi()
        function_contract = abi.functions.get("bundle_preconditions")
        if function_contract is None:
            raise ForgeError("NATIVE_ABI_UNKNOWN", "bundle precondition function is absent")
        inputs = function_contract["inputs"]
        outputs = function_contract["outputs"]
        if len(inputs) != 1 or len(outputs) != 1:
            raise ForgeError("NATIVE_ABI_UNKNOWN", "bundle precondition ABI has invalid arity")
        input_contract = inputs[0].get("record")
        output_contract = outputs[0].get("record")
        if not isinstance(input_contract, Mapping) or not isinstance(output_contract, Mapping):
            raise ForgeError("NATIVE_ABI_UNKNOWN", "bundle precondition ABI is not record-based")
        input_type = input_contract.get("type_identity")
        output_type = output_contract.get("type_identity")
        if not isinstance(input_type, str) or not isinstance(output_type, str):
            raise ForgeError(
                "NATIVE_ABI_UNKNOWN", "bundle precondition ABI identities are malformed"
            )
        digest_type = self._abi_record_type(abi, "Digest32", context="Digest32")
        request_identity = (
            requested_candidate if requested_candidate is not None else current_candidate
        )
        request_value = self._record_value(
            input_type,
            "BundleInput",
            {
                "candidate_current": {"boolean": {"value": candidate_current}},
                "candidate_matches": {"boolean": {"value": candidate_matches}},
                "candidate_present": {"boolean": {"value": candidate_present}},
                "current_candidate": self._digest_value(
                    current_candidate, context="bundle current candidate", type_identity=digest_type
                ),
                "evidence_ready": {"boolean": {"value": evidence_ready}},
                "evidence_status": self._abi_finite_value(
                    abi, "Status", evidence_status, context="bundle evidence status"
                ),
                "freeze_current": {"boolean": {"value": freeze_current}},
                "frozen": {"boolean": {"value": frozen}},
                "mode": self._abi_finite_value(
                    abi, "BundleMode", mode.capitalize(), context="bundle mode"
                ),
                "request_valid": {"boolean": {"value": request_valid}},
                "requested_candidate": self._digest_value(
                    request_identity,
                    context="bundle requested candidate",
                    type_identity=digest_type,
                ),
                "selected": {"boolean": {"value": selected}},
            },
        )
        cache_key = (
            NATIVE_BUNDLE_CONTRACT,
            self.semantic_input_identity(),
            json.dumps(
                {
                    "requested_candidate": requested_candidate,
                    "current_candidate": current_candidate,
                    **flags,
                    "mode": mode,
                    "evidence_status": evidence_status,
                },
                sort_keys=True,
            ),
        )
        cached = _BUNDLE_CACHE.get(cache_key)
        if cached is not None:
            return cached
        request = {
            "schema_version": NATIVE_SCHEMA_VERSION,
            "target": {"module": _CORE_MODULE, "function": "bundle_preconditions"},
            "arguments": [request_value],
            "step_budget": 100_000,
        }
        with tempfile.TemporaryDirectory(prefix=".mncs-native-", dir=self.forge_root) as directory:
            request_path = Path(directory) / "bundle-preconditions-request.json"
            request_path.write_text(json.dumps(request), encoding="utf-8")
            invocation = self.execute(self.native_source, request_path)
        if not invocation.ok or invocation.payload is None:
            raise ForgeError(
                "NATIVE_BUNDLE_UNKNOWN",
                "language-owned bundle preconditions did not return valid JSON "
                f"(returncode {invocation.returncode})",
            )
        result_fields = self._record_fields(invocation.payload, context="bundle preconditions")
        returned = invocation.payload.get("returned")
        if (
            not isinstance(returned, list)
            or len(returned) != 1
            or not isinstance(returned[0], Mapping)
            or not isinstance(returned[0].get("record"), Mapping)
            or returned[0]["record"].get("type_identity") != output_type
        ):
            raise ForgeError(
                "NATIVE_ABI_MISMATCH", "bundle precondition result type disagrees with language ABI"
            )
        state_type = self._abi_record_type(abi, "BundleState", context="BundleState")
        state_fields = self._record_value_fields(
            result_fields.get("state"), state_type, context="bundle precondition state"
        )
        result_status = self._abi_finite_variant(
            result_fields.get("status"), abi, "Status", context="bundle precondition result status"
        )
        state_status = self._abi_finite_variant(
            state_fields.get("status"), abi, "Status", context="bundle precondition state status"
        )
        result_reason = self._abi_finite_variant(
            result_fields.get("reason"),
            abi,
            "BundleReason",
            context="bundle precondition result reason",
        )
        state_reason = self._abi_finite_variant(
            state_fields.get("reason"),
            abi,
            "BundleReason",
            context="bundle precondition state reason",
        )
        valid = self._boolean(state_fields.get("valid"), context="bundle precondition validity")
        ready = self._boolean(state_fields.get("ready"), context="bundle precondition ready flag")
        returned_evidence_status = self._abi_finite_variant(
            state_fields.get("evidence_status"),
            abi,
            "Status",
            context="bundle precondition evidence status",
        )
        returned_evidence_ready = self._boolean(
            state_fields.get("evidence_ready"), context="bundle precondition evidence ready flag"
        )
        expected_ready = result_status == "PASS" and result_reason == "Ready"
        if (
            not valid
            or state_status != result_status
            or state_reason != result_reason
            or ready != expected_ready
            or returned_evidence_status != evidence_status
            or returned_evidence_ready != evidence_ready
        ):
            raise ForgeError(
                "NATIVE_BUNDLE_UNKNOWN",
                "language-owned bundle precondition projection is inconsistent",
            )
        result = NativeBundlePreconditionProjection(
            ready=ready,
            status=result_status,
            reason=result_reason,
            evidence_status=returned_evidence_status,
            evidence_ready=returned_evidence_ready,
            valid=valid,
        )
        _BUNDLE_CACHE[cache_key] = result
        return result
