from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
import tempfile
import unittest

from auth.backup import create_backup, hourly_retention


class RetentionTests(unittest.TestCase):
    def fixtures(self, folder):
        now = datetime.now(timezone.utc).astimezone().replace(hour=18, minute=0, second=0, microsecond=0)
        files = []
        for index, hours in enumerate([0,1,2,3,24,25,48]):
            timestamp = (now-timedelta(hours=hours)).astimezone(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
            path = Path(folder) / f'hourly-{timestamp}-{index:032x}.db'
            path.touch()
            files.append(path)
        return now, files

    def test_keep_three_and_prior_day_preserve_other_files(self):
        with tempfile.TemporaryDirectory() as folder:
            now, files = self.fixtures(folder)
            for name in ['gateway-legacy.db','maintenance-protected.db','hourly-invalid.db']:
                (Path(folder)/name).touch()
            with patch('auth.backup.verify_restore') as verify:
                removed = hourly_retention(folder, now)
            self.assertEqual(verify.call_count,4)
            self.assertEqual(len(removed),3)
            self.assertEqual([p.exists() for p in files],[True,True,True,False,True,False,False])
            self.assertTrue((Path(folder)/'maintenance-protected.db').exists())
            self.assertTrue((Path(folder)/'gateway-legacy.db').exists())

    def test_failed_verification_deletes_nothing(self):
        with tempfile.TemporaryDirectory() as folder:
            now, files = self.fixtures(folder)
            with patch('auth.backup.verify_restore', side_effect=ValueError('corrupt')):
                with self.assertRaises(ValueError):
                    hourly_retention(folder,now)
            self.assertTrue(all(p.exists() for p in files))

    def test_new_backup_failure_never_prunes(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch('auth.backup._create_snapshot', side_effect=OSError('failed')), patch('auth.backup.hourly_retention') as prune:
                with self.assertRaises(OSError):
                    create_backup(Path(folder)/'source.db',Path(folder)/'backups',kind='hourly')
                prune.assert_not_called()

    def test_missing_yesterday_keeps_older_fallback(self):
        with tempfile.TemporaryDirectory() as folder:
            now, files = self.fixtures(folder)
            files[4].unlink()
            files[5].unlink()
            with patch('auth.backup.verify_restore'):
                hourly_retention(folder,now)
            self.assertTrue(files[6].exists())
