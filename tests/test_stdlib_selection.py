from pathlib import Path
from mncs_forge.mncs_native import selected_library_root


def test_dedicated_owner_and_explicit_missing_selection_are_preserved(tmp_path, monkeypatch):
    language = tmp_path / 'mncs-language'
    (language / 'library').mkdir(parents=True)
    dedicated = tmp_path / 'mncs-stdlib/library'
    dedicated.mkdir(parents=True)
    monkeypatch.delenv('MNCS_LIBRARY_ROOT', raising=False)
    monkeypatch.delenv('MNCS_STDLIB_ROOT', raising=False)
    assert selected_library_root(language) == dedicated
    monkeypatch.setenv('MNCS_LIBRARY_ROOT', str(tmp_path / 'missing-selected-library'))
    assert selected_library_root(language) == tmp_path / 'missing-selected-library'


def test_legacy_library_requires_absent_dedicated_provider(tmp_path, monkeypatch):
    language = tmp_path / 'language'
    (language / 'library').mkdir(parents=True)
    monkeypatch.delenv('MNCS_LIBRARY_ROOT', raising=False)
    monkeypatch.delenv('MNCS_STDLIB_ROOT', raising=False)
    assert selected_library_root(language) == language / 'library'
