import sqlite3
from contextlib import closing
import tempfile
import unittest
from pathlib import Path

from auth.backup import checked_copy, create_backup, verify_restore


class BackupTests(unittest.TestCase):
    def test_wal_snapshot_and_restore_preserve_data(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "live.db"
            with closing(sqlite3.connect(source)) as db:
                db.execute("PRAGMA journal_mode=WAL")
                db.execute("CREATE TABLE records (id INTEGER PRIMARY KEY, value TEXT)")
                db.execute("INSERT INTO records VALUES (1, 'test-only')")
                db.commit()
                snapshot = create_backup(source, Path(folder) / "backups")
                self.assertEqual(snapshot.stat().st_mode & 0o777, 0o600)
                db.execute("INSERT INTO records VALUES (2, 'later')")
                db.commit()
                verify_restore(snapshot)
                with closing(sqlite3.connect(snapshot)) as saved:
                    self.assertEqual(saved.execute("SELECT count(*) FROM records").fetchone()[0], 1)
                self.assertEqual(db.execute("SELECT count(*) FROM records").fetchone()[0], 2)

    def test_missing_source_does_not_create_database(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "missing.db"
            with self.assertRaises(FileNotFoundError):
                checked_copy(source, Path(folder) / "copy.db")
            self.assertFalse(source.exists())

    def test_restore_never_overwrites_existing_file(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "live.db"
            with closing(sqlite3.connect(source)) as db:
                db.execute("CREATE TABLE records (id INTEGER)")
            with self.assertRaises(FileExistsError):
                checked_copy(source, source)

    def test_corrupt_source_leaves_no_completed_snapshot(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "bad.db"
            source.write_bytes(b"not a database")
            destination = Path(folder) / "copy.db"
            with self.assertRaises(sqlite3.DatabaseError):
                checked_copy(source, destination)
            self.assertFalse(destination.exists())
