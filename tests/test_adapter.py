"""Contract tests for LibreChat's Mistral OCR flow and MDdoc progress."""

import asyncio
import io
import json
from collections import deque
from email import policy
from email.parser import BytesParser
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException, UploadFile
from fastapi.testclient import TestClient

import app

AUTH = {"Authorization": "Bearer librechat-secret"}
CONTENT = b"%PDF-1.7\noriginal bytes\x00\xff"
MARKDOWN = (
    "<!-- page: 1 -->\n## Документ\n\n| A | B |\n|---|---|\n| 1 | 2 |\n\n\n\n"
    "![Схема](https://mddoc.example/api/v1/jobs/7/images/token/fig.png)\n\n"
    "> Описание схемы\n<!-- page: 2 -->\n中文\n"
)


def job(status="done", **kwargs):
    return {
        "job_id": 7, "status": status, "page_unit": "pdf_page",
        "page_count": 2, "pages_done": 2, "figures_total": 1, "figures_done": 1,
        "position": None, "retry_rounds": 0, "retry_at": None, "error": None,
        **kwargs,
    }


class MDdoc:
    def __init__(self):
        self.calls = []
        self.uploads = []
        self.states = deque()
        self.last_state = job()
        self.overrides = {}
        self.submit_status = 202
        self.submit_job = job("queued", page_count=0, pages_done=0)
        self.markdown = MARKDOWN

    async def __call__(self, request):
        path = request.url.path.removeprefix("/prefix")
        self.calls.append((request.method, path))
        assert request.headers["authorization"] == "Bearer mddoc-secret"
        override = self.overrides.get((request.method, path))
        if override is not None:
            if isinstance(override, Exception):
                raise override
            return override
        if request.method == "POST" and path == "/api/v1/jobs":
            body = await request.aread()
            message = BytesParser(policy=policy.default).parsebytes(
                f"Content-Type: {request.headers['content-type']}\r\n\r\n".encode() + body
            )
            self.uploads.append({
                part.get_param("name", header="content-disposition"): {
                    "filename": part.get_filename(),
                    "content_type": part.get_content_type(),
                    "content": part.get_payload(decode=True),
                }
                for part in message.iter_parts()
            })
            return httpx.Response(self.submit_status, json=self.submit_job)
        if request.method == "GET" and path == "/api/v1/jobs/7":
            if self.states:
                self.last_state = self.states.popleft()
            return httpx.Response(200, json=self.last_state)
        if request.method == "GET" and path == "/api/v1/jobs/7/markdown":
            return httpx.Response(200, text=self.markdown, headers={"Content-Type": "text/markdown; charset=utf-8"})
        pytest.fail(f"Unexpected MDdoc call: {request.method} {path}")


@pytest.fixture
def service(tmp_path, monkeypatch):
    fake = MDdoc()
    monkeypatch.setattr(app, "STORE", str(tmp_path))
    monkeypatch.setattr(app, "SELF_BASE_URL", "http://ocr-shim:8000")
    monkeypatch.setattr(app, "SHARED_SECRET", "librechat-secret")
    monkeypatch.setattr(app, "MDDOC_URL", "http://mddoc:8000")
    monkeypatch.setattr(app, "MDDOC_API_KEY", "mddoc-secret")
    monkeypatch.setattr(app, "MDDOC_ENGINE", "")
    monkeypatch.setattr(app, "CA_CERT", "")
    monkeypatch.setattr(app, "MDDOC_POLL_INTERVAL", 0.001)
    monkeypatch.setattr(app, "MDDOC_TIMEOUT", 2)
    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        app.httpx, "AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(fake), **kwargs),
    )
    with TestClient(app.app, headers=AUTH) as client:
        yield client, fake
    assert app.app.state.mddoc.is_closed


def upload(client, filename="test.pdf", content_type="application/pdf", content=CONTENT):
    response = client.post("/files", files={"file": (filename, content, content_type)}, data={"purpose": "ocr"})
    assert response.status_code == 200, response.text
    return response.json()["id"]


def recognize(client, file_id, document_type="document_url"):
    signed = client.get(f"/files/{file_id}/url?expiry=24")
    assert signed.status_code == 200
    return client.post("/ocr", json={
        "model": "qwen-3.6-flash", "image_limit": 0, "include_image_base64": False,
        "document": {"type": document_type, document_type: signed.json()["url"]},
    })


def events(response):
    assert response.status_code == 200, response.text
    return [
        (lines[0].removeprefix("event: "), json.loads(lines[1].removeprefix("data: ")))
        for block in response.text.split("\n\n")
        if (lines := block.splitlines()) and lines[0].startswith("event: ")
    ]


def test_librechat_flow_and_exact_markdown(service):
    client, fake = service
    fake.states.extend([job("queued"), job("parsing"), job("recognizing"), job("assembling"), job()])
    fake.markdown += "x" * 310_000
    file_id = upload(client)
    assert fake.calls == [("POST", "/api/v1/jobs")]
    raw = client.get(f"/files/{file_id}/raw", headers={"Authorization": ""})
    assert raw.status_code == 200
    assert raw.content == CONTENT
    assert raw.headers["content-type"] == "application/pdf"
    response = recognize(client, file_id)
    assert response.status_code == 200, response.text
    assert response.json() == {
        "pages": [{"index": 0, "markdown": fake.markdown, "images": [], "dimensions": None}],
        "model": "mddoc", "usage_info": {"pages_processed": 2},
    }
    assert client.delete(f"/files/{file_id}").json() == {"id": file_id, "deleted": True}
    assert not list(app.Path(app.STORE).iterdir())
    assert client.get(f"/files/{file_id}/raw").status_code == 404
    assert client.delete(f"/files/{file_id}").status_code == 200
    assert all(method != "DELETE" for method, _ in fake.calls)


@pytest.mark.parametrize("extension", ["pdf", "djvu", "docx", "pptx", "xlsx", "doc", "ppt", "xls", "rtf", "odt", "odp", "ods"])
def test_all_formats_are_forwarded_without_local_parsing(service, extension):
    client, fake = service
    filename = f"Исходный документ.{extension}"
    file_id = upload(client, filename, "application/octet-stream", b"unparsed\x00original")
    assert fake.uploads == [{
        "file": {"filename": filename, "content_type": "application/octet-stream", "content": b"unparsed\x00original"},
    }]
    assert recognize(client, file_id).status_code == 200


@pytest.mark.parametrize("status", [200, 202])
def test_repeated_ocr_reuses_job_including_cached_upload(service, status):
    client, fake = service
    fake.submit_status = status
    fake.submit_job = job()
    file_id = upload(client)
    assert recognize(client, file_id).status_code == 200
    assert recognize(client, file_id, "image_url").status_code == 200
    assert len(fake.uploads) == 1
    assert fake.calls.count(("POST", "/api/v1/jobs")) == 1


def test_engine_is_only_sent_when_configured(service, monkeypatch):
    client, fake = service
    monkeypatch.setattr(app, "MDDOC_ENGINE", "text")
    upload(client)
    assert fake.uploads[0]["engine"]["content"] == b"text"


def test_upstream_base_path_is_preserved(service, monkeypatch):
    client, fake = service
    monkeypatch.setattr(app, "MDDOC_URL", "http://mddoc:8000/prefix")
    with TestClient(app.app, headers=AUTH) as prefixed:
        assert recognize(prefixed, upload(prefixed)).status_code == 200
    assert len(fake.uploads) == 1


def test_partial_success_and_empty_markdown_are_returned(service):
    client, fake = service
    fake.last_state = job(error="Не удалось распознать страниц: 1 из 2")
    assert recognize(client, upload(client)).json()["pages"][0]["markdown"] == MARKDOWN
    fake.markdown = ""
    assert recognize(client, upload(client)).json()["pages"][0]["markdown"] == ""


@pytest.mark.parametrize("content_type", ["image/png", "image/jpeg"])
def test_images_receive_mddoc_unsupported_format_error(service, content_type):
    client, fake = service
    fake.overrides[("POST", "/api/v1/jobs")] = httpx.Response(415, json={"detail": "Формат не поддерживается"})
    response = client.post("/files", files={"file": ("image.png", b"image", content_type)})
    assert response.status_code == 415
    assert response.json() == {"detail": "Формат не поддерживается"}
    assert not list(app.Path(app.STORE).iterdir())


@pytest.mark.parametrize("method,path,kwargs", [
    ("POST", "/files", {"files": {"file": ("file.pdf", CONTENT)}}),
    ("GET", "/files/" + "a" * 32 + "/url", {}),
    ("GET", "/files/" + "a" * 32 + "/events", {}),
    ("DELETE", "/files/" + "a" * 32, {}),
    ("POST", "/ocr", {"json": {}}),
])
def test_authorization_before_upstream_calls(service, method, path, kwargs):
    client, fake = service
    response = client.request(method, path, headers={"Authorization": "Bearer wrong"}, **kwargs)
    assert response.status_code == 401
    assert fake.calls == []


def test_health_does_not_call_mddoc(service):
    client, fake = service
    assert client.get("/health", headers={"Authorization": ""}).json() == {"status": "ok"}
    assert fake.calls == []


@pytest.mark.parametrize("suffix", ["url", "raw", "events"])
def test_missing_files_are_404(service, suffix):
    client, fake = service
    assert client.get(f"/files/{'a' * 32}/{suffix}").status_code == 404
    assert fake.calls == []


@pytest.mark.parametrize("body", [None, [], {}, {"document": []}, {"document": {}}, {"document": {"document_url": 42}}, {"document": {"document_url": "http://["}}])
def test_invalid_ocr_input_is_400(service, body):
    client, fake = service
    response = client.post("/ocr", content=json.dumps(body), headers={"Content-Type": "application/json"})
    assert response.status_code == 400
    assert fake.calls == []


def test_malformed_json_is_400(service):
    client, _ = service
    assert client.post("/ocr", content=b"{", headers={"Content-Type": "application/json"}).status_code == 400


def test_unknown_ocr_file_and_metadata_path_are_not_read(service):
    client, fake = service
    response = client.post("/ocr", json={"document": {"document_url": f"http://ocr-shim:8000/files/{'a' * 32}/raw"}})
    assert response.status_code == 404
    file_id = upload(client)
    assert client.get(f"/files/{file_id}.meta/raw").status_code == 404
    assert len(fake.calls) == 1


def test_upload_size_limit_and_cleanup(service, monkeypatch):
    client, fake = service
    monkeypatch.setattr(app, "MAX_UPLOAD_BYTES", 3)
    response = client.post("/files", files={"file": ("big.pdf", b"1234")})
    assert response.status_code == 413
    assert fake.calls == []
    assert not list(app.Path(app.STORE).iterdir())

    async def upload_without_reported_size():
        with pytest.raises(HTTPException) as exc:
            await app.upload_file(
                SimpleNamespace(app=app.app),
                UploadFile(file=io.BytesIO(b"1234"), filename="big.pdf"),
                "ocr", "Bearer librechat-secret",
            )
        assert exc.value.status_code == 413
    asyncio.run(upload_without_reported_size())
    assert fake.calls == []
    assert not list(app.Path(app.STORE).iterdir())


@pytest.mark.parametrize("upstream,expected", [(400, 400), (401, 502), (403, 502), (413, 413), (415, 415), (422, 422), (429, 429), (500, 502), (503, 502), (302, 502)])
def test_upstream_upload_errors_do_not_fall_back(service, upstream, expected):
    client, fake = service
    fake.overrides[("POST", "/api/v1/jobs")] = httpx.Response(upstream, json={"detail": "upstream error"})
    response = client.post("/files", files={"file": ("test.pdf", CONTENT)})
    assert response.status_code == expected
    assert not list(app.Path(app.STORE).iterdir())
    assert len(fake.calls) == 1


@pytest.mark.parametrize("exception,expected", [(httpx.ConnectError("offline"), 502), (httpx.ReadTimeout("slow"), 504)])
def test_transport_errors_cleanup_upload(service, exception, expected):
    client, fake = service
    fake.overrides[("POST", "/api/v1/jobs")] = exception
    response = client.post("/files", files={"file": ("test.pdf", CONTENT)})
    assert response.status_code == expected
    assert not list(app.Path(app.STORE).iterdir())


@pytest.mark.parametrize("payload", [[], {}, {"job_id": 0, "status": "done"}, {"job_id": 7, "status": "unknown"}])
def test_invalid_upstream_upload_response_is_502_and_cleaned(service, payload):
    client, fake = service
    fake.overrides[("POST", "/api/v1/jobs")] = httpx.Response(200, json=payload)
    response = client.post("/files", files={"file": ("test.pdf", CONTENT)})
    assert response.status_code == 502
    assert not list(app.Path(app.STORE).iterdir())


def test_invalid_status_response_and_wrong_job_id_are_502(service):
    client, fake = service
    file_id = upload(client)
    for response in [httpx.Response(200, text="{"), httpx.Response(200, json=job(job_id=8))]:
        fake.overrides[("GET", "/api/v1/jobs/7")] = response
        assert recognize(client, file_id).status_code == 502


@pytest.mark.parametrize("status", [404, 409, 410])
def test_markdown_fetch_errors_are_preserved(service, status):
    client, fake = service
    fake.overrides[("GET", "/api/v1/jobs/7/markdown")] = httpx.Response(status, json={"detail": "result unavailable"})
    assert recognize(client, upload(client)).status_code == status


def test_invalid_markdown_response_is_502(service):
    client, fake = service
    fake.overrides[("GET", "/api/v1/jobs/7/markdown")] = httpx.Response(200, json={"unexpected": "json"})
    assert recognize(client, upload(client)).status_code == 502


def test_failed_job_is_502_without_fetching_markdown(service):
    client, fake = service
    fake.last_state = job("failed", error="Отменён администратором")
    response = recognize(client, upload(client))
    assert response.status_code == 502
    assert response.json() == {"detail": "Отменён администратором"}
    assert ("GET", "/api/v1/jobs/7/markdown") not in fake.calls


def test_total_deadline_covers_polling_and_slow_http(service, monkeypatch):
    client, fake = service
    file_id = upload(client)
    fake.last_state = job("queued")
    monkeypatch.setattr(app, "MDDOC_TIMEOUT", 0.02)
    monkeypatch.setattr(app, "MDDOC_POLL_INTERVAL", 1)
    assert recognize(client, file_id).status_code == 504

    async def stalled(request):
        await asyncio.sleep(1)
        pytest.fail("The total deadline must cancel a pending upstream request")

    transport = app.app.state.mddoc._transport
    transport.handler = stalled
    assert recognize(client, file_id).status_code == 504
    assert client.get(f"/files/{file_id}/events").status_code == 504


def test_sse_reports_changes_retries_and_partial_success(service):
    client, fake = service
    snapshots = [
        job("queued", position=3, page_count=0, pages_done=0),
        job("queued", position=3, page_count=0, pages_done=0),
        job("parsing", pages_done=0),
        job("recognizing", pages_done=2, figures_done=0),
        job("recognizing", pages_done=1, retry_rounds=1, retry_at="2026-11-03T10:00:00"),
        job("assembling"),
        job(error="Не удалось распознать страниц: 1 из 2"),
    ]
    fake.states.extend(snapshots)
    file_id = upload(client)
    response = client.get(f"/files/{file_id}/events")
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["x-accel-buffering"] == "no"
    assert response.headers["cache-control"] == "no-cache, no-transform"
    stream = events(response)
    assert [kind for kind, _ in stream] == ["progress"] * 5 + ["done"]
    assert [data for _, data in stream] == snapshots[:1] + snapshots[2:]
    assert fake.calls.count(("POST", "/api/v1/jobs")) == 1
    assert ("GET", "/api/v1/jobs/7/markdown") not in fake.calls


def test_sse_cached_done_returns_one_terminal_event(service):
    client, fake = service
    file_id = upload(client)
    assert events(client.get(f"/files/{file_id}/events")) == [("done", job())]


def test_sse_failed_job_is_terminal_error(service):
    client, fake = service
    fake.states.extend([job("recognizing"), job("failed", error="model offline")])
    stream = events(client.get(f"/files/{upload(client)}/events"))
    assert [kind for kind, _ in stream] == ["progress", "error"]
    assert stream[-1][1] == {"status_code": 502, "detail": "model offline", "job": job("failed", error="model offline")}


def test_sse_http_error_before_headers_and_during_stream(service):
    client, fake = service
    file_id = upload(client)
    fake.overrides[("GET", "/api/v1/jobs/7")] = httpx.Response(503, json={"detail": "offline"})
    assert client.get(f"/files/{file_id}/events").status_code == 502

    fake.overrides.clear()
    fake.states.extend([job("queued"), {"bad": "response"}])
    stream = events(client.get(f"/files/{file_id}/events"))
    assert [kind for kind, _ in stream] == ["progress", "error"]
    assert stream[-1][1]["status_code"] == 502
    assert stream[-1][1]["job"] == job("queued")


def test_sse_heartbeat_and_deadline(service, monkeypatch):
    client, fake = service
    fake.last_state = job("queued")
    monkeypatch.setattr(app, "MDDOC_TIMEOUT", 0.05)
    monkeypatch.setattr(app, "MDDOC_POLL_INTERVAL", 1)
    monkeypatch.setattr(app, "HEARTBEAT_INTERVAL", 0.01)
    response = client.get(f"/files/{upload(client)}/events")
    assert ": heartbeat\n\n" in response.text
    stream = events(response)
    assert [kind for kind, _ in stream] == ["progress", "error"]
    assert stream[-1][1]["status_code"] == 504


@pytest.mark.parametrize("during_send", [False, True])
def test_sse_disconnect_cancels_pending_upstream_request(service, monkeypatch, during_send):
    client, fake = service
    file_id = upload(client)

    monkeypatch.setattr(app, 'HEARTBEAT_INTERVAL', 0.005)

    async def scenario():
        cancelled = asyncio.Event()
        disconnect = asyncio.Event()
        messages = []
        reads = 0

        async def handler(request):
            nonlocal reads
            reads += 1
            if reads == 1:
                return httpx.Response(200, json=job("queued"))
            if not during_send:
                disconnect.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        app.app.state.mddoc._transport.handler = handler

        async def receive():
            await disconnect.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            messages.append(message)
            if during_send and b': heartbeat' in message.get('body', b''):
                disconnect.set()
                await asyncio.Event().wait()

        scope = {
            "type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
            "method": "GET", "scheme": "http", "path": f"/files/{file_id}/events",
            "query_string": b"", "root_path": "", "headers": [(b"authorization", b"Bearer librechat-secret")],
            "server": ("testserver", 80), "client": ("testclient", 123),
        }
        await asyncio.wait_for(app.app(scope, receive, send), timeout=1)
        assert cancelled.is_set()
        assert reads == 2
        assert any(message["type"] == "http.response.start" and message["status"] == 200 for message in messages)

    asyncio.run(scenario())
    assert all(method != "DELETE" for method, _ in fake.calls)


@pytest.mark.parametrize("setting,value,error", [
    ("MDDOC_URL", "", "MDDOC_URL"),
    ("MDDOC_URL", "file:///tmp/mddoc", "MDDOC_URL"),
    ("MDDOC_API_KEY", "", "MDDOC_API_KEY"),
    ("MDDOC_TIMEOUT", 0, "positive"),
    ("MDDOC_POLL_INTERVAL", float("nan"), "finite"),
    ("MAX_UPLOAD_BYTES", -1, "positive"),
])
def test_startup_requires_valid_backend_configuration(monkeypatch, setting, value, error):
    monkeypatch.setattr(app, "MDDOC_URL", "http://mddoc:8000")
    monkeypatch.setattr(app, "MDDOC_API_KEY", "secret")
    monkeypatch.setattr(app, setting, value)
    with pytest.raises(ValueError, match=error):
        with TestClient(app.app):
            pytest.fail("Startup should fail")
