"""Bounded provider-owned artifact transport; Commons/Doctor decide admission.

Owners declare inputs and a compiler capability. This adapter observes bytes,
serializes receipts, invokes that capability, and retains the admitted result.
It neither invents provenance nor turns compilation into verification PASS.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import subprocess
import tempfile
from pathlib import Path

from .retained_embed import RetainedEmbedSession, RetainedEmbedError

BUILD_SCHEMA = 'mncs.provider-build-receipt/1'
EXECUTION_SCHEMA = 'mncs.provider-execution-provenance/1'


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()


def byte_digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def integer(value):
    return {'integer': {'value': int(value), 'type': {'bits': 64, 'signed': False}}}


def bytes_value(value):
    return {'sequence': {'values': [{'byte': {'value': b}} for b in bytes.fromhex(value)]}}


class ProviderArtifactError(RuntimeError):
    pass


class ProviderArtifact:
    """One owner declaration, one confined output namespace, exact receipts."""

    def __init__(self, specification, *, roots, compiler, embed, cache):
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
        self.runtime = None
        self.receipt = None
        self.rebuilds = 0

    def path(self, binding):
        root = self.roots[binding['repository']]
        path = (root / binding['path']).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            raise ProviderArtifactError('declared provider input is missing or escapes selected root')
        return path

    def inputs(self):
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
            libraries.append(dict(binding, content_identity=digest(material)))
        return {'provider': self.spec['provider'], 'declaration_identity': digest(self.spec),
                'source_inputs': sources, 'compiler_artifact_sha256': byte_digest(self.compiler),
                'build_transport_sha256': byte_digest(__file__),
                'configuration': self.spec['configuration'],
                'dependency_roots': libraries,
                'stdlib': self.spec['stdlib'], 'abi': self.spec['abi']}

    def _native(self, binding, function, arguments):
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

    def _load(self, expected):
        try:
            receipt = json.loads((self.cache / 'current.json').read_text())
            if receipt['schema_version'] != BUILD_SCHEMA or digest(receipt['core']) != receipt['identity']:
                return None
            return receipt
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def _artifact(self, receipt):
        path = (self.cache / receipt['core']['artifact']['address']).resolve()
        if not path.is_relative_to(self.cache):
            raise ProviderArtifactError('artifact receipt escapes confined cache')
        return path

    def _state(self, expected, receipt):
        intact = False
        built = '00' * 32
        if receipt:
            try:
                built = digest(receipt['core']['inputs'])
                artifact = json.loads(self._artifact(receipt).read_bytes())
                claimed = receipt['core']['artifact']
                intact = (byte_digest(self._artifact(receipt)) == claimed['sha256']
                          and artifact['identity'] == claimed['identity']
                          and artifact['bytes_sha256'] == claimed['payload_sha256']
                          and artifact['schema_version'] == self.spec['abi']['backend_schema']
                          and digest(artifact['function_value_contracts']) == receipt['core']['inventory_identity'])
            except (KeyError, OSError, ValueError, ProviderArtifactError):
                pass
        state, _ = self._native(self.spec['admission'], 'artifact_state',
                                [bytes_value(digest(expected)), bytes_value(built), integer(1),
                                 integer(receipt is not None), integer(intact)])
        return state

    def ensure(self):
        # Artifact publication is single-flight inside the selected bounded
        # cache. Shared source/outputs are never edited by this capability.
        with (self.cache / 'reconcile.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            expected = self.inputs()
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
            if self.inputs() != expected:
                raise ProviderArtifactError('provider inputs moved during admission')
            self.receipt = receipt
            return receipt

    def _build(self, expected, admission):
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
            if self.inputs() != expected:
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
                    'inventory_identity': digest(artifact['function_value_contracts']),
                    'build_capability': self.spec['build_capability'],
                    'build_invocation_identity': digest({'compiler': expected['compiler_artifact_sha256'], 'source': self.spec['source'], 'configuration': self.spec['configuration'], 'roots': self.spec['dependency_roots']}),
                    'remediation_admission': admission}
            receipt = {'schema_version': BUILD_SCHEMA, 'identity': digest(core), 'core': core}
            pending = self.cache / 'current.pending'
            pending.write_text(json.dumps(receipt, sort_keys=True) + '\n')
            os.replace(pending, self.cache / 'current.json')
            return receipt

    def call(self, module, function, arguments, *, step_budget=100000):
        expected = self.inputs()
        # A retained session is never called after input/artifact/runtime drift.
        # For a cold process, native admission runs once before opening it.
        if self.receipt is None or expected != self.receipt['core']['inputs']:
            self.close()
            self.ensure()
        path = self._artifact(self.receipt)
        if byte_digest(path) != self.receipt['core']['artifact']['sha256']:
            self.close()
            raise ProviderArtifactError('admitted artifact bytes changed')
        runtime_sha = byte_digest(self.embed)
        if self.runtime is not None and self.runtime.executor_sha256 != runtime_sha:
            self.close()
        if self.runtime is None:
            self.runtime = RetainedEmbedSession(self.embed, path.read_bytes())
        info = self.runtime.info()
        if info['artifact_identity'] != self.receipt['core']['artifact']['identity'] or info['artifact_sha256'] != self.receipt['core']['artifact']['payload_sha256']:
            raise ProviderArtifactError('executing artifact does not match build receipt')
        result, elapsed = self.runtime.call(module, function, arguments, step_budget=step_budget)
        if self.inputs() != expected or byte_digest(self.embed) != runtime_sha:
            self.close()
            raise ProviderArtifactError('execution inputs moved; result cannot establish evidence')
        core = {'provider': self.spec['provider'], 'build_receipt': self.receipt['identity'],
                'artifact_identity': info['artifact_identity'], 'artifact_sha256': info['artifact_sha256'],
                'executor_sha256': runtime_sha, 'abi': self.spec['abi'],
                'operation': module + '::' + function,
                'subject_identity': 'mncs.native-decision-input:' + digest(arguments), 'input_identity': digest({'arguments': arguments, 'step_budget': step_budget, 'grants': []}),
                'inventory_identity': self.receipt['core']['inventory_identity'], 'result_identity': digest(result)}
        receipt = {'schema_version': EXECUTION_SCHEMA, 'identity': digest(core), 'core': core,
                   'build': self.receipt}
        return result, receipt

    def close(self):
        if self.runtime is not None:
            self.runtime.close()
            self.runtime = None
