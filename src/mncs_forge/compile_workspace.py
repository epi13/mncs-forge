"""Local compiler scratch lifetime, including recovery after owner death."""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
from typing import Iterator

PREFIX = ".mncs-native-artifact-"
MARKER = ".compile-owner.json"
LEASE = ".compile-lease"
SCHEMA = "mncs.forge.compile-workspace/1"
FILES = {MARKER, LEASE, "backend.json", "result.json"}


@contextmanager
def _root_lock(root: Path):
    # Directory locks coordinate aliases of the same local filesystem root
    # without leaving another coordination file in the user's project.
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield os.fstat(descriptor)
    finally:
        os.close(descriptor)


def _owner(directory: Path, parent) -> dict:
    metadata = directory.lstat()
    return {"schema_version": SCHEMA, "device": metadata.st_dev, "inode": metadata.st_ino,
            "parent_device": parent.st_dev, "parent_inode": parent.st_ino, "uid": os.getuid()}


def _in_use(name: str) -> bool:
    """Also retain an orphan compiler child whose parent no longer holds a lease."""
    if not Path("/proc").is_dir():
        return True
    for process in Path("/proc").iterdir():
        if not process.name.isdigit() or int(process.name) == os.getpid():
            continue
        try:
            if process.stat().st_uid != os.getuid():
                continue
            command = (process / "cmdline").read_bytes()
            # The random directory component is stable across Control mount
            # aliases; the compiler's fixed argv always names --output-dir.
            if name.encode() in command:
                return True
            try:
                links = [process / "cwd", process / "exe", *(process / "fd").iterdir()]
                for link in links:
                    try:
                        if name in os.readlink(link):
                            return True
                    except OSError:
                        pass
            except OSError:
                pass
        except FileNotFoundError:
            continue
        except OSError:
            # An unreadable command line cannot disprove a surviving writer.
            return True
    return False


def _prune(root: Path, parent) -> int:
    removed = 0
    for directory in root.glob(PREFIX + "*"):
        descriptor = None
        try:
            metadata = directory.lstat()
            if (not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid()
                    or metadata.st_mode & 0o077):
                continue
            marker_fd = os.open(directory / MARKER, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(marker_fd) as stream:
                if os.fstat(stream.fileno()).st_size > 4096 or json.load(stream) != _owner(directory, parent):
                    continue
            if any(child.name not in FILES or not stat.S_ISREG(child.lstat().st_mode)
                   for child in directory.iterdir()):
                continue
            tracked = subprocess.run(
                ["git", "-C", str(root), "ls-files", "-z", "--", str(directory)],
                capture_output=True, timeout=3,
            )
            if tracked.stdout or (tracked.returncode and any(
                (ancestor / ".git").exists() for ancestor in (root, *root.parents)
            )):
                continue
            descriptor = os.open(directory / LEASE, os.O_RDWR | os.O_NOFOLLOW)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                continue
            if _in_use(directory.name):
                continue
            shutil.rmtree(directory)
            removed += 1
        except (OSError, ValueError, subprocess.SubprocessError):
            continue
        finally:
            if descriptor is not None:
                os.close(descriptor)
    return removed


def prune_inactive(root: Path) -> int:
    with _root_lock(root) as parent:
        return _prune(root, parent)


@contextmanager
def workspace(root: Path) -> Iterator[Path]:
    temporary = None
    descriptor = None
    try:
        with _root_lock(root) as parent:
            _prune(root, parent)
            temporary = tempfile.TemporaryDirectory(prefix=PREFIX, dir=root)
            directory = Path(temporary.name)
            descriptor = os.open(directory / LEASE, os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            (directory / MARKER).write_text(json.dumps(_owner(directory, parent), sort_keys=True))
        yield directory
    finally:
        try:
            if temporary is not None:
                temporary.cleanup()
        finally:
            if descriptor is not None:
                os.close(descriptor)
