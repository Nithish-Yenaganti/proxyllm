"""Consistent SQLite snapshots and non-destructive restore checks."""

import argparse
from contextlib import closing
from datetime import datetime, timezone
import os
from pathlib import Path
import sqlite3
import tempfile
import time
from uuid import uuid4

from auth.database import DATABASE_PATH

BACKUP_DIRECTORY = DATABASE_PATH.parent.parent / "backups"


def connect_readonly(path):
    return sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)


def checked_copy(source, destination):
    """Never overwrite a destination or implicitly create a missing source."""
    source, destination = Path(source), Path(destination)
    if not source.is_file():
        raise FileNotFoundError("Source database does not exist")
    fd = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    try:
        deadline = time.monotonic() + 60

        def progress(status, remaining, total):
            if time.monotonic() > deadline:
                raise TimeoutError("Backup exceeded sixty seconds")

        with closing(connect_readonly(source)) as original:
            with closing(sqlite3.connect(destination)) as copied:
                original.backup(copied, pages=256, progress=progress)
                if copied.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                    raise ValueError("Database integrity check failed")
                if copied.execute("PRAGMA foreign_key_check").fetchall():
                    raise ValueError("Database foreign-key check failed")
    except BaseException:
        destination.unlink(missing_ok=True)
        raise


def verify_restore(snapshot):
    """Compare all restored schema/data against the snapshot, not a changing live DB."""
    with tempfile.TemporaryDirectory(prefix="proxyllm-restore-") as directory:
        restored = Path(directory) / "restored.db"
        checked_copy(snapshot, restored)
        with closing(connect_readonly(snapshot)) as original:
            with closing(connect_readonly(restored)) as recovered:
                if list(original.iterdump()) != list(recovered.iterdump()):
                    raise ValueError("Restored schema or data differs from backup")


def create_backup(source=DATABASE_PATH, directory=BACKUP_DIRECTORY):
    directory = Path(directory)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if directory.is_symlink() or directory.stat().st_mode & 0o077:
        raise PermissionError("Backup directory must be private (mode 700)")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    staging = directory / (".pending-" + uuid4().hex + ".db")
    final = directory / ("gateway-" + stamp + "-" + uuid4().hex + ".db")
    try:
        checked_copy(source, staging)
        verify_restore(staging)
        staging.rename(final)
        return final
    finally:
        staging.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["create", "verify"])
    parser.add_argument("--snapshot", type=Path)
    args = parser.parse_args()
    if args.command == "verify":
        if args.snapshot is None:
            parser.error("verify requires --snapshot")
        verify_restore(args.snapshot)
        print("Restore check passed; original database untouched.")
    else:
        result = create_backup()
        print(f"Backup and restore check passed: {result.name}")


if __name__ == "__main__":
    main()
