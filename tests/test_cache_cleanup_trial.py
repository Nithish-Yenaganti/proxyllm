"""Isolated cleanup proposal only; no production cleanup command is installed."""
from contextlib import closing
import sqlite3
import tempfile
import unittest
from pathlib import Path

from auth import database
from api.main import CACHE_TTL_SECONDS


class CacheCleanupTrial(unittest.TestCase):
    def test_expiry_selection_preserves_valid_cache_and_other_tables(self):
        with tempfile.TemporaryDirectory(prefix="cache-cleanup-trial-") as folder:
            with closing(sqlite3.connect(Path(folder) / "test.db")) as db:
                db.execute("PRAGMA foreign_keys=ON")
                for schema in (
                    database.CREATE_VIRTUAL_KEYS_TABLE,
                    database.CREATE_PROVIDER_PERMISSIONS_TABLE,
                    database.CREATE_USAGE_LOGS_TABLE,
                    database.CREATE_RATE_LIMIT_EVENTS_TABLE,
                    database.CREATE_RESPONSE_CACHE_TABLE,
                ):
                    db.execute(schema)
                db.execute("INSERT INTO virtual_keys (id,app_name,key_prefix,key_hash,provider,provider_credential) VALUES (1,'fixture','test','not-a-real-key','fireworks','default')")
                db.execute("INSERT INTO virtual_key_provider_permissions VALUES (1,'fireworks','default')")
                db.execute("INSERT INTO usage_logs (virtual_key_id,latency_ms,status,status_code) VALUES (1,10,'success',200)")
                db.execute("INSERT INTO rate_limit_events (virtual_key_id,accepted_at_unix) VALUES (1,2999)")
                # Stored expiry, current-policy boundary, legacy one-hour expiry, still valid.
                entries = [('expired',2500,2999), ('boundary',1200,4800),
                           ('legacy',1100,4700), ('valid',1201,4801)]
                for name, created, expires in entries:
                    db.execute("INSERT INTO response_cache VALUES (?,1,'fireworks','fixture',200,?, '{}',0,?,?)",
                               (name,b'{"fixture":true}',created,expires))
                db.commit()
                tables = ('virtual_keys','virtual_key_provider_permissions','usage_logs','rate_limit_events')
                before = {t: db.execute(f'SELECT * FROM {t}').fetchall() for t in tables}
                valid = db.execute("SELECT * FROM response_cache WHERE cache_key='valid'").fetchall()
                predicate = 'expires_at_unix <= ? OR created_at_unix <= ?'
                parameters = (3000,3000-CACHE_TTL_SECONDS)
                preview = db.execute('SELECT cache_key FROM response_cache WHERE '+predicate,parameters).fetchall()
                self.assertEqual({r[0] for r in preview},{'expired','boundary','legacy'})
                self.assertEqual(db.execute('SELECT COUNT(*) FROM response_cache').fetchone()[0],4)
                db.execute('BEGIN')
                self.assertEqual(db.execute('DELETE FROM response_cache WHERE '+predicate,parameters).rowcount,3)
                self.assertEqual(db.execute('SELECT * FROM response_cache').fetchall(),valid)
                self.assertEqual(before,{t: db.execute(f'SELECT * FROM {t}').fetchall() for t in tables})
                db.rollback()
                self.assertEqual(db.execute('SELECT COUNT(*) FROM response_cache').fetchone()[0],4)
                db.execute('DELETE FROM response_cache WHERE '+predicate,parameters)
                db.commit()
                self.assertEqual(db.execute('DELETE FROM response_cache WHERE '+predicate,parameters).rowcount,0)
                self.assertEqual(db.execute('PRAGMA integrity_check').fetchall(),[('ok',)])
                self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(),[])
