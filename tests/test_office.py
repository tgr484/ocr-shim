"""Office text: local DOCX parsing and the MDdoc route.

Run: pip install -r requirements.txt pytest && pytest tests
"""

import asyncio
import io
import os
import sys
import zipfile

import httpx
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app  # noqa: E402

W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'


def _docx(body: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("word/document.xml", f"<w:document {W}><w:body>{body}</w:body></w:document>")
    return buffer.getvalue()


def _store(file_id: str, data: bytes, name: str, ctype: str = "") -> None:
    with open(app._path(file_id), "wb") as f:
        f.write(data)
    with open(app._path(file_id) + ".meta", "w") as f:
        f.write(f"{name}\n{ctype}")


@pytest.fixture
def docx_file(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "STORE", str(tmp_path))
    body = (
        "<w:p>"
        "<w:r><w:t>опе</w:t></w:r><w:r><w:rPr><w:b/></w:rPr><w:t>чатка</w:t></w:r>"
        '<w:r><w:t xml:space="preserve"> в </w:t></w:r>'
        "<w:del><w:r><w:delText>удалённом </w:delText></w:r></w:del>"
        "<w:r><w:t>тексте</w:t></w:r>"
        "</w:p>"
        "<w:p><w:r><w:instrText> PAGE \\* MERGEFORMAT </w:instrText></w:r><w:r><w:t>Страница</w:t></w:r></w:p>"
    )
    _store("doc1", _docx(body), "Правила.docx")
    return "doc1"


def test_runs_of_one_word_are_joined_without_a_space(docx_file):
    text = app._extract_docx_text(app._path(docx_file))
    assert "опечатка в тексте" in text
    assert "опе чатка" not in text


def test_deleted_revisions_and_field_codes_are_not_text(docx_file):
    text = app._extract_docx_text(app._path(docx_file))
    assert "удалённом" not in text
    assert "MERGEFORMAT" not in text
    assert "Страница" in text


def test_mddoc_markdown_is_cleaned_for_the_chat_model():
    markdown = (
        "<!-- page: 1 -->\n## Раздел\n\n"
        "![Схема](https://mddoc.example/api/v1/jobs/1/images/t/fig1.png)\n\n> Схема процесса\n\n\n\n"
        "<!-- page: 2 -->\nТекст"
    )
    docx = app._clean_mddoc_markdown(markdown, "section")
    assert docx == "## Раздел\n\n[Иллюстрация]\n\n> Схема процесса\n\nТекст"

    pptx = app._clean_mddoc_markdown(markdown, "slide")
    assert pptx.startswith("[Слайд 1]\n## Раздел")
    assert "[Слайд 2]" in pptx


def _mddoc(monkeypatch, handler):
    """Points the shim at a fake MDdoc served by `handler`."""
    monkeypatch.setattr(app, "MDDOC_URL", "http://mddoc:8000")
    monkeypatch.setattr(app, "MDDOC_API_KEY", "secret")
    monkeypatch.setattr(app, "MDDOC_POLL_INTERVAL", 0)
    real = httpx.AsyncClient
    monkeypatch.setattr(
        app.httpx,
        "AsyncClient",
        lambda **kwargs: real(transport=httpx.MockTransport(handler), **kwargs),
    )


def test_office_document_goes_through_mddoc(docx_file, monkeypatch):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(f"{request.method} {request.url.path}")
        assert request.headers["authorization"] == "Bearer secret"
        if request.method == "POST":
            return httpx.Response(202, json={"job_id": 7, "status": "queued", "page_unit": None})
        if request.url.path == "/api/v1/jobs/7":
            return httpx.Response(200, json={"job_id": 7, "status": "done", "page_unit": "section"})
        return httpx.Response(200, text="<!-- page: 1 -->\n## Правила\n\nНачало в 9:00")

    _mddoc(monkeypatch, handler)
    text = asyncio.run(app._mddoc_markdown(docx_file))

    assert text == "## Правила\n\nНачало в 9:00"
    assert calls == ["POST /api/v1/jobs", "GET /api/v1/jobs/7", "GET /api/v1/jobs/7/markdown"]


@pytest.mark.parametrize(
    "handler",
    [
        lambda request: httpx.Response(503, json={"detail": "down"}),
        lambda request: httpx.Response(
            200 if request.method == "GET" else 202,
            json={"job_id": 7, "status": "failed", "error": "broken"},
        ),
    ],
    ids=["unavailable", "job failed"],
)
def test_falls_back_to_the_local_parser(docx_file, monkeypatch, handler):
    _mddoc(monkeypatch, handler)
    assert asyncio.run(app._mddoc_markdown(docx_file)) is None


def test_gives_up_on_a_job_that_runs_too_long(docx_file, monkeypatch):
    _mddoc(
        monkeypatch,
        lambda request: httpx.Response(
            200 if request.method == "GET" else 202,
            json={"job_id": 7, "status": "recognizing"},
        ),
    )
    monkeypatch.setattr(app, "MDDOC_TIMEOUT", 0)
    assert asyncio.run(app._mddoc_markdown(docx_file)) is None


def test_skips_mddoc_when_not_configured_or_unsupported(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "STORE", str(tmp_path))
    _store("odt1", b"x", "file.odt")
    _mddoc(monkeypatch, lambda request: pytest.fail("MDdoc must not be called"))
    assert asyncio.run(app._mddoc_markdown("odt1")) is None

    monkeypatch.setattr(app, "MDDOC_URL", "")
    _store("doc2", b"x", "file.docx")
    assert asyncio.run(app._mddoc_markdown("doc2")) is None
