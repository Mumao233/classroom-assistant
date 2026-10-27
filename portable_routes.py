"""Loopback package routes; ZIP bodies are streamed to bounded local staging."""
from __future__ import annotations

import os
import uuid

from fastapi import Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, StrictBool
from starlette.concurrency import run_in_threadpool

from portable_package import MAX_PACKAGE_BYTES, PortableStore, fail


class ExportPreviewInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    session_id: str
    include_audio: StrictBool = False


class PackageActionInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    preview_id: str
    request_id: str
    allow_missing: StrictBool = False


def register_portable_routes(app, room):
    manager = PortableStore(room)
    app.state.portable = manager

    @app.post('/api/packages/preview')
    def preview_export(payload: ExportPreviewInput):
        return manager.preview_export(payload.session_id, payload.include_audio)

    @app.post('/api/packages/export')
    def build_export(payload: PackageActionInput):
        return manager.build_export(payload.preview_id, request_id=payload.request_id,
                                    allow_missing=payload.allow_missing)

    @app.get('/api/packages/exports/{request_id}')
    def lookup_export(request_id: str, session_id: str):
        return manager.lookup_export(request_id, session_id)

    @app.get('/api/packages/download/{package_id}')
    def download(package_id: str):
        path = manager.download_path(package_id)
        return FileResponse(path, media_type='application/zip', filename=f'classroom-{package_id[:12]}.zip',
                            headers={'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'})

    @app.post('/api/packages/import/preview')
    async def preview_import(request: Request, request_id: str):
        manager._key(request_id)
        declared = request.headers.get('content-length')
        if declared is not None:
            try:
                if int(declared) < 0:
                    fail('上传长度无效。', 400)
                if int(declared) > MAX_PACKAGE_BYTES:
                    fail('课堂包超过 2 GiB 上传上限。', 413)
            except ValueError:
                fail('上传长度无效。', 400)
        path = manager.root / 'uploads' / f'{uuid.uuid4().hex}.upload'
        size = 0
        try:
            with path.open('xb') as output:
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > MAX_PACKAGE_BYTES:
                        fail('课堂包超过 2 GiB 上传上限；未恢复任何课堂。', 413)
                    await run_in_threadpool(output.write, chunk)
                if not size:
                    fail('上传文件为空。')
                await run_in_threadpool(output.flush)
                await run_in_threadpool(os.fsync, output.fileno())
            return await run_in_threadpool(manager.preview_import, path, request_id=request_id)
        except OSError:
            fail('本机暂存写入失败；请检查磁盘空间，原课堂未改动。', 507)
        finally:
            path.unlink(missing_ok=True)

    @app.post('/api/packages/import')
    def restore(payload: PackageActionInput):
        return manager.restore(payload.preview_id, request_id=payload.request_id,
                               allow_missing=payload.allow_missing)

    @app.get('/api/packages/imports/{request_id}')
    def lookup_import(request_id: str):
        return manager.lookup_import(request_id)

    return manager
