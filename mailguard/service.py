"""Authenticated, loopback-only HTTP adapter; inference stays in the expert core."""
import base64
import binascii
import hmac
import os
from pathlib import Path
import secrets
from threading import BoundedSemaphore
from typing import Literal

from fastapi import Depends, FastAPI, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from .config import provider_concurrency
from .expert import Expert, MAX_MAIL_BYTES
from .models import AnalysisError


class PrepareInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    sample: Literal['m002', 'm026'] | None = None
    eml_base64: str | None = Field(default=None, max_length=7*1024*1024)
    context: dict = Field(default_factory=dict)
    profile: Literal['v6-c1', 'v6'] = 'v6-c1'


class SubmitInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    request_hash: str = Field(min_length=64, max_length=64)
    response: dict
    host: str = Field(default='workbuddy', min_length=1, max_length=100)
    model: str = Field(default='not_reported', min_length=1, max_length=100)


class AnalyzeInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    provider: Literal['deepseek', 'glm', 'gpt'] = 'deepseek'


class ReviewInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    verdict: Literal['risk', 'clear', 'review']
    note: str = Field(min_length=1, max_length=4000)


class ImapInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    limit: int = Field(default=3, ge=1, le=50, strict=True)
    context: dict = Field(default_factory=dict)


def service_token(state):
    path = Path(state)/'service.token'
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        token = path.read_text(encoding='utf-8').strip()
    else:
        token = secrets.token_urlsafe(32)
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write(token+'\n')
    if len(token) < 32:
        raise AnalysisError('本机服务令牌文件无效，请在本机重新生成。', code='invalid_config')
    path.chmod(0o600)
    return token


def create_app(*, expert=None, token=None, assessor=None, limits=None):
    expert = expert or Expert()
    token = token or service_token(expert.state)
    limits = limits or {p: provider_concurrency(p) for p in ('deepseek', 'glm', 'gpt')}
    if any(type(v) is not int or not 1 <= v <= 20 for v in limits.values()):
        raise AnalysisError('服务并发数须为1至20。', code='invalid_config')
    slots = {p: BoundedSemaphore(v) for p, v in limits.items()}

    app = FastAPI(title='MailGuard Expert', version='0.1.0', docs_url=None, redoc_url=None, openapi_url=None)
    app.state.expert = expert

    async def authenticate(authorization: str | None = Header(default=None)):
        supplied = authorization[7:] if authorization and authorization.startswith('Bearer ') else ''
        if not hmac.compare_digest(supplied.encode(), token.encode()):
            raise AnalysisError('本机API需要有效Bearer令牌。', code='unauthorized')

    @app.middleware('http')
    async def limit_body(request: Request, call_next):
        # Also bound chunked requests; do not rely solely on Content-Length.
        chunks, size = [], 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > 8*1024*1024:
                return JSONResponse({'status': 'error', 'error_code': 'request_too_large'}, status_code=413)
            chunks.append(chunk)
        request._body = b''.join(chunks)
        return await call_next(request)

    @app.exception_handler(AnalysisError)
    async def known_error(request, exc):
        status = {'unauthorized': 401, 'job_not_found': 404, 'job_conflict': 409,
                  'provider_busy': 429}.get(exc.code, 400)
        return JSONResponse({'status': 'error', 'error_code': exc.code, 'error': str(exc)}, status_code=status)

    @app.get('/health')
    def health():
        return {'status': 'ok', 'service': 'mailguard-expert', 'version': '0.1.0'}

    @app.get('/v1/capabilities', dependencies=[Depends(authenticate)])
    def capabilities():
        return {**expert.doctor(), 'concurrency_per_process': limits,
                'default_profile': 'v6-c1', 'readonly_imap': True}

    @app.get('/v1/openapi.json', dependencies=[Depends(authenticate)])
    def openapi():
        return app.openapi()

    @app.post('/v1/prepare', dependencies=[Depends(authenticate)])
    def prepare(value: PrepareInput):
        raw = None
        if value.eml_base64 is not None:
            try:
                raw = base64.b64decode(value.eml_base64, validate=True)
            except (ValueError, binascii.Error):
                raise AnalysisError('邮件Base64无效。', code='invalid_source') from None
            if len(raw) > MAX_MAIL_BYTES:
                raise AnalysisError('邮件超过5MiB。', code='request_too_large')
        return expert.prepare(raw=raw, sample=value.sample, context=value.context, profile=value.profile)

    @app.get('/v1/jobs/{job_id}', dependencies=[Depends(authenticate)])
    def get_job(job_id: str):
        return expert.get(job_id)

    @app.post('/v1/jobs/{job_id}/submit', dependencies=[Depends(authenticate)])
    def submit(job_id: str, value: SubmitInput):
        return expert.submit(job_id, **value.model_dump())

    @app.post('/v1/jobs/{job_id}/analyze', dependencies=[Depends(authenticate)])
    def analyze(job_id: str, value: AnalyzeInput):
        slot = slots[value.provider]
        if not slot.acquire(blocking=False):
            raise AnalysisError('当前模型并发已满；任务仍待执行，请稍后重试。', code='provider_busy')
        try:
            return expert.analyze(job_id, value.provider, **({'assessor': assessor} if assessor else {}))
        finally:
            slot.release()

    @app.post('/v1/jobs/{job_id}/review', dependencies=[Depends(authenticate)])
    def review(job_id: str, value: ReviewInput):
        return expert.review(job_id, **value.model_dump())

    @app.post('/v1/imap/read', dependencies=[Depends(authenticate)])
    def read_imap(value: ImapInput):
        return expert.read_imap(**value.model_dump())

    return app


def serve(state=None, port=8765):
    import uvicorn
    expert = Expert(state)
    app = create_app(expert=expert)
    # A single server process owns provider semaphores. No access log/body/key output.
    uvicorn.run(app, host='127.0.0.1', port=port, access_log=False, workers=1)
