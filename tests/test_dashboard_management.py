"""Exercise real local management operations against isolated SQLite files."""
from contextlib import closing
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from auth.database import initialize_database, upsert_cached_response_record
from dashboard.app import create_app


class ManagementTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'test.db'
        self.backups = Path(self.temp.name) / 'backups'
        await initialize_database(self.path)
        self.app = create_app(self.path, self.backups)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(
            app=self.app, client=('127.0.0.1', 123)), base_url='http://localhost')
        self.addAsyncCleanup(self.client.aclose)
        await self.client.get('/')
        token = (await self.client.get('/session')).json()['csrf']
        self.headers = {'Origin': 'http://localhost', 'X-CSRF-Token': token}

    async def post(self, route, data):
        return await self.client.post('/admin/' + route, json=data, headers=self.headers)

    async def create_key(self):
        result = await self.post('keys', {'app_name': '<script>demo</script>', 'provider': 'anthropic'})
        self.assertEqual(result.status_code, 201, result.text)
        return result.json()

    async def test_key_lifecycle_secret_once_and_export(self):
        key = await self.create_key()
        self.assertTrue(key['secret'].startswith('nk_'))
        self.assertEqual((await self.post(f"keys/{key['id']}/grant", {'provider':'fireworks'})).status_code, 200)
        response = await self.client.get('/data')
        self.assertEqual(response.headers['cache-control'], 'no-store')
        self.assertNotIn(key['secret'], response.text)
        self.assertNotIn('key_hash', response.text)
        self.assertEqual(response.json()['keys'][0]['providers'], ['anthropic', 'fireworks'])
        usage = await self.client.get('/admin/usage', params={'key_id':key['id']})
        self.assertEqual(usage.json()[0]['requests'], 0)
        self.assertNotIn(key['secret'], usage.text)
        self.assertEqual((await self.post(f"keys/{key['id']}/revoke", {})).status_code, 400)
        self.assertEqual((await self.post(f"keys/{key['id']}/revoke", {'confirm':True})).status_code, 200)
        self.assertEqual((await self.post(f"keys/{key['id']}/grant", {'provider':'fireworks'})).status_code, 404)
        self.assertEqual((await self.post(f"keys/{key['id']}/revoke", {'confirm':True})).status_code, 404)
        self.assertEqual((await self.client.get('/data')).json()['keys'][0]['is_active'], 0)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM usage_logs').fetchone()[0], 0)
            self.assertNotIn(key['secret'], '\n'.join(db.iterdump()))

    async def test_write_guards_validation_and_no_side_effects(self):
        payload = {'app_name':'demo','provider':'anthropic'}
        for headers in ({}, {'Origin':'http://localhost'}, {'Origin':'https://evil.test','X-CSRF-Token':self.headers['X-CSRF-Token']}, {'Origin':'http://localhost','X-CSRF-Token':'wrong'}):
            self.assertEqual((await self.client.post('/admin/keys',json=payload,headers=headers)).status_code,403)
        self.assertEqual((await self.post('keys',dict(payload, provider='unknown'))).status_code,400)
        self.assertEqual((await self.post('keys',dict(payload, credential='private'))).status_code,400)
        self.assertEqual((await self.post('keys',dict(payload, app_name=' '))).status_code,400)
        self.assertEqual((await self.post('keys',dict(payload, api_key='do-not-echo'))).status_code,400)
        response = await self.post('keys',dict(payload,app_name='x'*17000))
        self.assertEqual(response.status_code,413)
        self.assertEqual((await self.client.get('/data')).json()['keys'],[])
        self.client.cookies.clear()
        self.assertEqual((await self.post('keys',payload)).status_code,401)
        self.assertEqual((await self.client.get('/admin/usage')).status_code,401)

    async def test_backups_verification_and_cleanup_failure(self):
        key = await self.create_key()
        await upsert_cached_response_record('test',key['id'],'anthropic','test',200,
            b'private answer','{}',0,0,1,self.path)
        preview = await self.client.get('/admin/cache')
        self.assertEqual(preview.json(), {'expired':1})
        self.assertFalse(self.backups.exists())
        self.assertEqual((await self.post('cache/cleanup',{})).status_code,400)
        with patch('auth.cache_cleanup.create_backup', side_effect=OSError('secret-path')):
            failed = await self.post('cache/cleanup',{'confirm':True})
        self.assertEqual(failed.status_code,503)
        self.assertNotIn('secret-path',failed.text)
        self.assertEqual((await self.client.get('/admin/cache')).json()['expired'],1)
        result = await self.post('cache/cleanup',{'confirm':True})
        self.assertEqual(result.json()['deleted'],1)
        snapshots = (await self.client.get('/admin/backups')).json()['snapshots']
        self.assertEqual(len(snapshots),1)
        self.assertEqual((await self.post('backups/verify',{'snapshot':snapshots[0]})).status_code,200)
        self.assertEqual((await self.post('backups/verify',{'snapshot':'../test.db'})).status_code,400)
        (self.backups/'link.db').symlink_to(self.path)
        self.assertEqual((await self.post('backups/verify',{'snapshot':'link.db'})).status_code,400)
        self.assertEqual((await self.post('backups',{'kind':'hourly','confirm':True})).status_code,200)
        self.assertEqual((await self.post('backups',{'kind':'maintenance','confirm':True})).status_code,200)
        with closing(sqlite3.connect(self.backups/snapshots[0])) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM response_cache').fetchone()[0],1)
        self.assertNotIn('private answer',preview.text)

    async def test_missing_database_is_not_created_by_write(self):
        self.path.unlink()
        self.assertEqual((await self.post('keys',{'app_name':'demo','provider':'anthropic'})).status_code,503)
        self.assertFalse(self.path.exists())
