"""Mistral OCR protocol for LibreChat, backed entirely by MDdoc."""

import asyncio
import json
import logging
import math
import os
import re
import secrets
import ssl
import tempfile
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, BinaryIO, Literal
from urllib.parse import urlsplit

import anyio
import httpx
from fastapi import FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field, TypeAdapter
from starlette.concurrency import run_in_threadpool

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
log = logging.getLogger("ocr-shim")

SELF_BASE_URL = os.environ.get("SELF_BASE_URL", "http://ocr-shim:8000").rstrip("/")
SHARED_SECRET = os.environ.get("OCR_SHARED_SECRET", "")
CA_CERT = os.environ.get("CA_CERT", "")
MDDOC_URL = os.environ.get("MDDOC_URL", "").strip().rstrip("/")
MDDOC_API_KEY = os.environ.get("MDDOC_API_KEY", "").strip()
MDDOC_ENGINE = os.environ.get("MDDOC_ENGINE", "").strip()
MDDOC_DESCRIBE_PDF_FIGURES = TypeAdapter(bool).validate_python(
    os.environ.get("MDDOC_DESCRIBE_PDF_FIGURES", "true")
)
MDDOC_TIMEOUT = float(os.environ.get("MDDOC_TIMEOUT", "1800"))
MDDOC_POLL_INTERVAL = float(os.environ.get("MDDOC_POLL_INTERVAL", "5"))
MDDOC_HTTP_TIMEOUT = float(os.environ.get("MDDOC_HTTP_TIMEOUT", "30"))
MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_BYTES", str(60 * 1024 * 1024)))
HEARTBEAT_INTERVAL = 15
STORE = tempfile.gettempdir()


class Job(BaseModel):
    job_id: int = Field(gt=0)
    status: Literal["queued", "parsing", "recognizing", "assembling", "done", "failed"]
    describe_pdf_figures: bool = True
    page_unit: str | None = None
    page_count: int = Field(default=0, ge=0)
    pages_done: int = Field(default=0, ge=0)
    figures_total: int = Field(default=0, ge=0)
    figures_done: int = Field(default=0, ge=0)
    position: int | None = None
    retry_rounds: int = Field(default=0, ge=0)
    retry_at: str | None = None
    error: str | None = None


class StoredFile(BaseModel):
    job_id: int = Field(gt=0)
    filename: str
    content_type: str


@asynccontextmanager
async def lifespan(app: FastAPI):
    url = urlsplit(MDDOC_URL)
    if url.scheme not in {"http", "https"} or not url.netloc or url.query or url.fragment:
        raise ValueError("MDDOC_URL must be an absolute HTTP(S) URL")
    if not MDDOC_API_KEY:
        raise ValueError("MDDOC_API_KEY is required")
    limits = (MDDOC_TIMEOUT, MDDOC_POLL_INTERVAL, MDDOC_HTTP_TIMEOUT, MAX_UPLOAD_BYTES)
    if any(not math.isfinite(value) or value <= 0 for value in limits):
        raise ValueError("Timeouts, polling interval and upload limit must be positive and finite")
    verify = ssl.create_default_context(cafile=CA_CERT) if CA_CERT else True
    async with httpx.AsyncClient(
        base_url=f"{MDDOC_URL}/",
        headers={"Authorization": f"Bearer {MDDOC_API_KEY}"},
        timeout=MDDOC_HTTP_TIMEOUT,
        verify=verify,
    ) as client:
        app.state.mddoc = client
        yield


app = FastAPI(title="LibreChat MDdoc adapter", version="2.0.1", lifespan=lifespan)


def _check_auth(authorization: str | None):
    expected = f"Bearer {SHARED_SECRET}".encode()
    if SHARED_SECRET and not secrets.compare_digest((authorization or "").encode(), expected):
        raise HTTPException(401, "invalid OCR credentials")


def _path(file_id: str) -> Path:
    if not re.fullmatch(r"[a-f0-9]{32}", file_id):
        raise HTTPException(404, "file not found")
    return Path(STORE) / f"ocrshim-{file_id}"


def _stored_file(file_id: str) -> StoredFile:
    path = _path(file_id)
    try:
        if not path.is_file():
            raise FileNotFoundError
        return StoredFile.model_validate_json(path.with_suffix(".meta").read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise HTTPException(404, "file not found") from exc
    except ValueError as exc:
        raise HTTPException(500, "invalid stored file metadata") from exc


def _remove_file(file_id: str):
    path = _path(file_id)
    path.unlink(missing_ok=True)
    path.with_suffix(".meta").unlink(missing_ok=True)


def _copy_upload(source: BinaryIO, path: Path) -> int:
    size = 0
    with path.open("xb") as target:
        while chunk := source.read(1024 * 1024):
            size += len(chunk)
            if size > MAX_UPLOAD_BYTES:
                raise HTTPException(413, f"file is too large; limit is {MAX_UPLOAD_BYTES} bytes")
            target.write(chunk)
    return size


async def _mddoc_request(client: httpx.AsyncClient, method: str, path: str, **kwargs) -> httpx.Response:
    try:
        response = await client.request(method, path, **kwargs)
    except httpx.TimeoutException as exc:
        raise HTTPException(504, "MDdoc request timed out") from exc
    except httpx.HTTPError as exc:
        log.warning("MDdoc connection failed: %s", type(exc).__name__)
        raise HTTPException(502, "MDdoc is unavailable") from exc

    if response.is_success:
        return response
    if response.status_code in {401, 403}:
        raise HTTPException(502, "MDdoc rejected adapter credentials")
    try:
        payload = response.json()
        detail = payload.get("detail") if isinstance(payload, dict) else None
    except ValueError:
        detail = None
    status = response.status_code if response.status_code in {400, 404, 409, 410, 413, 415, 422, 429} else 502
    raise HTTPException(status, detail or response.text[:500] or "MDdoc request failed")


def _decode_job(response: httpx.Response) -> Job:
    try:
        return Job.model_validate(response.json())
    except ValueError as exc:
        raise HTTPException(502, "MDdoc returned an invalid job response") from exc


async def _get_job(client: httpx.AsyncClient, job_id: int) -> Job:
    job = _decode_job(await _mddoc_request(client, "GET", f"api/v1/jobs/{job_id}"))
    if job.job_id != job_id:
        raise HTTPException(502, "MDdoc returned a different job")
    return job


async def _poll_jobs(client: httpx.AsyncClient, file_id: str, job_id: int, initial: Job | None = None):
    previous = None
    job = initial
    while True:
        _stored_file(file_id)
        if job is None:
            job = await _get_job(client, job_id)
        if job != previous:
            log.info(
                "file %s: MDdoc job %s %s, pages=%d/%d figures=%d/%d retry=%d",
                file_id, job_id, job.status, job.pages_done, job.page_count,
                job.figures_done, job.figures_total, job.retry_rounds,
            )
        yield job
        if job.status in {"done", "failed"}:
            return
        previous = job
        await asyncio.sleep(MDDOC_POLL_INTERVAL)
        job = None


@app.post("/files")
async def upload_file(
    request: Request,
    file: UploadFile = File(...),
    purpose: str = Form("ocr"),
    authorization: str | None = Header(None),
    describe_pdf_figures: Annotated[bool | None, Form()] = None,
):
    _check_auth(authorization)
    describe_figures = (
        MDDOC_DESCRIBE_PDF_FIGURES if describe_pdf_figures is None else describe_pdf_figures
    )
    data = {"describe_pdf_figures": str(describe_figures).lower()}
    if MDDOC_ENGINE:
        data["engine"] = MDDOC_ENGINE
    if file.size is not None and file.size > MAX_UPLOAD_BYTES:
        raise HTTPException(413, f"file is too large; limit is {MAX_UPLOAD_BYTES} bytes")
    file_id = uuid.uuid4().hex
    path = _path(file_id)
    try:
        size = await run_in_threadpool(_copy_upload, file.file, path)
        filename = file.filename or "unnamed"
        content_type = file.content_type or "application/octet-stream"
        with path.open("rb") as source:
            response = await _mddoc_request(
                request.app.state.mddoc,
                "POST", "api/v1/jobs",
                files={"file": (filename, source, content_type)},
                data=data,
            )
        job = _decode_job(response)
        if job.describe_pdf_figures != describe_figures:
            raise HTTPException(
                502, "MDdoc did not apply describe_pdf_figures; update MDdoc to support per-document figure modes",
            )
        record = StoredFile(job_id=job.job_id, filename=filename, content_type=content_type)
        path.with_suffix(".meta").write_text(record.model_dump_json(), encoding="utf-8")
    except BaseException:
        _remove_file(file_id)
        raise
    log.info("file %s: submitted %s (%d bytes) as MDdoc job %s", file_id, filename, size, job.job_id)
    return {"id": file_id, "object": "file", "bytes": size, "purpose": purpose}


@app.get("/files/{file_id}/url")
def signed_url(file_id: str, expiry: int = 24, authorization: str | None = Header(None)):
    _check_auth(authorization)
    _stored_file(file_id)
    return {"url": f"{SELF_BASE_URL}/files/{file_id}/raw"}


@app.get("/files/{file_id}/raw")
def raw_file(file_id: str):
    record = _stored_file(file_id)
    return FileResponse(_path(file_id), media_type=record.content_type)


def _extract_id(url: str | None) -> str:
    try:
        parsed = urlsplit(url) if isinstance(url, str) else None
        match = re.search(r"/files/([a-f0-9]{32})/raw$", parsed.path) if parsed else None
        if parsed and parsed.scheme in {"http", "https"} and parsed.netloc and match:
            return match.group(1)
    except ValueError:
        pass
    raise HTTPException(400, "document URL must reference an uploaded file")


@app.post("/ocr")
async def ocr(request: Request, authorization: str | None = Header(None)):
    _check_auth(authorization)
    try:
        body = await request.json()
    except ValueError as exc:
        raise HTTPException(400, "invalid OCR request JSON") from exc
    document = body.get("document") if isinstance(body, dict) else None
    if not isinstance(document, dict):
        raise HTTPException(400, "document is required")
    file_id = _extract_id(document.get("document_url") or document.get("image_url"))
    record = _stored_file(file_id)
    started = time.monotonic()
    try:
        async with asyncio.timeout(MDDOC_TIMEOUT):
            async for job in _poll_jobs(request.app.state.mddoc, file_id, record.job_id):
                if await request.is_disconnected():
                    raise HTTPException(499, "client disconnected")
                if job.status == "failed":
                    raise HTTPException(502, job.error or "MDdoc job failed")
                if job.status != "done":
                    continue
                result = await _mddoc_request(
                    request.app.state.mddoc, "GET", f"api/v1/jobs/{record.job_id}/markdown",
                )
                media_type = result.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                if media_type not in {"text/markdown", "text/plain"}:
                    raise HTTPException(502, "MDdoc returned an invalid Markdown response")
                log.info("file %s: MDdoc job %s returned in %.1fs", file_id, record.job_id, time.monotonic() - started)
                return {
                    "pages": [{"index": 0, "markdown": result.text, "images": [], "dimensions": None}],
                    "model": "mddoc",
                    "usage_info": {"pages_processed": job.page_count},
                }
    except TimeoutError as exc:
        raise HTTPException(504, "timed out waiting for MDdoc") from exc


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


class ProgressResponse(StreamingResponse):
    """Close the iterator even when disconnect interrupts sending a chunk."""

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            with anyio.CancelScope(shield=True):
                await self.body_iterator.aclose()


async def _events(client: httpx.AsyncClient, file_id: str, initial: Job, deadline: float):
    updates = _poll_jobs(client, file_id, initial.job_id, initial)
    pending = None
    previous = None
    last_emission = time.monotonic()
    job = initial
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise HTTPException(504, "timed out waiting for MDdoc")
            if pending is None:
                pending = asyncio.create_task(anext(updates))
            heartbeat = max(0, HEARTBEAT_INTERVAL - (time.monotonic() - last_emission))
            ready, _ = await asyncio.wait({pending}, timeout=min(remaining, heartbeat))
            if not ready:
                if time.monotonic() >= deadline:
                    raise HTTPException(504, "timed out waiting for MDdoc")
                yield ": heartbeat\n\n"
                last_emission = time.monotonic()
                continue
            job = pending.result()
            pending = None
            if job.status == "failed":
                raise HTTPException(502, job.error or "MDdoc job failed")
            if job.status == "done":
                yield _sse("done", job.model_dump())
                return
            if job != previous:
                yield _sse("progress", job.model_dump())
                previous = job
                last_emission = time.monotonic()
    except HTTPException as exc:
        yield _sse("error", {"status_code": exc.status_code, "detail": exc.detail, "job": job.model_dump()})
    finally:
        with anyio.CancelScope(shield=True):
            if pending is not None:
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
            await updates.aclose()


@app.get("/files/{file_id}/events")
async def file_events(request: Request, file_id: str, authorization: str | None = Header(None)):
    _check_auth(authorization)
    record = _stored_file(file_id)
    deadline = time.monotonic() + MDDOC_TIMEOUT
    try:
        async with asyncio.timeout(MDDOC_TIMEOUT):
            initial = await _get_job(request.app.state.mddoc, record.job_id)
    except TimeoutError as exc:
        raise HTTPException(504, "timed out waiting for MDdoc") from exc
    return ProgressResponse(
        _events(request.app.state.mddoc, file_id, initial, deadline),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
    )


@app.delete("/files/{file_id}")
def delete_file(file_id: str, authorization: str | None = Header(None)):
    _check_auth(authorization)
    _remove_file(file_id)
    return {"id": file_id, "deleted": True}


@app.get("/health")
def health():
    return {"status": "ok"}
