# LibreChat attachments and OCR research

Date: 2026-06-24

## Summary

LibreChat has several attachment paths. They are easy to confuse because the
same uploaded file can be handled as a provider attachment, extracted into chat
context, stored as agent context, or indexed for RAG.

For this OCR shim, the important point is: the shim is only called when
LibreChat selects an OCR strategy compatible with the Mistral OCR flow. If
LibreChat uses its built-in document parser, this shim is not involved.

## Attachment paths

### Standard upload

The file is attached to the message or provider-specific upload path. This is
not necessarily text extraction. Images can be passed to vision-capable models,
and provider-specific file APIs may be used when the selected endpoint supports
them.

### Upload as Text

LibreChat extracts text from the file and injects the extracted text into the
current conversation context.

The documented processing priority is:

1. OCR, when OCR is configured and the file matches OCR-supported MIME types.
2. STT, for audio files when STT is configured.
3. Text parsing, for known text/document MIME types.
4. Fallback text parsing.

This is the path users typically mean when they ask LibreChat to "read this
file" in the current chat.

### Agent File Context

LibreChat extracts text from uploaded files and stores that extracted text as
part of the agent's system instructions. The context persists with the agent,
unlike Upload as Text, which is scoped to the current conversation.

Agent File Context can use text parsing by default and OCR/STT when configured
and applicable.

### File Search / RAG

This is a separate path. The file is chunked and indexed, then relevant chunks
are retrieved later for user questions. It is not the same as OCR or Upload as
Text.

## When LibreChat uses OCR

LibreChat uses OCR when the selected file-processing path needs text extraction
and the OCR configuration is applicable:

- The user uses Upload as Text or Agent File Context.
- `ocr` is configured in `librechat.yaml`.
- The file MIME type matches OCR-supported MIME types.
- The configured OCR strategy can process the file.

For OCR strategies such as `mistral_ocr`, LibreChat performs a Mistral-style
file/OCR API flow. This shim implements that flow:

1. `POST /files`
2. `GET /files/{id}/url`
3. `POST /ocr` with `document_url` or `image_url`
4. `DELETE /files/{id}`

The shim returns `pages[].markdown`, which LibreChat injects into the relevant
context.

## When LibreChat uses document parsing

LibreChat can parse many text-based documents without OCR. Its documented
`document_parser` handles text-based files such as PDF, DOCX, XLS/XLSX, and
OpenDocument files locally.

This is usually sufficient for:

- Digital PDFs with embedded text.
- DOCX files containing normal text.
- Spreadsheet files where cell text can be extracted directly.
- Source code, Markdown, JSON, YAML, CSV, and other plain-text formats.

OCR is still needed for:

- Scanned PDFs.
- Image-only PDFs.
- Screenshots or photos of documents.
- Complex layouts where visual structure matters more than embedded text.
- Presentations where slide layout, visual ordering, or non-text elements are
  important.

## Implications for this shim

Because fallback behavior can vary by LibreChat version and route, the shim
should not rely on LibreChat fallback for Office files. It should behave as a
hybrid extractor:

- For text-heavy modern Office/OpenDocument files, return extracted text directly
  as `pages[].markdown` without calling the vision model.
- For image-heavy Office/OpenDocument files, render through LibreOffice to PDF,
  rasterize pages, then use the vision model.
- For old binary `.doc`/`.ppt`, use LibreOffice rendering and OCR because the
  shim does not implement a cheap text preflight for those formats.
- For PDF scans, complex PDFs, and images with text, keep using the existing
  rasterize-to-vision OCR path.

This keeps Office behavior deterministic even before validating LibreChat's
runtime fallback behavior in the target deployment.

## Sources

- LibreChat OCR docs: https://www.librechat.ai/docs/features/ocr
- LibreChat Upload as Text docs: https://www.librechat.ai/docs/features/upload_as_text
- LibreChat OCR config object: https://www.librechat.ai/docs/configuration/librechat_yaml/object_structure/ocr
- LibreChat fileConfig object: https://www.librechat.ai/docs/configuration/librechat_yaml/object_structure/file_config
