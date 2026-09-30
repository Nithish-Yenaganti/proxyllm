"""Create private SQLite files without changing existing operator-owned files."""

import os
import stat
from pathlib import Path


def _check_file(path: Path) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
            or info.st_uid != os.getuid()):
        raise PermissionError(f"SQLite file must be a regular, singly linked file owned by this user: {path}")
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise PermissionError(
            f"SQLite file is accessible to other users: {path}. "
            "Stop all gateway and dashboard processes, then set the database and "
            "any -wal, -shm, and -journal files to mode 0600 before restarting."
        )


def prepare_private_database(database_path: Path, *, create: bool = True) -> Path:
    """Check existing storage and create a missing database with mode 0600.

    SQLite inherits the database mode when it creates journal, WAL and SHM files.
    The containing directory may be readable, but other users must not be able to
    replace its files. We never change the permissions of a source directory.
    """
    supplied = Path(database_path).absolute()
    path = supplied.parent.resolve() / supplied.name
    directory = path.parent.stat()
    if directory.st_uid != os.getuid() or stat.S_IMODE(directory.st_mode) & 0o022:
        raise PermissionError("SQLite directory must be owned by this user and not writable by other users")
    for candidate in (path, *(Path(str(path) + suffix) for suffix in ("-wal", "-shm", "-journal"))):
        _check_file(candidate)
    if not create:
        if not path.exists():
            raise FileNotFoundError(path)
        return path
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        _check_file(path)
    else:
        os.close(descriptor)
    return path
