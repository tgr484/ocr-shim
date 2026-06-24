# OCR-шим для LibreChat (сканы PDF → Qwen vision, без облака)

Сервис притворяется Mistral OCR API. LibreChat 0.8.6 умеет ходить на кастомный
`baseURL` только через стратегию **`mistral_ocr`** (стратегия `custom_ocr` в этой
версии НЕ подключена в диспетчере и молча откатывается на `document_parser`).
Поэтому в конфиге используем `mistral_ocr` + свой `baseURL`.

Внутри: PDF → постранично в PNG (PyMuPDF) → vision-модель Qwen через LiteLLM
(OpenAI-совместимый `/v1/chat/completions`). Картинки уходят в модель напрямую.

## 1. Файлы
Положите папку `ocr-shim/` рядом с вашим `docker-compose.yaml`:
```
ocr-shim/
  app.py
  requirements.txt
  Dockerfile
```

## 2. docker-compose.yaml
Добавьте сервис из `docker-compose.ocr-shim.yaml`. Ключевое:
- сеть `internal` (LibreChat достучится по `http://ocr-shim:8000`);
- монтируется тот же `RN-T.RU.crt`, чтобы доверять TLS у `llmproxy`;
- `VISION_MODEL` — **укажите алиас vision-модели в LiteLLM** (ту самую, что уже
  распознаёт картинки у вас в чате). Шим использует именно её, поле `mistralModel`
  из librechat.yaml он игнорирует.

## 3. .env
Добавьте общий секрет (любая строка) — он же пойдёт в librechat.yaml:
```
OCR_API_KEY=<любой-длинный-секрет>
```
`LITELLM_API_KEY` у вас уже есть.

## 4. librechat.yaml
Добавьте блок верхнего уровня:
```yaml
ocr:
  strategy: "mistral_ocr"
  baseURL: "http://ocr-shim:8000"
  apiKey: "${OCR_API_KEY}"
  mistralModel: "qwen-vision"   # игнорируется шимом, можно любое значение
```
Capability `ocr` у агентов включена по умолчанию (в дефолтном списке
возможностей агентов она есть), отдельно прописывать не нужно.

## 5. Как это запускается у пользователя
OCR в 0.8.6 работает **только через агента** (путь обработки файлов агента).
У вас `interface.agents.create: false` — значит, в UI агента не создать.
Варианты:
- временно поставить `create: true`, создать одного агента, вернуть `false`; или
- создать агента под админом.
Затем: выбрать этого агента → прикрепить PDF как **Context / Upload as Text** →
текст со сканов извлечётся через Qwen и попадёт в контекст агента.

## 6. Проверка
```
docker compose up -d --build ocr-shim
curl http://<host>:<проброшенный-порт>/health   # если порт наружу не нужен — проверяйте изнутри сети
docker compose logs -f ocr-shim                  # увидите "OCR file ...: N page(s)"
```

## Заметки по вашей среде
- Базовый образ `python:3.12-slim` тянется с Docker Hub. Если он недоступен —
  замените `FROM` в Dockerfile на образ из `docker.sphere.rn-t.ru/docker-rnd-ai/`,
  и при необходимости настройте pip на внутренний индекс (PyMuPDF ставится из wheels).
- Тяжёлые многостраничные сканы: крутите `CONCURRENCY` (параллельные страницы) и
  `OCR_DPI` (200 — баланс качества/скорости; для мелкого шрифта 300).
- Качество распознавания = качество vision-модели. Промпт можно переопределить
  через env `OCR_PROMPT`.
