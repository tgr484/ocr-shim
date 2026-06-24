# HANDOFF — интеграция OCR-шима в self-hosted LibreChat

Контекст для агентского режима. Цель: включить распознавание **сканов PDF**
в LibreChat без облачных зависимостей, переиспользуя уже работающую vision-модель
Qwen через LiteLLM. Все артефакты (`app.py`, `Dockerfile`, `requirements.txt`,
`docker-compose.ocr-shim.yaml`, `README.md`) лежат в папке `ocr-shim/`.

## Среда
- LibreChat **0.8.6**, `librechat.yaml` версии **1.3.12**, развёрнут в Docker.
- Единая bridge-сеть `internal`. MongoDB, meilisearch, rag-api, vectordb, librechat.
- LLM-шлюз: **LiteLLM** на `https://llmproxy.rnd-ai.rn-t.ru` (внутренний TLS).
  - Кастомный endpoint "RNT", модель чата `qwen-3.6-thinking`, эмбеддинги `qwen-3.6-flash`.
  - Внутренний CA `./librechat/RN-T.RU.crt` монтируется в контейнеры (`NODE_EXTRA_CA_CERTS`).
- Жёсткое требование: **никакого облака / сторонних API**.
- Подтверждено: одиночная картинка в чате распознаётся Qwen — пробел только в конвертации PDF→картинки.

## Ключевые факты (проверено по исходникам LibreChat 0.8.6)
1. Стратегия **`custom_ocr` НЕ подключена** в диспетчере `getStrategyFunctions`
   (`api/server/services/Files/strategies.js`). При `strategy: custom_ocr` бросается
   «Invalid file source» и происходит молчаливый откат на `document_parser`
   (`api/server/services/Files/process.js`), который на сканах возвращает пусто.
   → Рабочий хук: **`strategy: mistral_ocr` + кастомный `baseURL`**.
2. Протокол, который дёргает LibreChat (`packages/api/src/files/mistral/crud.ts`),
   относительно `baseURL`:
   - `POST {baseURL}/files` (multipart: `purpose=ocr`, `file`) → `{"id": ...}`
   - `GET {baseURL}/files/{id}/url?expiry=24` → `{"url": ...}`
   - `POST {baseURL}/ocr` тело `{model, image_limit, include_image_base64,
     document:{type:"document_url"|"image_url", document_url|image_url:url}}`
     → `{"pages":[{"markdown": "..."}]}` (LibreChat склеивает `pages[].markdown`,
     добавляя `# PAGE n` при >1 странице)
   - `DELETE {baseURL}/files/{id}` (очистка, best-effort)
   Аутентификация — `Authorization: Bearer {ocr.apiKey}`.
3. OCR срабатывает **только через агента** (`processAgentFileUpload`): нужен агент,
   capability `ocr` (есть в дефолтном списке возможностей агентов), и совпадение MIME
   с `fileConfig.ocr.supportedMimeTypes` (pdf и картинки входят по умолчанию).

## Что делает шим
`app.py` (FastAPI) реализует протокол выше. На `/ocr`: достаёт id из URL, грузит файл,
PDF → постранично PNG (PyMuPDF, `OCR_DPI`), каждую страницу шлёт в
`{LLM_CHAT_URL}` (OpenAI-совместимый `/v1/chat/completions`) с `VISION_MODEL`,
параллельно (`CONCURRENCY`), собирает `pages[].markdown`. Картинки идут в модель напрямую.

## Шаги деплоя (выполнить агентом на хосте со стеком)
1. Скопировать папку `ocr-shim/` рядом с `docker-compose.yaml`.
2. В `docker-compose.yaml` добавить сервис из `ocr-shim/docker-compose.ocr-shim.yaml`.
   Проверить отступы (YAML), что сервис в сети `internal` и монтирует `RN-T.RU.crt`.
3. В `.env` добавить `OCR_API_KEY=<длинный-секрет>` (тот же попадёт в шим как `OCR_SHARED_SECRET`).
4. В `librechat.yaml` добавить блок верхнего уровня:
   ```yaml
   ocr:
     strategy: "mistral_ocr"
     baseURL: "http://ocr-shim:8000"
     apiKey: "${OCR_API_KEY}"
     mistralModel: "qwen-vision"   # игнорируется шимом
   ```
5. **Выставить `VISION_MODEL`** в compose шима = алиас vision-модели в LiteLLM
   (той, что распознаёт картинки в чате). НЕ оставлять дефолт вслепую — сверить со списком моделей LiteLLM.
6. `docker compose up -d --build ocr-shim` → затем `docker compose up -d librechat` (перечитать конфиг).
7. Создать агента: временно `interface.agents.create: true` в `librechat.yaml`
   (рестарт librechat), создать одного агента, вернуть `false`. Либо создать под админом.

## Верификация
- `docker compose logs -f ocr-shim` — при загрузке PDF должно быть `OCR file ...: N page(s) via <model>`.
- `docker compose logs -f librechat` — НЕ должно быть `falling back to document_parser`.
- В UI: выбрать агента → прикрепить скан PDF как **Context / Upload as Text** → текст извлекается.
- Здоровье шима: `GET http://ocr-shim:8000/health` (изнутри сети) → `{"status":"ok"}`.

## Открытые переменные / на что смотреть агенту
- `VISION_MODEL` — единственная вещь, которую нужно подтвердить руками (список моделей LiteLLM).
- Базовый образ `python:3.12-slim` тянется с Docker Hub. Если только внутренний реестр —
  заменить `FROM` на `docker.sphere.rn-t.ru/docker-rnd-ai/...` и, если надо, pip на внутренний индекс.
- Шиму нужен `CA_CERT=/app/ca.crt` (внутренний CA) для TLS к `llmproxy` — иначе SSL-ошибки.
- Тюнинг качества/скорости: `OCR_DPI` (300 для мелкого шрифта), `CONCURRENCY`, `OCR_PROMPT`.

## Возможные грабли
- `Invalid file source: custom_ocr` в логах → стоит `custom_ocr` вместо `mistral_ocr`.
- Пустой текст со сканов, но без ошибок → OCR не запустился (нет агента / capability),
  либо `VISION_MODEL` не vision.
- SSL/cert ошибки в логах шима → не примонтирован/не указан `CA_CERT`.
- 401 от шима → `OCR_SHARED_SECRET` ≠ `ocr.apiKey` (`${OCR_API_KEY}`).
