"""Real owner build/admission/execution and fail-closed provenance mutation."""
import copy
import hashlib
import json
import os
import shutil
from pathlib import Path
from unittest.mock import patch

import pytest

from mncs_forge.provider_artifacts import ProviderArtifact, ProviderArtifactError, integer
from mncs_forge.retained_embed import RetainedEmbedError, RetainedEmbedSession


def _project_family_root() -> Path:
    test_path = Path(__file__).resolve()
    for candidate in test_path.parents:
        if (candidate / 'mncs-automation/.mncs/coherence-artifact.json').is_file():
            return candidate
    return test_path.parents[2]


BASE = _project_family_root()
BINARY = Path(os.environ.get('MNCS_BINARY', '/unavailable'))
EMBED = BINARY.parent / 'libmncs_embed.so'


@pytest.fixture
def provider(tmp_path):
    if not BINARY.is_file() or not EMBED.is_file() or not (BASE / 'mncs-automation/.mncs/coherence-artifact.json').is_file():
        pytest.skip('explicit matched owner providers required')
    spec = json.loads((BASE / 'mncs-automation/.mncs/coherence-artifact.json').read_text())
    roots = {}
    for repository in {item['repository'] for item in spec['inputs']}:
        source = BASE / repository
        root = tmp_path / repository
        root.mkdir()
        roots[repository] = root
        for item in spec['inputs']:
            if item['repository'] == repository:
                path = root / item['path']
                path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source / item['path'], path)
    return ProviderArtifact(spec, roots=roots, compiler=BINARY, embed=EMBED, cache=tmp_path / 'cache')


def call(provider):
    args = [{'sequence': {'values': [{'sequence': {'values': [integer(i) for i in [1, 1, 1, 0, 1, 0, 0, 0]]}}]}}]
    return provider.call('mncs.automation.coherence.v1', 'route_batch', args)


def test_native_provider_sha256_matches_independent_vectors(provider):
    hasher = provider._hasher()
    vectors = (b'', b'abc', bytes(range(56)), bytes(range(64)), bytes(range(255)),
               b'large-provider-input' * 61, b'boundary' * 128)
    try:
        for value in vectors:
            assert hasher.digest_bytes(value) == hashlib.sha256(value).digest()
        structured = {'z': 'føø', 'a': [0, 1, {'truth': True}]}
        canonical = json.dumps(structured, sort_keys=True, separators=(',', ':'),
                               ensure_ascii=False).encode()
        assert provider._digest(structured) == hashlib.sha256(canonical).hexdigest()
        source = provider.path(provider.spec['source'])
        assert hasher.digest_file(source) == hashlib.sha256(source.read_bytes()).digest()
    finally:
        provider.close()


def test_real_provider_repair_execution_receipt_and_quiet_reuse(provider):
    result, receipt = call(provider)
    assert result['returned'][0]['sequence']['values'][0]['integer']['value'] == 0
    assert provider.rebuilds == 1
    assert receipt['core']['build_receipt'] == provider.receipt['identity']
    assert receipt['core']['executor_sha256'] == provider.runtime.executor_sha256
    canonical_receipt = json.dumps(receipt['core'], sort_keys=True, separators=(',', ':'),
                                   ensure_ascii=False).encode()
    assert receipt['identity'] == hashlib.sha256(canonical_receipt).hexdigest()
    canonical_build = json.dumps(provider.receipt['core'], sort_keys=True, separators=(',', ':'),
                                 ensure_ascii=False).encode()
    assert provider.receipt['identity'] == hashlib.sha256(canonical_build).hexdigest()
    with patch('subprocess.Popen', side_effect=AssertionError('current provider spawned')):
        again, execution = call(provider)
    assert again == result and execution == receipt
    assert provider.rebuilds == 1
    provider.close()


def test_source_edit_rebuilds_only_owner_and_restart_reuses_exact_receipt(provider):
    call(provider)
    first = provider.receipt['identity']
    source = provider.path(provider.spec['source'])
    source.write_text(source.read_text() + '\n// owner source change\n')
    call(provider)
    assert provider.rebuilds == 2 and provider.receipt['identity'] != first
    rebuilt = provider.receipt['identity']
    (provider.cache.parent / 'unrelated-provider.txt').write_text('unrelated')
    call(provider)
    assert provider.rebuilds == 2
    provider.close()
    second = ProviderArtifact(provider.spec, roots=provider.roots, compiler=provider.compiler,
                              embed=provider.embed, cache=provider.cache.parent)
    call(second)
    assert second.rebuilds == 0 and second.receipt['identity'] == rebuilt
    second.close()


def test_bad_receipt_and_bad_artifact_cannot_execute(provider):
    call(provider)
    provider.close()
    provider._artifact(provider.receipt).write_text('{}')
    with pytest.raises(ProviderArtifactError, match='bytes changed'):
        call(provider)
    provider.receipt = None
    (provider.cache / 'current.json').write_text('{}')
    # A corrupt content address is never silently overwritten as a repair.
    with pytest.raises(ProviderArtifactError, match='corrupt'):
        provider.ensure()


def test_undeclared_compile_closure_and_abi_fail_closed(provider):
    provider.spec = copy.deepcopy(provider.spec)
    provider.spec['modules'].remove('doctor.provider.v1')
    with pytest.raises(ProviderArtifactError, match='closure'):
        provider.ensure()


def test_loaded_executor_replacement_is_refused(tmp_path):
    if not EMBED.is_file():
        pytest.skip('explicit embed runtime required')
    library = tmp_path / 'embed.so'
    shutil.copyfile(EMBED, library)
    RetainedEmbedSession._library(library)
    replacement = tmp_path / 'replacement'
    replacement.write_bytes(b'changed executor')
    os.replace(replacement, library)
    with pytest.raises(RetainedEmbedError, match='loaded executor bytes changed'):
        RetainedEmbedSession._library(library)


def test_owner_descriptor_drift_cannot_reuse_old_instance(provider):
    import importlib.util
    spec = importlib.util.spec_from_file_location('owner_reconcile_fixture', BASE / 'mncs-automation/tools/coherence_provider.py')
    owner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(owner)
    arguments = dict(roots=provider.roots, compiler=provider.compiler, embed=provider.embed, cache=provider.cache.parent)
    first = owner.provider(**arguments)
    assert owner.provider(**arguments) is first
    declaration = provider.roots['mncs-automation'] / '.mncs/coherence-artifact.json'
    material = json.loads(declaration.read_text())
    material['configuration']['effects'] = ['undeclared-write']
    declaration.write_text(json.dumps(material))
    with pytest.raises(ProviderArtifactError, match='effectful'):
        owner.provider(**arguments)
    owner.close()
