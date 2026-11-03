# LibreChat attachments: integration boundary

The configured OCR block uses mistral_ocr and the adapter's base URL.
LibreChat performs this flow:

1. POST /files
2. GET /files/{id}/url
3. POST /ocr
4. DELETE /files/{id}

The adapter implements that contract and delegates every document to MDdoc.
It returns MDdoc's complete Markdown as one Mistral OCR page. Format detection,
Office parsing, PDF rendering, OCR, queueing and retries belong to MDdoc.

## Attachment routing

LibreChat has distinct paths: native/provider attachments, Upload as Text,
Agent File Context, and File Search/RAG. The adapter is only called when
LibreChat selects configured OCR for text extraction.

Office files must match fileConfig.ocr.supportedMimeTypes. Documents routed
to LibreChat's built-in parser or RAG bypass this adapter. LibreChat may
fall back to its document parser after an OCR failure; the adapter has no fallback.

## Progress

The local LibreChat implementation in packages/api/src/files/mistral/crud.ts
calls /ocr with Axios and expects a single JSON response. Its existing upload
SSE helper provides heartbeat and terminal messages, without a subscription
to MDdoc progress.

The adapter exposes authenticated GET /files/{id}/events independently.
Showing those events in LibreChat requires a separate client/backend change.
No LibreChat change is required for the existing OCR JSON flow.

## Local source references

- packages/api/src/files/mistral/crud.ts: upload, URL, OCR and cleanup;
  result aggregation adds # PAGE for multiple Mistral OCR pages.
- api/server/services/Files/process.js: extraction routing and fallback.
- packages/api/src/files/sse.ts: LibreChat upload SSE contract.

The adapter contract, configuration and SSE examples are documented in README.md.
