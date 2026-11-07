"""Bounded provider-owned artifact transport; Commons/Doctor decide admission.

Owners declare inputs and a compiler capability. This adapter observes bytes,
serializes receipts, invokes that capability, and retains the admitted result.
It neither invents provenance nor turns compilation into verification PASS.
"""
from __future__ import annotations

import atexit
import fcntl
import hashlib
import json
import os
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Any

from .retained_embed import RetainedEmbedSession

BUILD_SCHEMA = 'mncs.provider-build-receipt/1'
EXECUTION_SCHEMA = 'mncs.provider-execution-provenance/1'


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()


def byte_digest(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def integer(value: int) -> dict[str, Any]:
    return {'integer': {'value': int(value), 'type': {'bits': 64, 'signed': False}}}


def bytes_value(value: str) -> dict[str, Any]:
    return {'sequence': {'values': [{'byte': {'value': b}} for b in bytes.fromhex(value)]}}


class ProviderArtifactError(RuntimeError):
    pass


_NATIVE_DIGEST_ADAPTER_LOCK = threading.RLock()
_NATIVE_DIGEST_ADAPTER: Any = None
_NATIVE_DIGEST_ADAPTER_KEY: tuple[str, tuple[tuple[str, str], ...]] | None = None
_NATIVE_DIGEST_ENVIRONMENT = (
    'MNCS_CLI', 'MNCS_LANGUAGE_ROOT', 'MNCS_LIBRARY_ROOT', 'MNCS_STDLIB_ROOT',
    'MNCS_EMBED_LIB', 'MNCS_NATIVE_APPLICATION_CACHE_DIR', 'MNCS_RUNTIME_PROFILE',
    'MNCS_STDLIB_BUNDLE', 'MNCS_BACKEND_CONFIG', 'MNCS_TARGET_PROFILE',
    'MNCS_COMPILER_CHECKOUT', 'MNCS_COMPILER_PROBE', 'MNCS_VM_CHECKOUT', 'MNCS_VM_BIN',
)


def _provider_digest_adapter(forge_root: Path):
    """Share one bounded digest application across provider instances."""
    from importlib.resources import as_file, files

    from .errors import ForgeError
    from .mncs_native import NativeForgeAdapter

    global _NATIVE_DIGEST_ADAPTER, _NATIVE_DIGEST_ADAPTER_KEY
    root = forge_root.resolve()
    key = (str(root), tuple((name, os.environ.get(name, '')) for name in _NATIVE_DIGEST_ENVIRONMENT))
    with _NATIVE_DIGEST_ADAPTER_LOCK:
        if _NATIVE_DIGEST_ADAPTER is not None and key == _NATIVE_DIGEST_ADAPTER_KEY:
            return _NATIVE_DIGEST_ADAPTER
        if _NATIVE_DIGEST_ADAPTER is not None:
            _NATIVE_DIGEST_ADAPTER.close()

        class ProviderDigestAdapter(NativeForgeAdapter):
            def __init__(self, adapter_root: Path) -> None:
                super().__init__(adapter_root)
                resource_root = files('mncs_forge.resources').joinpath(
                    'native', 'forge', 'provider_digest')
                self.native_root = self._resource_stack.enter_context(as_file(resource_root))
                self.native_source = self.native_root / 'core.mncs'
                configured_cache = os.environ.get('MNCS_NATIVE_APPLICATION_CACHE_DIR')
                cache_root = (Path(configured_cache).expanduser() if configured_cache else
                              self.forge_root / '.mncs' / 'cache' / 'native-applications')
                if not cache_root.is_absolute():
                    cache_root = self.forge_root / cache_root
                self._artifact_cache_override = cache_root / 'provider-digest'

            def ensure_available(self) -> None:
                if not self.available:
                    raise ForgeError('NATIVE_UNAVAILABLE',
                                     'selected compiler and Stdlib are required for provider identities')
                if not self.native_source.is_file():
                    raise ForgeError('NATIVE_UNAVAILABLE', 'provider digest MNCS source is missing')

        _NATIVE_DIGEST_ADAPTER = ProviderDigestAdapter(root)
        _NATIVE_DIGEST_ADAPTER_KEY = key
        return _NATIVE_DIGEST_ADAPTER


def _close_native_digest_adapter() -> None:
    global _NATIVE_DIGEST_ADAPTER, _NATIVE_DIGEST_ADAPTER_KEY
    with _NATIVE_DIGEST_ADAPTER_LOCK:
        adapter = _NATIVE_DIGEST_ADAPTER
        _NATIVE_DIGEST_ADAPTER = None
        _NATIVE_DIGEST_ADAPTER_KEY = None
    if adapter is not None:
        adapter.close()


atexit.register(_close_native_digest_adapter)


class _NativeProviderSha256:
    """Stream provider identity bytes through Forge's retained MNCS runtime."""

    MODULE = 'mncs.forge.provider_digest.v1'
    CHUNK_BYTES = 1024
    UPDATE_BUDGET = 500_000

    def __init__(self, forge_root: Path) -> None:
        from .mncs_native import _record_type_identity

        self.adapter = _provider_digest_adapter(forge_root)
        self._lock = threading.RLock()
        self._state_identity = _record_type_identity(
            'mncs.std.sha256.v1', 'DigestState',
            {'h': '[u32; 8]', 'total': 'u64', 'buf': '[byte; 64]', 'buffered': 'u64'})
        self._state_name = 'DigestState'

    def _call(self, function: str, arguments: list[dict[str, Any]], *, budget: int) -> object:
        try:
            payload, _elapsed = self.adapter.ensure_session().call(
                self.MODULE, function, arguments, step_budget=budget)
        except Exception as error:
            raise ProviderArtifactError(
                f'native provider SHA-256 {function} failed: {error}') from error
        if not isinstance(payload, dict):
            raise ProviderArtifactError(f'native provider SHA-256 {function} returned malformed data')
        returned = payload.get('returned')
        if payload.get('status') != 'returned' or not isinstance(returned, list) or len(returned) != 1:
            raise ProviderArtifactError(f'native provider SHA-256 {function} did not return one value')
        return returned[0]

    def _validate_state(self, value: object) -> dict[str, Any]:
        if not isinstance(value, dict) or not isinstance(value.get('record'), dict):
            raise ProviderArtifactError('native provider SHA-256 returned a non-record state')
        record = value['record']
        fields = record.get('fields')
        if (record.get('type_identity') != self._state_identity or
                record.get('name') != self._state_name or not isinstance(fields, list)):
            raise ProviderArtifactError('native provider SHA-256 returned an invalid state type')
        field_names = [field[0] for field in fields if isinstance(field, list) and len(field) == 2]
        if len(field_names) != len(fields) or set(field_names) != {'h', 'total', 'buf', 'buffered'}:
            raise ProviderArtifactError('native provider SHA-256 returned malformed state fields')
        return value

    def _digest(self, chunks: Any) -> bytes:
        with self._lock:
            state = self._validate_state(self._call('provider_sha256_init', [], budget=10_000))
            for chunk in chunks:
                if not isinstance(chunk, bytes) or not 0 < len(chunk) <= self.CHUNK_BYTES:
                    raise ProviderArtifactError('provider SHA-256 chunks must contain 1..1024 bytes')
                data = {'sequence': {'values': [{'byte': {'value': value}} for value in chunk]}}
                state = self._validate_state(self._call(
                    'provider_sha256_update', [state, data], budget=self.UPDATE_BUDGET))
            output = self._call('provider_sha256_finalize', [state], budget=100_000)
            if not isinstance(output, dict) or not isinstance(output.get('sequence'), dict):
                raise ProviderArtifactError('native provider SHA-256 returned a malformed digest')
            values = output['sequence'].get('values')
            if not isinstance(values, list) or len(values) != 32:
                raise ProviderArtifactError('native provider SHA-256 returned a non-32-byte digest')
            result = bytearray()
            for item in values:
                if not isinstance(item, dict) or not isinstance(item.get('byte'), dict):
                    raise ProviderArtifactError('native provider SHA-256 returned a malformed byte')
                value = item['byte'].get('value')
                if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 255:
                    raise ProviderArtifactError('native provider SHA-256 returned an invalid byte')
                result.append(value)
            return bytes(result)

    def digest_bytes(self, data: bytes) -> bytes:
        return self._digest(data[offset:offset + self.CHUNK_BYTES]
                            for offset in range(0, len(data), self.CHUNK_BYTES))

    def digest_file(self, path: Path) -> bytes:
        def chunks():
            with path.open('rb') as stream:
                while data := stream.read(self.CHUNK_BYTES):
                    yield data
        return self._digest(chunks())


class ProviderArtifact:
    """One owner declaration, one confined output namespace, exact receipts."""

    def __init__(self, specification: dict[str, Any], *, roots: dict[str, str | Path],
                 compiler: str | Path, embed: str | Path, cache: str | Path) -> None:
        self.spec = specification
        self.roots = {key: Path(value).resolve() for key, value in roots.items()}
        self.compiler, self.embed = Path(compiler).resolve(), Path(embed).resolve()
        if specification.get('schema_version') != 'mncs.provider-artifact-declaration/1' or specification['configuration'].get('effects') != []:
            raise ProviderArtifactError('undeclared or effectful provider artifact capability')
        selected_cache = Path(cache).resolve()
        if any(selected_cache.is_relative_to(root) for root in self.roots.values()):
            raise ProviderArtifactError('artifact output cache overlaps selected provider source')
        self.cache = selected_cache / digest(specification['provider'])
        self.cache.mkdir(parents=True, exist_ok=True)
        self._native_hasher: _NativeProviderSha256 | None = None
        self._native_digest_lock = threading.RLock()
        self.runtime: RetainedEmbedSession | None = None
        self.receipt: dict[str, Any] | None = None
        self.rebuilds = 0
        self._inputs_lock = threading.Lock()
        self._inputs_signature: tuple[Any, ...] | None = None
        self._inputs_cache: dict[str, Any] | None = None

    def path(self, binding: dict[str, Any]) -> Path:
        root = self.roots[binding['repository']]
        raw: str = binding['path']
        path = (root / raw).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            raise ProviderArtifactError('declared provider input is missing or escapes selected root')
        return path

    @staticmethod
    def _stat_signature(path: Path) -> tuple[int, ...]:
        """Cheap freshness tuple; ctime closes mtime-preserving rewrites."""
        link = path.lstat()
        target = path.stat()
        return (link.st_mtime_ns, link.st_ctime_ns, link.st_size, link.st_ino,
                target.st_mtime_ns, target.st_ctime_ns, target.st_size, target.st_ino)

    def _material_signature(self) -> tuple[Any, ...] | None:
        """Stat-only generation over exactly the files inputs() hashes.

        Returns None when any material stat is unreadable so the caller falls
        back to the exact path (which raises the canonical error).
        """
        try:
            # Exact canonical declaration bytes are a cache-generation key,
            # not a published digest; this keeps warm freshness checks cheap.
            declaration = json.dumps(
                self.spec, sort_keys=True, separators=(',', ':'), ensure_ascii=False)
            sources = tuple(
                (f"{item['repository']}:{item['path']}",
                 self._stat_signature(self.path(item)))
                for item in self.spec['inputs']
            )
            libraries = []
            for binding in self.spec['dependency_roots']:
                root = (self.roots[binding['repository']] / binding['path']).resolve()
                if not root.is_relative_to(self.roots[binding['repository']]):
                    raise ProviderArtifactError('declared library escapes selected root')
                files = sorted(root.rglob('*.mncs'))
                if len(files) > 2048:
                    raise ProviderArtifactError('library input exceeds bounded provider capability')
                entries = []
                for path in files:
                    # No resolve here: any add/remove/replace/content change
                    # alters this list or a stat tuple, and the resulting
                    # mismatch runs the exact inputs() path with its canonical
                    # containment checks. A matching signature reuses a
                    # generation whose containment is already established.
                    entries.append((str(path.relative_to(root)), self._stat_signature(path)))
                libraries.append((binding['repository'], binding['path'], tuple(entries)))
            compiler = self._stat_signature(self.compiler)
            transport = self._stat_signature(Path(__file__))
        except OSError:
            return None
        return (declaration, sources, tuple(libraries), compiler, transport)

    def _hasher(self) -> _NativeProviderSha256:
        with self._native_digest_lock:
            if self._native_hasher is None:
                self._native_hasher = _NativeProviderSha256(Path(__file__).resolve().parents[2])
            return self._native_hasher

    def _digest(self, value: object) -> str:
        encoded = json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()
        return self._hasher().digest_bytes(encoded).hex()

    def expected_inputs(self) -> dict[str, Any]:
        """Authoritative input identity, reusing it while the source generation is unchanged.

        The stat signature is recomputed on every call (live across processes);
        only the byte reads are skipped on a warm generation. Corruption and
        movement checks below this layer keep reading artifact bytes.
        """
        with self._inputs_lock:
            signature = self._material_signature()
            if (signature is not None and signature == self._inputs_signature
                    and self._inputs_cache is not None):
                return self._inputs_cache
            expected = self.inputs()
            self._inputs_signature = signature
            self._inputs_cache = expected
            return expected

    def inputs(self) -> dict[str, Any]:
        sources = {f"{item['repository']}:{item['path']}": byte_digest(self.path(item))
                   for item in self.spec['inputs']}
        libraries = []
        for binding in self.spec['dependency_roots']:
            root = (self.roots[binding['repository']] / binding['path']).resolve()
            if not root.is_relative_to(self.roots[binding['repository']]):
                raise ProviderArtifactError('declared library escapes selected root')
            files = sorted(root.rglob('*.mncs'))
            if len(files) > 2048:
                raise ProviderArtifactError('library input exceeds bounded provider capability')
            material = []
            for path in files:
                if not path.resolve().is_relative_to(root):
                    raise ProviderArtifactError('library input symlink escapes selected root')
                material.append([str(path.relative_to(root)), byte_digest(path)])
            libraries.append(dict(binding, content_identity=self._digest(material)))
        return {'provider': self.spec['provider'], 'declaration_identity': self._digest(self.spec),
                'source_inputs': sources, 'compiler_artifact_sha256': byte_digest(self.compiler),
                'build_transport_sha256': byte_digest(__file__),
                'configuration': self.spec['configuration'],
                'dependency_roots': libraries,
                'stdlib': self.spec['stdlib'], 'abi': self.spec['abi']}

    def _native(self, binding: dict[str, Any], function: str,
                arguments: list[dict[str, Any]]) -> tuple[int, dict[str, Any]]:
        roots = [str(self.roots[item['repository']] / item['path']) for item in self.spec['dependency_roots']]
        command = [str(self.compiler), 'call', str(self.path(binding)), '--module', binding['module'],
                   '--function', function, '--args-json', json.dumps(arguments),
                   '--cache-dir', str(self.cache / 'admission')]
        for root in roots:
            command.extend(['--library', root])
        result = subprocess.run(command, capture_output=True, text=True, timeout=60, check=False)
        try:
            document = json.loads(result.stdout)
            returned = document['call']['returned'][0]['integer']['value']
            if result.returncode or document['status'] != 'returned':
                raise ValueError('native admission did not return')
            return int(returned), {key: document['call'].get(key) for key in ('artifact_identity', 'artifact_sha256', 'backend')}
        except (ValueError, KeyError, TypeError) as error:
            raise ProviderArtifactError('owner admission unavailable: ' + result.stdout[:500] + result.stderr[-300:]) from error

    def _load(self, expected: dict[str, Any]) -> dict[str, Any] | None:
        try:
            receipt: Any = json.loads((self.cache / 'current.json').read_text())
            if receipt['schema_version'] != BUILD_SCHEMA or self._digest(receipt['core']) != receipt['identity']:
                return None
            return dict(receipt)
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def _artifact(self, receipt: dict[str, Any]) -> Path:
        address: str = receipt['core']['artifact']['address']
        path = (self.cache / address).resolve()
        if not path.is_relative_to(self.cache):
            raise ProviderArtifactError('artifact receipt escapes confined cache')
        return path

    def _state(self, expected: dict[str, Any], receipt: dict[str, Any] | None) -> int:
        intact = False
        built = '00' * 32
        if receipt:
            try:
                built = self._digest(receipt['core']['inputs'])
                artifact = json.loads(self._artifact(receipt).read_bytes())
                claimed = receipt['core']['artifact']
                intact = (byte_digest(self._artifact(receipt)) == claimed['sha256']
                          and artifact['identity'] == claimed['identity']
                          and artifact['bytes_sha256'] == claimed['payload_sha256']
                          and artifact['schema_version'] == self.spec['abi']['backend_schema']
                          and self._digest(artifact['function_value_contracts']) == receipt['core']['inventory_identity'])
            except (KeyError, OSError, ValueError, ProviderArtifactError):
                pass
        state, _ = self._native(self.spec['admission'], 'artifact_state',
                                [bytes_value(self._digest(expected)), bytes_value(built), integer(1),
                                 integer(receipt is not None), integer(intact)])
        return state

    def ensure(self) -> dict[str, Any]:
        # Artifact publication is single-flight inside the selected bounded
        # cache. Shared source/outputs are never edited by this capability.
        with (self.cache / 'reconcile.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            expected = self.expected_inputs()
            receipt = self._load(expected)
            state = self._state(expected, receipt)
            decision, admission = self._native(self.spec['remediation'], 'repair_artifact',
                                               [integer(state), integer(1), integer(1), integer(1), integer(0)])
            if decision == 1:
                receipt = self._build(expected, admission)
                if self._state(expected, receipt) != 1:
                    raise ProviderArtifactError('post-build provenance admission refused')
                self.rebuilds += 1
            elif decision != 0:
                raise ProviderArtifactError(f'provider artifact remediation blocked: {decision}')
            if self.expected_inputs() != expected:
                raise ProviderArtifactError('provider inputs moved during admission')
            if receipt is None:
                raise ProviderArtifactError('provider admission produced no receipt')
            self.receipt = receipt
            return receipt

    def _build(self, expected: dict[str, Any], admission: dict[str, Any]) -> dict[str, Any]:
        source = self.path(self.spec['source'])
        roots = [str(self.roots[item['repository']] / item['path']) for item in self.spec['dependency_roots']]
        env = dict(os.environ)
        env['MNCS_LIBRARY_PATH'] = os.pathsep.join(roots)
        with tempfile.TemporaryDirectory(prefix='build-', dir=self.cache) as directory:
            command = [str(self.compiler), 'compile', str(source), '--emit', 'backend', '--target',
                       self.spec['configuration']['target'], '--output-dir', directory]
            result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=60, check=False)
            path = Path(directory) / 'backend.json'
            if result.returncode or not path.is_file():
                raise ProviderArtifactError('owner build failed: ' + (result.stderr or result.stdout)[-1500:])
            data = path.read_bytes()
            artifact = json.loads(data)
            modules = sorted({name.rsplit('::', 1)[0] for name in artifact['function_value_contracts'] if '::' in name})
            if modules != sorted(self.spec['modules']) or artifact['unsupported'] or artifact['status'] != 'PASS' or artifact['schema_version'] != self.spec['abi']['backend_schema']:
                raise ProviderArtifactError('compiler artifact closure differs from declared owner inputs')
            if self.expected_inputs() != expected:
                raise ProviderArtifactError('provider inputs moved during build')
            sha = hashlib.sha256(data).hexdigest()
            address = 'artifacts/' + sha + '.json'
            destination = self.cache / address
            destination.parent.mkdir(exist_ok=True)
            if destination.exists() and byte_digest(destination) != sha:
                raise ProviderArtifactError('content-addressed provider artifact is corrupt')
            if not destination.exists():
                os.replace(path, destination)
            core = {'inputs': expected, 'artifact': {'address': address, 'sha256': sha,
                    'identity': artifact['identity'], 'payload_sha256': artifact['bytes_sha256'],
                    'interface_identity': artifact['interface_identity'], 'schema_version': artifact['schema_version'],
                    'backend': artifact['backend'], 'target': artifact['target']['identity']},
                    'inventory_identity': self._digest(artifact['function_value_contracts']),
                    'build_capability': self.spec['build_capability'],
                    'build_invocation_identity': self._digest({'compiler': expected['compiler_artifact_sha256'], 'source': self.spec['source'], 'configuration': self.spec['configuration'], 'roots': self.spec['dependency_roots']}),
                    'remediation_admission': admission}
            receipt = {'schema_version': BUILD_SCHEMA, 'identity': self._digest(core), 'core': core}
            pending = self.cache / 'current.pending'
            pending.write_text(json.dumps(receipt, sort_keys=True) + '\n')
            os.replace(pending, self.cache / 'current.json')
            return receipt

    def call(self, module: str, function: str, arguments: list[dict[str, Any]],
             *, step_budget: int = 100000) -> tuple[dict[str, Any], dict[str, Any]]:
        expected = self.expected_inputs()
        # A retained session is never called after input/artifact/runtime drift.
        # For a cold process, native admission runs once before opening it.
        if self.receipt is None or expected != self.receipt['core']['inputs']:
            self.close()
            self.ensure()
        receipt = self.receipt
        if receipt is None:
            raise ProviderArtifactError('provider admission produced no receipt')
        path = self._artifact(receipt)
        if byte_digest(path) != receipt['core']['artifact']['sha256']:
            self.close()
            raise ProviderArtifactError('admitted artifact bytes changed')
        runtime_sha = byte_digest(self.embed)
        if self.runtime is not None and self.runtime.executor_sha256 != runtime_sha:
            self.close()
        if self.runtime is None:
            self.runtime = RetainedEmbedSession(self.embed, path.read_bytes())
        info = self.runtime.info()
        if info['artifact_identity'] != receipt['core']['artifact']['identity'] or info['artifact_sha256'] != receipt['core']['artifact']['payload_sha256']:
            raise ProviderArtifactError('executing artifact does not match build receipt')
        result, _elapsed = self.runtime.call(module, function, arguments, step_budget=step_budget)
        if self.expected_inputs() != expected or byte_digest(self.embed) != runtime_sha:
            self.close()
            raise ProviderArtifactError('execution inputs moved; result cannot establish evidence')
        core = {'provider': self.spec['provider'], 'build_receipt': receipt['identity'],
                'artifact_identity': info['artifact_identity'], 'artifact_sha256': info['artifact_sha256'],
                'executor_sha256': runtime_sha, 'abi': self.spec['abi'],
                'operation': module + '::' + function,
                'subject_identity': 'mncs.native-decision-input:' + self._digest(arguments), 'input_identity': self._digest({'arguments': arguments, 'step_budget': step_budget, 'grants': []}),
                'inventory_identity': receipt['core']['inventory_identity'], 'result_identity': self._digest(result)}
        execution = {'schema_version': EXECUTION_SCHEMA, 'identity': self._digest(core), 'core': core,
                     'build': receipt}
        return result, execution

    def close(self) -> None:
        if self.runtime is not None:
            self.runtime.close()
            self.runtime = None
        with self._native_digest_lock:
            # The digest application is process-shared and is closed at exit.
            self._native_hasher = None
