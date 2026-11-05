"""Compiler scratch is disposable without taking over another live build."""
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
from unittest import mock

import pytest

from mncs_forge import compile_workspace as scratch


def test_success_failure_and_live_lease(tmp_path):
    with scratch.workspace(tmp_path) as directory:
        (directory / "backend.json").write_text("artifact")
        assert scratch.prune_inactive(tmp_path) == 0
        assert directory.is_dir()
    assert not directory.exists()
    with pytest.raises(RuntimeError):
        with scratch.workspace(tmp_path) as directory:
            raise RuntimeError("failed compile")
    assert not directory.exists()


def test_compiler_scratch_does_not_reacquire_the_environment_checkout_lock(tmp_path):
    checkout_lock = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        fcntl.flock(checkout_lock, fcntl.LOCK_EX)
        with scratch.workspace(tmp_path) as directory:
            assert directory.parent == tmp_path
            assert directory.is_dir()
    finally:
        fcntl.flock(checkout_lock, fcntl.LOCK_UN)
        os.close(checkout_lock)
    assert not directory.exists()


def test_killed_owner_leaves_a_recognized_orphan_that_the_next_build_reaps(tmp_path):
    script = '''
import os,sys,time
from pathlib import Path
from mncs_forge.compile_workspace import workspace
with workspace(Path(sys.argv[1])) as directory:
    (directory / 'backend.json').write_text('artifact')
    print(directory,flush=True)
    time.sleep(30)
'''
    process = subprocess.Popen([sys.executable, "-c", script, str(tmp_path)],
                               stdout=subprocess.PIPE, text=True)
    try:
        directory = Path(process.stdout.readline().strip())
        assert directory.is_dir()
        process.kill()
        process.wait(timeout=5)
        with scratch.workspace(tmp_path):
            assert not directory.exists()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


def orphan(root):
    directory = root / (scratch.PREFIX + "fixture")
    directory.mkdir(mode=0o700)
    (directory / scratch.LEASE).write_text("")
    (directory / scratch.MARKER).write_text(json.dumps(scratch._owner(directory, root.stat())))
    (directory / "backend.json").write_text("artifact")
    return directory


def test_unmarked_symlink_replaced_and_extra_file_directories_are_preserved(tmp_path):
    unmarked = tmp_path / (scratch.PREFIX + "legacy")
    unmarked.mkdir()
    (unmarked / "backend.json").write_text("legacy")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep").write_text("keep")
    (tmp_path / (scratch.PREFIX + "link")).symlink_to(outside, target_is_directory=True)
    directory = orphan(tmp_path)
    (directory / "evidence.json").write_text("keep")
    assert scratch.prune_inactive(tmp_path) == 0
    (directory / "evidence.json").unlink()
    moved = tmp_path / "old"
    directory.rename(moved)
    directory.mkdir(mode=0o700)
    (directory / scratch.MARKER).write_bytes((moved / scratch.MARKER).read_bytes())
    (directory / scratch.LEASE).write_text("")
    assert scratch.prune_inactive(tmp_path) == 0
    assert unmarked.exists() and (outside / "keep").exists() and directory.exists()


def test_surviving_child_and_incomplete_process_view_preserve_workspace(tmp_path):
    directory = orphan(tmp_path)
    with mock.patch.object(scratch, "_in_use", return_value=True):
        assert scratch.prune_inactive(tmp_path) == 0
    assert directory.exists()
    with mock.patch.object(scratch, "_in_use", return_value=False):
        assert scratch.prune_inactive(tmp_path) == 1


def test_tracked_files_are_preserved_even_with_an_owner_shaped_marker(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    directory = orphan(tmp_path)
    subprocess.run(["git", "-C", str(tmp_path), "add", str(directory / "backend.json")], check=True)
    assert scratch.prune_inactive(tmp_path) == 0
    assert (directory / "backend.json").exists()
