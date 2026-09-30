"""Preview expired cache rows; delete only with an explicit flag and verified backup."""
import argparse
from contextlib import closing
import sqlite3
from time import time

from auth.backup import BACKUP_DIRECTORY, connect_readonly, create_backup
from auth.database import DATABASE_PATH
from auth.storage_security import prepare_private_database

# Must match the gateway reuse policy; regression test checks this agreement.
CACHE_REUSE_SECONDS = 1800
EXPIRED = "expires_at_unix <= ? OR created_at_unix <= ?"


def cleanup(source=DATABASE_PATH, backup_directory=BACKUP_DIRECTORY, *, delete=False, now=None):
    cutoff = time() if now is None else now
    parameters = (cutoff, cutoff - CACHE_REUSE_SECONDS)
    with closing(connect_readonly(source)) as db:
        count = db.execute("SELECT COUNT(*) FROM response_cache WHERE " + EXPIRED, parameters).fetchone()[0]
    if not delete or not count:
        return count, 0
    # Failure here prevents any deletion. The live database is never restored over.
    create_backup(source, backup_directory)
    source = prepare_private_database(source, create=False)
    # mode=rw prevents an absent source from becoming a newly created database.
    with closing(sqlite3.connect(source.resolve().as_uri() + "?mode=rw", uri=True, timeout=10)) as db:
        with db:
            db.execute("BEGIN IMMEDIATE")
            removed = db.execute("DELETE FROM response_cache WHERE " + EXPIRED, parameters).rowcount
    return count, removed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--delete", action="store_true", help="Back up first, then delete expired rows.")
    args = parser.parse_args()
    try:
        count, removed = cleanup(delete=args.delete)
    except (OSError, sqlite3.Error, ValueError, TimeoutError):
        parser.exit(1, "Cleanup failed; no successful deletion is confirmed. Check database access and backup verification.\n")
    print(f"Expired rows at preview: {count}; deleted: {removed}.")
    if not args.delete:
        print("Preview only. Use --delete to back up and remove expired rows.")


if __name__ == "__main__":
    main()
