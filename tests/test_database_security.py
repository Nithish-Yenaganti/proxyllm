"""Keep SQLite contents private without repairing existing files silently."""

import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from auth.database import initialize_database, open_database
from auth.storage_security import prepare_private_database
from auth.backup import connect_readonly
from auth.cache_cleanup import cleanup
from auth.rate_limit import consume_rate_limit
from dashboard.app import snapshot


class DatabaseSecurityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = self.enterContext(tempfile.TemporaryDirectory())
        self.path = Path(self.directory) / 'gateway.db'

    async def test_new_database_and_live_sidecars_are_private_under_normal_umask(self):
        Path(self.directory).chmod(0o755)
        previous = os.umask(0o022)
        try:
            await initialize_database(self.path)
            async with open_database(self.path) as database:
                await database.execute('INSERT INTO virtual_keys (app_name, key_prefix, key_hash, provider, provider_credential) VALUES (?, ?, ?, ?, ?)', ('fixture', 'fixture', 'fixture', 'anthropic', 'default'))
                await database.commit()
                for path in (self.path, Path(str(self.path) + '-wal'), Path(str(self.path) + '-shm')):
                    self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(Path(self.directory).stat().st_mode), 0o755)
        finally:
            os.umask(previous)

    async def test_unsafe_existing_database_is_rejected_without_changes(self):
        self.path.write_bytes(b'unchanged fixture')
        self.path.chmod(0o644)
        with self.assertRaisesRegex(PermissionError, '0600'):
            await initialize_database(self.path)
        self.assertEqual(self.path.read_bytes(), b'unchanged fixture')
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o644)

    async def test_unsafe_sidecars_are_rejected_before_open(self):
        await initialize_database(self.path)
        for suffix in ('-wal', '-shm', '-journal'):
            with self.subTest(suffix=suffix):
                sidecar = Path(str(self.path) + suffix)
                sidecar.write_bytes(b'unchanged fixture')
                sidecar.chmod(0o644)
                with self.assertRaises(PermissionError):
                    async with open_database(self.path):
                        self.fail('Unsafe sidecar was opened')
                self.assertEqual(sidecar.read_bytes(), b'unchanged fixture')
                sidecar.unlink()

    async def test_symlink_and_hardlink_are_rejected(self):
        target = Path(self.directory) / 'target.db'
        target.touch(mode=0o600)
        self.path.symlink_to(target)
        with self.assertRaises(PermissionError):
            await initialize_database(self.path)
        self.path.unlink()
        os.link(target, self.path)
        with self.assertRaises(PermissionError):
            await initialize_database(self.path)
        self.assertEqual(target.read_bytes(), b'')

    async def test_foreign_owner_and_shared_writable_directory_are_rejected(self):
        self.path.touch(mode=0o600)
        with patch('auth.storage_security.os.getuid', return_value=os.getuid() + 1):
            with self.assertRaises(PermissionError):
                await initialize_database(self.path)
        Path(self.directory).chmod(0o777)
        try:
            with self.assertRaises(PermissionError):
                await initialize_database(self.path)
        finally:
            Path(self.directory).chmod(0o700)

    async def test_runtime_open_does_not_recreate_missing_database(self):
        with self.assertRaises(FileNotFoundError):
            async with open_database(self.path):
                self.fail('Missing database was opened')
        self.assertFalse(self.path.exists())

    async def test_reporting_maintenance_and_limiter_reject_unsafe_storage(self):
        await initialize_database(self.path)
        self.path.chmod(0o644)
        for operation in (snapshot, connect_readonly, cleanup):
            with self.subTest(operation=operation.__name__):
                with self.assertRaises(PermissionError):
                    operation(self.path)
        with self.assertRaises(PermissionError):
            await consume_rate_limit(1, self.path)

    async def test_sidecar_symlink_and_nonregular_database_are_rejected(self):
        self.path.mkdir()
        with self.assertRaises(PermissionError):
            prepare_private_database(self.path)
        self.path.rmdir()
        self.path.touch(mode=0o600)
        target = Path(self.directory) / 'other-file'
        target.touch(mode=0o600)
        Path(str(self.path) + '-wal').symlink_to(target)
        with self.assertRaises(PermissionError):
            prepare_private_database(self.path)
