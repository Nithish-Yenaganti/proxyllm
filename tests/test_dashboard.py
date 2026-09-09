import tempfile
import unittest
from pathlib import Path
import httpx
from auth.database import initialize_database, create_virtual_key_record
from dashboard.app import create_app


class DashboardTests(unittest.IsolatedAsyncioTestCase):
    async def test_local_session_and_safe_fields(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'test.db'
            await initialize_database(path)
            await create_virtual_key_record('<script>alert(1)</script>', 'secret-prefix',
                                            'secret-hash', 'fireworks', 'default', path)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(path),
                    client=('127.0.0.1',123)), base_url='http://127.0.0.1:8001') as client:
                self.assertEqual((await client.get('/data')).status_code,401)
                page=await client.get('/')
                self.assertEqual(page.status_code,200)
                response=await client.get('/data')
                self.assertEqual(response.status_code,200)
                self.assertEqual(response.json()['totals']['requests'],0)
                self.assertNotIn('secret-hash',response.text)
                self.assertNotIn('secret-prefix',response.text)
                self.assertEqual((await client.post('/data')).status_code,405)
                self.assertEqual((await client.get('/data',headers={'Origin':'https://evil.test'})).status_code,403)
                self.assertEqual((await client.get('/',headers={'Host':'evil.test'})).status_code,400)

    async def test_missing_database_and_remote_access(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'missing.db'
            app=create_app(path)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app,
                    client=('127.0.0.1',123)),base_url='http://localhost') as client:
                await client.get('/')
                self.assertEqual((await client.get('/data')).status_code,503)
                self.assertFalse(path.exists())
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app,
                    client=('192.0.2.1',123)),base_url='http://localhost') as client:
                self.assertEqual((await client.get('/')).status_code,403)
