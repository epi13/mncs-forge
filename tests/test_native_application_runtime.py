from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path


def test_linked_provider_uses_common_runtime_but_selected_source(tmp_path, monkeypatch):
    script = Path(__file__).resolve().parents[1] / 'scripts/native_application_execution_runtime.py'
    spec = importlib.util.spec_from_file_location('forge_selected_runtime', script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    repository = tmp_path / 'forge'
    repository.mkdir()
    subprocess.run(['git', 'init', '-q', str(repository)], check=True)
    (repository / 'scripts').mkdir()
    provider = repository / 'scripts/native_application_execution_provider.py'
    provider.write_text('# selected-source fixture\n')
    subprocess.run(['git', '-C', str(repository), 'add', '.'], check=True)
    subprocess.run(['git', '-C', str(repository), '-c', 'user.name=Fixture', '-c',
                    'user.email=fixture@example.invalid', 'commit', '-qm', 'fixture'], check=True)
    runtime = repository / '.venv/bin/python'
    runtime.parent.mkdir(parents=True)
    runtime.symlink_to(sys.executable)
    worktree = tmp_path / 'isolated'
    subprocess.run(['git', '-C', str(repository), 'worktree', 'add', '-qb', 'isolated', str(worktree)], check=True)
    assert module.runtime_python(worktree) == runtime
    selected_provider = worktree / 'scripts/native_application_execution_provider.py'
    monkeypatch.setattr(module, 'ROOT', worktree)
    monkeypatch.setattr(module, 'PROVIDER', selected_provider)
    monkeypatch.setattr(module.sys, 'argv', ['runtime', '--call-json', '{"arguments":[]}'])
    calls = []
    monkeypatch.setattr(module.os, 'execv', lambda executable, argv: calls.append((executable, argv)))
    module.main()
    assert calls == [(str(runtime), [str(runtime), str(selected_provider), '--call-json', '{"arguments":[]}'])]
