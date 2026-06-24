"""
Mistral-OCR-compatible shim for LibreChat (strategy: mistral_ocr).

LibreChat 0.8.6 only wires mistral_ocr / azure / vertex / document_parser
(custom_ocr is NOT dispatched), so we masquerade as a Mistral OCR endpoint.

Flow LibreChat performs (see packages/api/src/files/mistral/crud.ts):
  POST   {baseURL}/files            multipart purpose=ocr,file=...   -> {"id": ...}
  GET    {baseURL}/files/{id}/url?expiry=24                          -> {"url": ...}
  POST   {baseURL}/ocr  {model, document:{type, document_url|image_url}} -> {"pages":[{"markdown"}]}
  DELETE {baseURL}/files/{id}                                        -> 200

Internally: PDF -> page images (PyMuPDF) -> OCR engine.
Default engine is Qwen vision via LiteLLM. Experimental Tesseract OCR can be
enabled explicitly with OCR_ENGINE=tesseract and ENABLE_TESSERACT_OCR=true.
"""
import os
import re
import io
import base64
import asyncio
import json
import logging
import shutil
import subprocess
import tempfile
import time
import uuid
import zipfile
import xml.etree.ElementTree as ET
from typing import Optional

import fitz  # PyMuPDF
import httpx
from fastapi import FastAPI, UploadFile, File, Form, Header, HTTPException, Request
from fastapi.responses import JSONResponse, Response

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
log = logging.getLogger("ocr-shim")

# --- Config (all via env) ---------------------------------------------------
LLM_CHAT_URL   = os.environ.get("LLM_CHAT_URL", "")               # e.g. https://llmproxy.rnd-ai.rn-t.ru/v1/chat/completions
LLM_API_KEY    = os.environ.get("LLM_API_KEY", "")               # LiteLLM key
VISION_MODEL   = os.environ.get("VISION_MODEL", "qwen-3.6-thinking")
SELF_BASE_URL  = os.environ.get("SELF_BASE_URL", "http://ocr-shim:8000")
SHARED_SECRET  = os.environ.get("OCR_SHARED_SECRET", "")         # optional: must match LibreChat OCR_API_KEY
CA_CERT        = os.environ.get("CA_CERT", "")                   # path to internal CA bundle for LiteLLM TLS
OCR_DPI        = int(os.environ.get("OCR_DPI", "200"))
CONCURRENCY    = max(1, int(os.environ.get("CONCURRENCY", "4")))
DOCUMENT_CONCURRENCY = max(1, int(os.environ.get("DOCUMENT_CONCURRENCY", "1")))
MAX_TOKENS     = int(os.environ.get("OCR_MAX_TOKENS", "4096"))
HTTP_TIMEOUT   = float(os.environ.get("LLM_TIMEOUT", "180"))
OCR_ENGINE = os.environ.get("OCR_ENGINE", "llm").strip().lower()
ENABLE_TESSERACT_OCR = os.environ.get("ENABLE_TESSERACT_OCR", "").lower() in {"1", "true", "yes", "on"}
TESSERACT_LANGS = os.environ.get("TESSERACT_LANGS", "rus+eng+chi_sim")
TESSERACT_TIMEOUT = float(os.environ.get("TESSERACT_TIMEOUT", "120"))
ENABLE_OCR_COMPARISON = os.environ.get("ENABLE_OCR_COMPARISON", "").lower() in {"1", "true", "yes", "on"}
OCR_COMPARISON_MODEL = os.environ.get("OCR_COMPARISON_MODEL", "").strip()
OCR_COMPARISON_TIMEOUT = float(os.environ.get("OCR_COMPARISON_TIMEOUT", "120"))
MAX_OCR_COMPARISON_TEXT_CHARS = int(os.environ.get("MAX_OCR_COMPARISON_TEXT_CHARS", "20000"))
ENABLE_OFFICE_OCR = os.environ.get("ENABLE_OFFICE_OCR", "").lower() in {"1", "true", "yes", "on"}
OFFICE_CONVERT_TIMEOUT = int(os.environ.get("OFFICE_CONVERT_TIMEOUT", "120"))
OFFICE_OCR_TEXT_THRESHOLD = int(os.environ.get("OFFICE_OCR_TEXT_THRESHOLD", "50"))
MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_BYTES", str(60 * 1024 * 1024)))
MAX_RENDERED_PAGES = int(os.environ.get("MAX_RENDERED_PAGES", "80"))
MAX_OCR_PAGES = int(os.environ.get("MAX_OCR_PAGES", "60"))
MAX_EXTRACTED_TEXT_CHARS = int(os.environ.get("MAX_EXTRACTED_TEXT_CHARS", "300000"))

OCR_PROMPT = os.environ.get(
    "OCR_PROMPT",
    "Извлеки весь текст с этого изображения документа дословно, сохраняя порядок строк, "
    "таблицы (в виде markdown-таблиц) и структуру. Не добавляй комментариев, пояснений или "
    "заголовков — только сам распознанный текст в формате markdown. Если на изображении нет "
    "текста, верни пустую строку.",
)

STORE = tempfile.gettempdir()
_verify = CA_CERT if CA_CERT and os.path.exists(CA_CERT) else True

app = FastAPI(title="LibreChat OCR shim", version="1.0")
DOCUMENT_SEM = asyncio.Semaphore(DOCUMENT_CONCURRENCY)


def _check_auth(authorization: Optional[str]):
    if not SHARED_SECRET:
        return
    expected = f"Bearer {SHARED_SECRET}"
    if authorization != expected:
        raise HTTPException(status_code=401, detail="invalid OCR credentials")


def _active_ocr_model() -> str:
    if OCR_ENGINE == "llm":
        return VISION_MODEL
    if OCR_ENGINE == "tesseract":
        return f"tesseract:{TESSERACT_LANGS}"
    raise HTTPException(
        status_code=500,
        detail=f"unsupported OCR_ENGINE={OCR_ENGINE}; expected llm or tesseract",
    )


def _require_llm_chat_url(reason: str):
    if not LLM_CHAT_URL:
        raise HTTPException(status_code=500, detail=f"LLM_CHAT_URL is required {reason}")


def _require_comparison_model():
    if not OCR_COMPARISON_MODEL:
        raise HTTPException(
            status_code=500,
            detail="OCR_COMPARISON_MODEL is required when ENABLE_OCR_COMPARISON=true",
        )


def _path(file_id: str) -> str:
    # file_id is generated by us; keep it filesystem-safe
    safe = re.sub(r"[^A-Za-z0-9_.-]", "", file_id)
    return os.path.join(STORE, f"ocrshim-{safe}")


# --- 1) upload --------------------------------------------------------------
@app.post("/files")
async def upload_file(
    file: UploadFile = File(...),
    purpose: str = Form("ocr"),
    authorization: Optional[str] = Header(None),
):
    _check_auth(authorization)
    file_id = uuid.uuid4().hex
    data = await file.read()
    if len(data) > MAX_UPLOAD_BYTES:
        log.warning(
            "rejecting upload %s (%s): %d bytes exceeds limit %d",
            file.filename,
            file.content_type,
            len(data),
            MAX_UPLOAD_BYTES,
        )
        raise HTTPException(
            status_code=413,
            detail=f"file is too large: {len(data)} bytes, limit is {MAX_UPLOAD_BYTES} bytes",
        )
    with open(_path(file_id), "wb") as f:
        f.write(data)
    # also remember original filename/mime for type detection
    with open(_path(file_id) + ".meta", "w") as f:
        f.write((file.filename or "") + "\n" + (file.content_type or ""))
    log.info("stored file %s (%s, %d bytes)", file_id, file.filename, len(data))
    return {"id": file_id, "object": "file", "bytes": len(data), "purpose": purpose}


# --- 2) signed url (we just hand back a self-URL carrying the id) -----------
@app.get("/files/{file_id}/url")
async def signed_url(file_id: str, expiry: int = 24, authorization: Optional[str] = Header(None)):
    _check_auth(authorization)
    if not os.path.exists(_path(file_id)):
        raise HTTPException(status_code=404, detail="file not found")
    return {"url": f"{SELF_BASE_URL}/files/{file_id}/raw"}


# --- raw fetch (not strictly needed by LibreChat, but keeps the URL real) ---
@app.get("/files/{file_id}/raw")
async def raw_file(file_id: str):
    p = _path(file_id)
    if not os.path.exists(p):
        raise HTTPException(status_code=404, detail="file not found")
    return Response(content=open(p, "rb").read(), media_type="application/octet-stream")


# --- 3) OCR -----------------------------------------------------------------
def _extract_id(url: str) -> str:
    m = re.search(r"/files/([A-Za-z0-9_.-]+)/raw", url or "")
    if not m:
        raise HTTPException(status_code=400, detail=f"cannot parse file id from url: {url}")
    return m.group(1)


def _metadata(file_id: str) -> tuple[str, str]:
    meta = _path(file_id) + ".meta"
    name = ctype = ""
    if os.path.exists(meta):
        parts = open(meta).read().split("\n")
        name = parts[0] if parts else ""
        ctype = parts[1] if len(parts) > 1 else ""
    return name, ctype


def _is_pdf(file_id: str) -> bool:
    name, ctype = _metadata(file_id)
    if "pdf" in ctype.lower() or name.lower().endswith(".pdf"):
        return True
    # sniff magic bytes
    with open(_path(file_id), "rb") as f:
        return f.read(5) == b"%PDF-"


def _is_office_document(file_id: str) -> bool:
    name, ctype = _metadata(file_id)
    lower_name = name.lower()
    lower_ctype = ctype.lower()
    office_exts = (".doc", ".docx", ".ppt", ".pptx", ".odt", ".ott", ".odp", ".otp")
    office_mimes = (
        "application/msword",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.ms-powerpoint",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "application/vnd.oasis.opendocument.text",
        "application/vnd.oasis.opendocument.text-template",
        "application/vnd.oasis.opendocument.presentation",
        "application/vnd.oasis.opendocument.presentation-template",
    )
    return lower_name.endswith(office_exts) or lower_ctype in office_mimes


def _office_suffix(file_id: str) -> str:
    name, _ = _metadata(file_id)
    lower_name = name.lower()
    for suffix in (".docx", ".doc", ".pptx", ".ppt", ".odt", ".ott", ".odp", ".otp"):
        if lower_name.endswith(suffix):
            return suffix
    return ".bin"


def _plain_text_length(text: str) -> int:
    return len(re.sub(r"\s+", "", text))


def _normalize_line(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _text_from_node(node: ET.Element) -> str:
    chunks = []
    for child in node.iter():
        if child.text:
            chunks.append(child.text)
    return _normalize_line(" ".join(chunks))


def _numeric_suffix(name: str) -> int:
    m = re.search(r"(\d+)\.xml$", name)
    return int(m.group(1)) if m else 0


def _limit_extracted_text(file_id: str, text: str) -> str:
    if len(text) <= MAX_EXTRACTED_TEXT_CHARS:
        return text
    log.warning(
        "Office file %s: truncating extracted text from %d to %d chars",
        file_id,
        len(text),
        MAX_EXTRACTED_TEXT_CHARS,
    )
    marker = "\n\n[Text truncated by OCR shim: MAX_EXTRACTED_TEXT_CHARS reached.]"
    return text[:MAX_EXTRACTED_TEXT_CHARS].rstrip() + marker


def _extract_ooxml_text(path: str, prefixes: tuple[str, ...]) -> str:
    try:
        with zipfile.ZipFile(path) as archive:
            xml_names = [
                name
                for name in archive.namelist()
                if name.endswith(".xml") and any(name.startswith(prefix) for prefix in prefixes)
            ]
            chunks = []
            for name in xml_names:
                try:
                    root = ET.fromstring(archive.read(name))
                except ET.ParseError:
                    continue
                for node in root.iter():
                    if node.tag.endswith("}p"):
                        line = _text_from_node(node)
                        if line:
                            chunks.append(line)
            return "\n\n".join(chunks)
    except zipfile.BadZipFile:
        return ""


def _extract_docx_text(path: str) -> str:
    return _extract_ooxml_text(path, ("word/",))


def _extract_pptx_text(path: str) -> str:
    try:
        with zipfile.ZipFile(path) as archive:
            slide_names = sorted(
                [
                    name
                    for name in archive.namelist()
                    if re.match(r"ppt/slides/slide\d+\.xml$", name)
                ],
                key=_numeric_suffix,
            )
            notes_names = sorted(
                [
                    name
                    for name in archive.namelist()
                    if re.match(r"ppt/notesSlides/notesSlide\d+\.xml$", name)
                ],
                key=_numeric_suffix,
            )
            sections = []
            for idx, name in enumerate(slide_names, start=1):
                try:
                    root = ET.fromstring(archive.read(name))
                except ET.ParseError:
                    continue
                lines = []
                for node in root.iter():
                    if node.tag.endswith("}p"):
                        line = _text_from_node(node)
                        if line:
                            lines.append(line)
                if lines:
                    sections.append(f"# Slide {idx}\n\n" + "\n".join(lines))
            for idx, name in enumerate(notes_names, start=1):
                try:
                    root = ET.fromstring(archive.read(name))
                except ET.ParseError:
                    continue
                lines = []
                for node in root.iter():
                    if node.tag.endswith("}p"):
                        line = _text_from_node(node)
                        if line:
                            lines.append(line)
                if lines:
                    sections.append(f"# Notes {idx}\n\n" + "\n".join(lines))
            return "\n\n".join(sections)
    except zipfile.BadZipFile:
        return ""


def _extract_odf_text(path: str) -> str:
    try:
        with zipfile.ZipFile(path) as archive:
            xml_names = [name for name in ("content.xml", "styles.xml") if name in archive.namelist()]
            sections = []
            for name in xml_names:
                try:
                    root = ET.fromstring(archive.read(name))
                except ET.ParseError:
                    continue
                chunks = []
                for node in root.iter():
                    if node.tag.endswith("}h") or node.tag.endswith("}p"):
                        line = _text_from_node(node)
                        if line:
                            chunks.append(line)
                if chunks:
                    sections.append(f"# {name}\n\n" + "\n\n".join(chunks))
            return "\n\n".join(sections)
    except zipfile.BadZipFile:
        return ""


def _extract_office_text(file_id: str) -> str:
    suffix = _office_suffix(file_id)
    path = _path(file_id)
    if suffix == ".docx":
        return _limit_extracted_text(file_id, _extract_docx_text(path))
    if suffix == ".pptx":
        return _limit_extracted_text(file_id, _extract_pptx_text(path))
    if suffix in {".odt", ".ott", ".odp", ".otp"}:
        return _limit_extracted_text(file_id, _extract_odf_text(path))
    return ""


def _extract_text_heavy_office(file_id: str) -> Optional[str]:
    text = _extract_office_text(file_id)
    text_length = _plain_text_length(text)
    if text_length >= OFFICE_OCR_TEXT_THRESHOLD:
        log.info(
            "Office file %s: found %d text chars, returning %d chars without OCR",
            file_id,
            text_length,
            len(text),
        )
        return text
    log.info(
        "Office file %s: found %d text chars, using Office OCR",
        file_id,
        text_length,
    )
    return None


def _convert_office_to_pdf(file_id: str) -> str:
    if not ENABLE_OFFICE_OCR:
        raise HTTPException(
            status_code=415,
            detail="office document OCR is disabled; set ENABLE_OFFICE_OCR=true to enable it",
        )
    soffice = shutil.which("soffice") or shutil.which("libreoffice")
    if not soffice:
        raise HTTPException(
            status_code=500,
            detail="office document OCR requires LibreOffice/soffice in the runtime image",
        )

    source = _path(file_id)
    with tempfile.TemporaryDirectory(prefix="ocrshim-office-") as workdir:
        suffix = _office_suffix(file_id)
        input_path = os.path.join(workdir, f"input{suffix}")
        profile_path = os.path.join(workdir, "lo-profile")
        profile_uri = "file://" + os.path.abspath(profile_path)
        shutil.copyfile(source, input_path)
        cmd = [
            soffice,
            "--headless",
            "--nologo",
            "--nofirststartwizard",
            f"-env:UserInstallation={profile_uri}",
            "--convert-to",
            "pdf",
            "--outdir",
            workdir,
            input_path,
        ]
        try:
            start = time.perf_counter()
            log.info("Office file %s: LibreOffice conversion started, suffix=%s", file_id, suffix)
            completed = subprocess.run(
                cmd,
                check=True,
                capture_output=True,
                text=True,
                timeout=OFFICE_CONVERT_TIMEOUT,
            )
            log.info(
                "Office file %s: LibreOffice conversion completed in %.1fs",
                file_id,
                time.perf_counter() - start,
            )
        except subprocess.TimeoutExpired:
            log.warning(
                "Office file %s: LibreOffice conversion timed out after %ds",
                file_id,
                OFFICE_CONVERT_TIMEOUT,
            )
            raise HTTPException(status_code=504, detail="office document conversion timed out")
        except subprocess.CalledProcessError as exc:
            log.warning("office conversion failed: stdout=%s stderr=%s", exc.stdout, exc.stderr)
            raise HTTPException(status_code=422, detail="office document conversion failed")

        output_path = os.path.join(workdir, "input.pdf")
        if not os.path.exists(output_path):
            log.warning(
                "office conversion did not produce input.pdf: stdout=%s stderr=%s",
                completed.stdout,
                completed.stderr,
            )
            raise HTTPException(status_code=422, detail="office document conversion produced no PDF")

        converted_path = _path(file_id) + ".office.pdf"
        shutil.copyfile(output_path, converted_path)
        return converted_path


def _rasterize(file_id: str) -> list[bytes]:
    """Return a list of PNG bytes, one per page (or the single image as-is)."""
    p = _path(file_id)
    if _is_pdf(file_id):
        log.info("OCR file %s: rasterize route=pdf", file_id)
        return _rasterize_pdf(p)
    if _is_office_document(file_id):
        log.info("OCR file %s: rasterize route=office", file_id)
        return _rasterize_pdf(_convert_office_to_pdf(file_id))
    # already an image
    log.info("OCR file %s: rasterize route=image", file_id)
    return [open(p, "rb").read()]


def _rasterize_pdf(path: str) -> list[bytes]:
    start = time.perf_counter()
    pages = []
    zoom = OCR_DPI / 72.0
    mat = fitz.Matrix(zoom, zoom)
    with fitz.open(path) as doc:
        log.info("Rasterize PDF: page_count=%d, dpi=%d", doc.page_count, OCR_DPI)
        if doc.page_count > MAX_RENDERED_PAGES:
            log.warning(
                "Rejecting rasterize: %d pages exceeds rendered page limit %d",
                doc.page_count,
                MAX_RENDERED_PAGES,
            )
            raise HTTPException(
                status_code=413,
                detail=f"document has too many pages: {doc.page_count}, limit is {MAX_RENDERED_PAGES}",
            )
        for page in doc:
            pix = page.get_pixmap(matrix=mat, alpha=False)
            pages.append(pix.tobytes("png"))
    log.info("Rasterize PDF: rendered %d page(s) in %.1fs", len(pages), time.perf_counter() - start)
    return pages


async def _ocr_page(
    client: httpx.AsyncClient,
    png: bytes,
    sem: asyncio.Semaphore,
    page_index: int,
    total_pages: int,
    progress: dict[str, int],
    progress_lock: asyncio.Lock,
) -> str:
    b64 = base64.b64encode(png).decode()
    payload = {
        "model": VISION_MODEL,
        "temperature": 0,
        "max_tokens": MAX_TOKENS,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": OCR_PROMPT},
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
                ],
            }
        ],
    }
    headers = {"Content-Type": "application/json"}
    if LLM_API_KEY:
        headers["Authorization"] = f"Bearer {LLM_API_KEY}"
    async with sem:
        start = time.perf_counter()
        log.info("OCR page %d/%d: started", page_index + 1, total_pages)
        r = await client.post(LLM_CHAT_URL, json=payload, headers=headers, timeout=HTTP_TIMEOUT)
        if r.is_error:
            log.warning(
                "OCR page %d/%d: LLM returned HTTP %d: %s",
                page_index + 1,
                total_pages,
                r.status_code,
                r.text[:1000],
            )
        log.info(
            "OCR page %d/%d: completed in %.1fs",
            page_index + 1,
            total_pages,
            time.perf_counter() - start,
        )
        async with progress_lock:
            progress["completed"] += 1
            completed = progress["completed"]
            percent = completed * 100 / total_pages
        log.info("OCR progress: %d/%d pages completed (%.1f%%)", completed, total_pages, percent)
    r.raise_for_status()
    data = r.json()
    try:
        return data["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError):
        log.warning("unexpected LLM response: %s", data)
        return ""


def _tesseract_page_sync(png: bytes, page_index: int, total_pages: int) -> str:
    tesseract = shutil.which("tesseract")
    if not tesseract:
        raise HTTPException(
            status_code=500,
            detail="tesseract OCR requires tesseract binary in the runtime image",
        )

    with tempfile.NamedTemporaryFile(prefix="ocrshim-page-", suffix=".png") as image:
        image.write(png)
        image.flush()
        start = time.perf_counter()
        log.info(
            "OCR page %d/%d: tesseract started, langs=%s",
            page_index + 1,
            total_pages,
            TESSERACT_LANGS,
        )
        try:
            completed = subprocess.run(
                [tesseract, image.name, "stdout", "-l", TESSERACT_LANGS],
                check=True,
                capture_output=True,
                text=True,
                timeout=TESSERACT_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            log.warning(
                "OCR page %d/%d: tesseract timed out after %.1fs",
                page_index + 1,
                total_pages,
                TESSERACT_TIMEOUT,
            )
            raise HTTPException(status_code=504, detail="tesseract OCR timed out")
        except subprocess.CalledProcessError as exc:
            log.warning(
                "OCR page %d/%d: tesseract failed: stdout=%s stderr=%s",
                page_index + 1,
                total_pages,
                exc.stdout[:1000],
                exc.stderr[:1000],
            )
            raise HTTPException(status_code=422, detail="tesseract OCR failed")
        log.info(
            "OCR page %d/%d: tesseract completed in %.1fs",
            page_index + 1,
            total_pages,
            time.perf_counter() - start,
        )
        return completed.stdout or ""


async def _tesseract_page(
    png: bytes,
    sem: asyncio.Semaphore,
    page_index: int,
    total_pages: int,
    progress: dict[str, int],
    progress_lock: asyncio.Lock,
) -> str:
    async with sem:
        text = await asyncio.to_thread(_tesseract_page_sync, png, page_index, total_pages)
        async with progress_lock:
            progress["completed"] += 1
            completed = progress["completed"]
            percent = completed * 100 / total_pages
        log.info("OCR progress: %d/%d pages completed (%.1f%%)", completed, total_pages, percent)
        return text


async def _ocr_pages_llm(images: list[bytes]) -> list[str]:
    _require_llm_chat_url("when OCR_ENGINE=llm")
    sem = asyncio.Semaphore(CONCURRENCY)
    progress = {"completed": 0}
    progress_lock = asyncio.Lock()
    limits = httpx.Limits(max_connections=CONCURRENCY, max_keepalive_connections=CONCURRENCY)
    async with httpx.AsyncClient(verify=_verify, limits=limits) as client:
        return await asyncio.gather(
            *[
                _ocr_page(client, img, sem, page_index, len(images), progress, progress_lock)
                for page_index, img in enumerate(images)
            ]
        )


async def _ocr_pages_tesseract(images: list[bytes]) -> list[str]:
    if not ENABLE_TESSERACT_OCR:
        raise HTTPException(
            status_code=403,
            detail="tesseract OCR is disabled; set ENABLE_TESSERACT_OCR=true to enable it",
        )
    sem = asyncio.Semaphore(CONCURRENCY)
    progress = {"completed": 0}
    progress_lock = asyncio.Lock()
    return await asyncio.gather(
        *[
            _tesseract_page(img, sem, page_index, len(images), progress, progress_lock)
            for page_index, img in enumerate(images)
        ]
    )


def _comparison_prompt(page_index: int, llm_text: str, tesseract_text: str) -> str:
    llm_text = _limit_comparison_text(llm_text)
    tesseract_text = _limit_comparison_text(tesseract_text)
    return (
        "Сравни два результата OCR одной и той же страницы документа. "
        "Оцени качество распознавания: опечатки, пропущенный текст, лишний текст, "
        "перепутанный порядок строк, сохранение таблиц и структуры. "
        "Верни только JSON без markdown-блока со схемой: "
        '{"page": number, "winner": "llm|tesseract|tie", '
        '"llm_score": number, "tesseract_score": number, '
        '"reason": "short Russian explanation", '
        '"llm_issues": ["..."], "tesseract_issues": ["..."]}. '
        "Оценки от 0 до 10, где 10 - лучшее качество.\n\n"
        f"Страница: {page_index + 1}\n\n"
        "OCR_LLM:\n"
        f"{llm_text}\n\n"
        "OCR_TESSERACT:\n"
        f"{tesseract_text}"
    )


def _limit_comparison_text(text: str) -> str:
    if len(text) <= MAX_OCR_COMPARISON_TEXT_CHARS:
        return text
    marker = "\n\n[Text truncated by OCR shim before comparison.]"
    return text[:MAX_OCR_COMPARISON_TEXT_CHARS].rstrip() + marker


async def _compare_ocr_page(
    client: httpx.AsyncClient,
    sem: asyncio.Semaphore,
    page_index: int,
    llm_text: str,
    tesseract_text: str,
) -> dict:
    payload = {
        "model": OCR_COMPARISON_MODEL,
        "temperature": 0,
        "messages": [{"role": "user", "content": _comparison_prompt(page_index, llm_text, tesseract_text)}],
    }
    headers = {"Content-Type": "application/json"}
    if LLM_API_KEY:
        headers["Authorization"] = f"Bearer {LLM_API_KEY}"
    async with sem:
        start = time.perf_counter()
        log.info("OCR comparison page %d: started", page_index + 1)
        r = await client.post(LLM_CHAT_URL, json=payload, headers=headers, timeout=OCR_COMPARISON_TIMEOUT)
        if r.is_error:
            log.warning(
                "OCR comparison page %d: LLM returned HTTP %d: %s",
                page_index + 1,
                r.status_code,
                r.text[:1000],
            )
        r.raise_for_status()
        data = r.json()
        try:
            content = data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError):
            log.warning("unexpected OCR comparison response: %s", data)
            content = ""
        try:
            result = json.loads(content)
        except json.JSONDecodeError:
            result = {
                "page": page_index + 1,
                "winner": "unknown",
                "reason": content[:1000],
                "llm_issues": [],
                "tesseract_issues": [],
            }
        result.setdefault("page", page_index + 1)
        log.info(
            "OCR comparison page %d: winner=%s llm_score=%s tesseract_score=%s in %.1fs reason=%s",
            page_index + 1,
            result.get("winner"),
            result.get("llm_score"),
            result.get("tesseract_score"),
            time.perf_counter() - start,
            str(result.get("reason", ""))[:500],
        )
        return result


async def _compare_ocr_outputs(llm_texts: list[str], tesseract_texts: list[str]) -> list[dict]:
    _require_llm_chat_url("when ENABLE_OCR_COMPARISON=true")
    _require_comparison_model()
    sem = asyncio.Semaphore(CONCURRENCY)
    limits = httpx.Limits(max_connections=CONCURRENCY, max_keepalive_connections=CONCURRENCY)
    async with httpx.AsyncClient(verify=_verify, limits=limits) as client:
        return await asyncio.gather(
            *[
                _compare_ocr_page(client, sem, page_index, llm_text, tesseract_text)
                for page_index, (llm_text, tesseract_text) in enumerate(zip(llm_texts, tesseract_texts))
            ]
        )


def _log_ocr_comparison_summary(file_id: str, results: list[dict], elapsed: float):
    counts: dict[str, int] = {}
    for result in results:
        winner = str(result.get("winner", "unknown"))
        counts[winner] = counts.get(winner, 0) + 1
    log.info(
        "OCR comparison file %s: completed %d page(s) in %.1fs summary=%s",
        file_id,
        len(results),
        elapsed,
        json.dumps(counts, ensure_ascii=False, sort_keys=True),
    )


def _ocr_response(texts: list[str], model: str, usage_extra: Optional[dict] = None) -> JSONResponse:
    pages = [{"index": i, "markdown": t, "images": [], "dimensions": None} for i, t in enumerate(texts)]
    usage_info = {"pages_processed": len(pages)}
    if usage_extra:
        usage_info.update(usage_extra)
    return JSONResponse({"pages": pages, "model": model, "usage_info": usage_info})


@app.post("/ocr")
async def ocr(request: Request, authorization: Optional[str] = Header(None)):
    _check_auth(authorization)
    body = await request.json()
    doc = body.get("document", {})
    url = doc.get("document_url") or doc.get("image_url")
    file_id = _extract_id(url)
    if not os.path.exists(_path(file_id)):
        raise HTTPException(status_code=404, detail="file not found")

    if DOCUMENT_SEM.locked():
        log.info(
            "OCR file %s: waiting for document slot, document_concurrency=%d",
            file_id,
            DOCUMENT_CONCURRENCY,
        )
    async with DOCUMENT_SEM:
        document_start = time.perf_counter()
        response_model = _active_ocr_model()
        if _is_office_document(file_id):
            extracted_text = _extract_text_heavy_office(file_id)
            if extracted_text is not None:
                log.info(
                    "OCR file %s: returned Office extracted text in %.1fs",
                    file_id,
                    time.perf_counter() - document_start,
                )
                return _ocr_response([extracted_text], "office_text_parser")

        images = await asyncio.to_thread(_rasterize, file_id)
        if len(images) > MAX_OCR_PAGES:
            log.warning(
                "OCR file %s: rejecting %d OCR pages, limit is %d",
                file_id,
                len(images),
                MAX_OCR_PAGES,
            )
            raise HTTPException(
                status_code=413,
                detail=f"document has too many OCR pages: {len(images)}, limit is {MAX_OCR_PAGES}",
            )
        log.info(
            "OCR file %s: %d page(s) via engine=%s model=%s, document_concurrency=%d, page_concurrency=%d",
            file_id,
            len(images),
            OCR_ENGINE,
            response_model,
            DOCUMENT_CONCURRENCY,
            CONCURRENCY,
        )

        comparison_results = None
        if ENABLE_OCR_COMPARISON:
            _require_llm_chat_url("when ENABLE_OCR_COMPARISON=true")
            _require_comparison_model()
            comparison_start = time.perf_counter()
            log.info("OCR comparison file %s: started", file_id)
            llm_texts, tesseract_texts = await asyncio.gather(
                _ocr_pages_llm(images),
                _ocr_pages_tesseract(images),
            )
            comparison_results = await _compare_ocr_outputs(llm_texts, tesseract_texts)
            _log_ocr_comparison_summary(
                file_id,
                comparison_results,
                time.perf_counter() - comparison_start,
            )
            texts = llm_texts if OCR_ENGINE == "llm" else tesseract_texts
        elif OCR_ENGINE == "llm":
            texts = await _ocr_pages_llm(images)
        elif OCR_ENGINE == "tesseract":
            texts = await _ocr_pages_tesseract(images)
        else:
            raise HTTPException(status_code=500, detail="unreachable OCR engine branch")

        elapsed = time.perf_counter() - document_start
        log.info(
            "OCR file %s: completed %d page(s) via engine=%s in %.1fs",
            file_id,
            len(images),
            OCR_ENGINE,
            elapsed,
        )

    usage_extra = None
    if comparison_results is not None:
        usage_extra = {
            "ocr_comparison_enabled": True,
            "ocr_comparison_model": OCR_COMPARISON_MODEL,
            "ocr_comparison": comparison_results,
        }
    return _ocr_response(texts, response_model, usage_extra)


# --- 4) cleanup -------------------------------------------------------------
@app.delete("/files/{file_id}")
async def delete_file(file_id: str, authorization: Optional[str] = Header(None)):
    _check_auth(authorization)
    for suffix in ("", ".meta", ".office.pdf"):
        try:
            os.remove(_path(file_id) + suffix)
        except FileNotFoundError:
            pass
    return {"id": file_id, "deleted": True}


@app.get("/health")
async def health():
    return {"status": "ok"}
