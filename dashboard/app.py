"""Run with python -m dashboard.app. Never expose through a public reverse proxy."""
import secrets
import sqlite3
from contextlib import closing
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware

from auth.database import DATABASE_PATH
from auth.backup import BACKUP_DIRECTORY
from dashboard.management import management_router


def snapshot(path):
    # Read-only SQLite connection: no migrations, key writes, or cache content reads.
    with closing(sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=3)) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN")
        totals = dict(db.execute("""SELECT COUNT(*) requests,
            COALESCE(SUM(total_tokens),0) tokens,
            COALESCE(SUM(estimated_cost_usd),0) cost,
            COALESCE(SUM(cost_avoided_usd),0) avoided,
            COALESCE(SUM(cache_status='hit'),0) hits,
            COALESCE(SUM(status_code>=400),0) errors,
            AVG(latency_ms) latency FROM usage_logs""").fetchone())
        keys = [dict(r) for r in db.execute("""SELECT k.id, k.app_name, k.is_active,
            COUNT(u.id) requests, COALESCE(SUM(u.total_tokens),0) tokens,
            COALESCE(SUM(u.estimated_cost_usd),0) cost
            FROM virtual_keys k LEFT JOIN usage_logs u ON u.virtual_key_id=k.id
            GROUP BY k.id ORDER BY k.id""")]
        for key in keys:
            key['providers'] = [r[0] for r in db.execute(
                'SELECT provider FROM virtual_key_provider_permissions WHERE key_id=? ORDER BY provider', (key['id'],))]
        recent = [dict(r) for r in db.execute("""SELECT virtual_key_id, provider,
            status_code, cache_status, total_tokens, latency_ms, created_at
            FROM usage_logs ORDER BY id DESC LIMIT 30""")]
    return {'totals': totals, 'keys': keys, 'recent': recent}


def create_app(path=DATABASE_PATH, backup_directory=BACKUP_DIRECTORY):
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    token = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(32)
    app.include_router(management_router(path, backup_directory))
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=['127.0.0.1', 'localhost', '[::1]'])

    @app.middleware('http')
    async def protect(request: Request, call_next):
        if not request.client or request.client.host not in {'127.0.0.1', '::1'}:
            return JSONResponse({'error': 'Local access only'}, status_code=403)
        if request.headers.get('sec-fetch-site') == 'cross-site':
            return JSONResponse({'error': 'Cross-site access denied'}, status_code=403)
        origin = request.headers.get('origin')
        if origin and origin != str(request.base_url).rstrip('/'):
            return JSONResponse({'error': 'Origin denied'}, status_code=403)
        if (request.url.path in {'/data', '/session'} or request.url.path.startswith('/admin/')) and not secrets.compare_digest(request.cookies.get('dashboard_session', ''), token):
            return JSONResponse({'error': 'Open the dashboard first'}, status_code=401)
        if request.url.path.startswith('/admin/'):
            if request.method != 'GET':
                if (origin != str(request.base_url).rstrip('/') or
                        not secrets.compare_digest(request.headers.get('x-csrf-token', ''), csrf)):
                    return JSONResponse({'error': 'Refresh the dashboard and retry.'}, status_code=403)
                if request.headers.get('content-type', '').split(';')[0] != 'application/json':
                    return JSONResponse({'error': 'JSON required'}, status_code=415)
            if not Path(path).is_file():
                return JSONResponse({'error': 'Database missing. Initialize the gateway first.'}, status_code=503)
        try:
            response = await call_next(request)
        except (sqlite3.Error, OSError, ValueError, TimeoutError):
            response = JSONResponse({'error': 'Operation failed. Check local database and backup access, then refresh before retrying.'}, status_code=503)
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
        return response

    @app.get('/session')
    def session():
        return {'csrf': csrf}

    @app.get('/')
    def index():
        response = FileResponse(Path(__file__).with_name('index.html'))
        response.set_cookie('dashboard_session', token, httponly=True, samesite='strict')
        return response

    @app.get('/app.js')
    def javascript():
        return FileResponse(Path(__file__).with_name('app.js'), media_type='text/javascript')

    @app.get('/style.css')
    def css():
        return FileResponse(Path(__file__).with_name('style.css'), media_type='text/css')

    @app.get('/data')
    def data():
        try:
            return snapshot(path)
        except sqlite3.Error:
            return JSONResponse({'error': 'Database unavailable or schema not ready. Initialize the proxy database and retry.'}, status_code=503)
    return app


if __name__ == '__main__':
    import uvicorn
    uvicorn.run(create_app(), host='127.0.0.1', port=8001, proxy_headers=False)
