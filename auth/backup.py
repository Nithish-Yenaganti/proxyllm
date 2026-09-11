"""Consistent SQLite snapshots and non-destructive restore checks."""

import argparse
from contextlib import closing
from datetime import datetime, timezone
import fcntl
import os
import re
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


def hourly_retention(directory, now=None):
    """Keep three hourly snapshots plus the latest from before today's local date.

    The prior-day slot keeps an older fallback when yesterday had no successful run.
    Only our exact hourly filenames are eligible; legacy/maintenance files are untouched.
    Caller holds the directory lock and has just verified a new snapshot.
    """
    today = (now or datetime.now(timezone.utc)).astimezone().date()
    candidates = []
    pattern = re.compile(r"hourly-(\d{8}T\d{6}Z)-[0-9a-f]{32}\.db")
    for path in Path(directory).iterdir():
        match = pattern.fullmatch(path.name)
        if not match or path.is_symlink() or not path.is_file():
            continue
        try:
            created = datetime.strptime(match[1], "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        candidates.append((created, path))
    candidates.sort(key=lambda item: (item[0], item[1].name), reverse=True)
    kept = {path for _, path in candidates[:3]}
    prior = next((path for created, path in candidates if created.astimezone().date() < today), None)
    if prior is not None:
        kept.add(prior)
    # Verify every recovery point to be retained before removing any older copy.
    for path in kept:
        verify_restore(path)
    removed = []
    for _, path in candidates:
        if path not in kept:
            path.unlink()
            removed.append(path.name)
    return removed


def create_backup(source=DATABASE_PATH, directory=BACKUP_DIRECTORY, *, kind="maintenance"):
    directory = Path(directory)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if directory.is_symlink() or directory.stat().st_mode & 0o077:
        raise PermissionError("Backup directory must be private (mode 700)")
    if kind not in {"hourly", "maintenance"}:
        raise ValueError("Unknown backup kind")
    # Serialize snapshots/pruning and refuse symlink lock-file targets.
    lock_fd = os.open(directory / '.backup.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(lock_fd, 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = _create_snapshot(source, directory, kind)
        if kind == "hourly":
            removed = hourly_retention(directory)
            if removed:
                print(f"Retention removed {len(removed)} older hourly backup(s); retained copies verified.")
        return result


def _create_snapshot(source, directory, kind):
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    staging = directory / (".pending-" + uuid4().hex + ".db")
    final = directory / (kind + "-" + stamp + "-" + uuid4().hex + ".db")
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
    parser.add_argument("--kind", choices=["hourly", "maintenance"], default="maintenance")
    args = parser.parse_args()
    if args.command == "verify":
        if args.snapshot is None:
            parser.error("verify requires --snapshot")
        verify_restore(args.snapshot)
        print("Restore check passed; original database untouched.")
    else:
        result = create_backup(kind=args.kind)
        print(f"Backup and restore check passed: {result.name}")


if __name__ == "__main__":
    main()
