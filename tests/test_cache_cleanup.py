from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from auth.cache_cleanup import cleanup, CACHE_REUSE_SECONDS
from api.main import CACHE_TTL_SECONDS


class CleanupCommandTests(unittest.TestCase):
    def test_preview_backup_delete_and_backup_failure(self):
        self.assertEqual(CACHE_REUSE_SECONDS, CACHE_TTL_SECONDS)
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / 'source.db'
            backups = Path(folder) / 'backups'
            with closing(sqlite3.connect(source)) as db:
                db.execute('CREATE TABLE response_cache (id INTEGER, created_at_unix REAL, expires_at_unix REAL)')
                db.executemany('INSERT INTO response_cache VALUES (?,?,?)', [(1,1200,4800),(2,1201,4801),(3,2500,2999)])
                db.execute('CREATE TABLE untouched (value TEXT)')
                db.execute("INSERT INTO untouched VALUES ('fixture')")
                db.commit()
            self.assertEqual(cleanup(source, backups, now=3000),(2,0))
            self.assertFalse(backups.exists())
            with patch('auth.cache_cleanup.create_backup', side_effect=OSError('test failure')):
                with self.assertRaises(OSError):
                    cleanup(source, backups, delete=True, now=3000)
            with closing(sqlite3.connect(source)) as db:
                self.assertEqual(db.execute('SELECT COUNT(*) FROM response_cache').fetchone()[0],3)
            self.assertEqual(cleanup(source, backups, delete=True, now=3000),(2,2))
            with closing(sqlite3.connect(next(backups.glob('*.db')))) as db:
                self.assertEqual(db.execute('SELECT COUNT(*) FROM response_cache').fetchone()[0],3)
            with closing(sqlite3.connect(source)) as db:
                self.assertEqual(db.execute('SELECT id FROM response_cache').fetchall(),[(2,)])
                self.assertEqual(db.execute('SELECT * FROM untouched').fetchall(),[('fixture',)])
            self.assertEqual(cleanup(source, backups, delete=True, now=3000),(0,0))
