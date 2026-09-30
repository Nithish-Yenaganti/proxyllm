"""Local administrative endpoints; reuse CLI operations with explicit storage paths."""
import re
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from api.request_body import read_json_body, RequestTooLarge, RequestBodyTimeout
from auth.backup import create_backup, verify_restore
from auth.cache_cleanup import cleanup
from auth.database import (create_virtual_key_record, grant_provider_permission,
                           revoke_virtual_key_record, summarize_usage)
from auth.keys import generate_virtual_key, get_key_prefix, hash_virtual_key


class Input(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)


class Permission(Input):
    provider: Literal['anthropic', 'fireworks']
    credential: Literal['default'] = 'default'


class NewKey(Permission):
    app_name: str = Field(min_length=1, max_length=100)


class Confirmation(Input):
    confirm: Literal[True]


class Backup(Input):
    kind: Literal['maintenance', 'hourly'] = 'maintenance'
    confirm: Literal[True]


class Verify(Input):
    snapshot: str = Field(min_length=1, max_length=200)


async def body(request, model):
    try:
        value = await read_json_body(request, 16384)
        return model.model_validate(value, strict=True)
    except RequestBodyTimeout:
        raise HTTPException(408, 'Request body did not arrive before the deadline.') from None
    except RequestTooLarge:
        raise HTTPException(413, 'Request exceeds 16 KB.') from None
    except (ValueError, RecursionError):
        # Never echo submitted content or secrets in validation responses.
        raise HTTPException(400, 'Invalid fields. Check the form and try again.') from None


def backup_entries(directory):
    if not directory.exists():
        return []
    if directory.is_symlink():
        raise ValueError('Invalid backup directory')
    return sorted(p.name for p in directory.iterdir()
                  if p.suffix == '.db' and not p.name.startswith('.')
                  and p.is_file() and not p.is_symlink())


def management_router(path, backup_directory):
    path, directory = Path(path), Path(backup_directory)
    router = APIRouter(prefix='/admin')

    @router.post('/keys', status_code=201)
    async def create(request: Request):
        data = await body(request, NewKey)
        secret = generate_virtual_key()
        key_id = await create_virtual_key_record(data.app_name, get_key_prefix(secret),
            hash_virtual_key(secret), data.provider, data.credential, path)
        return {'id': key_id, 'secret': secret}

    @router.post('/keys/{key_id}/grant')
    async def grant(key_id: int, request: Request):
        data = await body(request, Permission)
        if not await grant_provider_permission(key_id, data.provider, data.credential, path):
            raise HTTPException(404, 'Active key not found.')
        return {'message': 'Provider access granted.'}

    @router.post('/keys/{key_id}/revoke')
    async def revoke(key_id: int, request: Request):
        await body(request, Confirmation)
        if not await revoke_virtual_key_record(key_id, path):
            raise HTTPException(404, 'Active key not found.')
        return {'message': 'Key revoked. Usage records retained.'}

    @router.get('/usage')
    async def usage(key_id: int | None = None):
        return await summarize_usage(key_id, path)

    @router.get('/backups')
    def backups():
        return {'snapshots': backup_entries(directory)}

    @router.post('/backups')
    async def backup(request: Request):
        data = await body(request, Backup)
        result = await run_in_threadpool(create_backup, path, directory, kind=data.kind)
        return {'message': 'Backup created and verified.', 'snapshot': result.name}

    @router.post('/backups/verify')
    async def verify(request: Request):
        data = await body(request, Verify)
        # Select only existing regular files in the configured backup directory.
        if not re.fullmatch(r'[A-Za-z0-9_.-]+\.db', data.snapshot) or data.snapshot not in backup_entries(directory):
            raise HTTPException(400, 'Select an existing snapshot.')
        await run_in_threadpool(verify_restore, directory / data.snapshot)
        return {'message': 'Verification passed. Live database unchanged.'}

    @router.get('/cache')
    def preview():
        count, _ = cleanup(path, directory)
        return {'expired': count}

    @router.post('/cache/cleanup')
    async def remove(request: Request):
        await body(request, Confirmation)
        count, removed = await run_in_threadpool(cleanup, path, directory, delete=True)
        return {'message': f'Deleted {removed} expired entries.', 'expired': count, 'deleted': removed}

    return router
