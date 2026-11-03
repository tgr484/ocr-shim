# ocr-shim: LibreChat → MDdoc

Адаптер между LibreChat со стратегией `mistral_ocr` и сервисом MDdoc.
Принимает файл, передаёт его в MDdoc, ждёт готовности и возвращает Markdown
в формате Mistral OCR. Распознавание, разбор форматов, очередь и повторные
попытки выполняет MDdoc.

## Подключение

Блок LibreChat сохраняется:

```yaml
ocr:
  strategy: "mistral_ocr"
  baseURL: "http://ocr-shim:8000"
  apiKey: "${OCR_API_KEY}"
  mistralModel: "qwen-3.6-flash"
  allowedAddresses:
    - "ocr-shim:8000"
```

Поле `mistralModel` принимается для совместимости. Модель и движок выбирает
MDdoc; по умолчанию адаптер не передаёт `engine`. Для переопределения
задайте `MDDOC_ENGINE`.

Добавьте сервис из `docker-compose.ocr-shim.yaml` в существующий compose-стек
с сетью `internal`. В `.env` этого стека задайте:

```dotenv
OCR_API_KEY=<секрет для LibreChat и адаптера>
MDDOC_API_KEY_OCRSHIM=<сервисный токен MDdoc для источника ocr-shim>
```

Внутри контейнера эти значения становятся `OCR_SHARED_SECRET` и
`MDDOC_API_KEY`. Токен MDdoc выдаётся администратором MDdoc и определяет
источник и приоритет обработки. URL по умолчанию — `http://mddoc:8000`.

```bash
# Готовый образ tgr484/ocr-shim:2.0.0 из Docker Hub:
docker compose pull ocr-shim
docker compose up -d --no-build ocr-shim

# Или сборка из исходников:
docker compose up -d --build ocr-shim
docker compose logs -f ocr-shim
```

Для офисных форматов проверьте маршрутизацию соответствующих MIME-типов
в OCR через `fileConfig.ocr.supportedMimeTypes` LibreChat. Адаптер вызывается
только там, где LibreChat выбрал эту стратегию извлечения текста.

## Настройки

| Переменная | По умолчанию | Назначение |
|---|---|---|
| MDDOC_URL | обязательно | Базовый HTTP(S) URL MDdoc |
| MDDOC_API_KEY | обязательно | Bearer-токен MDdoc |
| OCR_SHARED_SECRET | пусто | Секрет LibreChat; пустое значение отключает проверку |
| SELF_BASE_URL | http://ocr-shim:8000 | URL адаптера в возвращаемых ссылках |
| MDDOC_ENGINE | пусто | Пусто — выбор движка на стороне MDdoc |
| MDDOC_TIMEOUT | 1800 | Ожидание результата в /ocr или SSE, секунды |
| MDDOC_POLL_INTERVAL | 5 | Интервал опроса статуса, секунды |
| MDDOC_HTTP_TIMEOUT | 30 | Таймаут отдельного HTTP-запроса, секунды |
| MAX_UPLOAD_BYTES | 62914560 | Максимальный размер файла, 60 МиБ |
| CA_CERT | пусто | CA bundle для HTTPS к MDdoc; иначе системные сертификаты |
| LOG_LEVEL | INFO | Уровень логов |

Отсутствие URL/ключа MDdoc или некорректные лимиты останавливают запуск.
Для HTTPS с внутренним CA включите mount из compose-примера и задайте
`CA_CERT=/app/ca.crt`.

Текущий MDdoc принимает PDF, DjVu, DOCX, PPTX, XLSX, DOC, PPT, XLS, RTF,
ODT, ODP, ODS. Проверку формата выполняет MDdoc. Отдельные PNG/JPEG и остальные
неподдерживаемые форматы получают его ошибку `415`.

## Файловый протокол

| Запрос | Результат |
|---|---|
| POST /files | multipart file, purpose=ocr; передаёт файл в MDdoc и возвращает id |
| GET /files/{id}/url?expiry=24 | Возвращает URL исходника для вызова /ocr |
| GET /files/{id}/raw | Отдаёт временный исходник по непрогнозируемому ID |
| POST /ocr | Ждёт задачу MDdoc и возвращает Mistral OCR JSON |
| GET /files/{id}/events | SSE-поток статуса MDdoc |
| DELETE /files/{id} | Удаляет временный исходник и метаданные адаптера |
| GET /health | {"status":"ok"} без обращения к MDdoc |

Для загрузки, получения URL, OCR, SSE и удаления нужен заголовок
`Authorization: Bearer <OCR_SHARED_SECRET>`, если секрет задан.
`raw` доступен по URL без Bearer, как в прежнем файловом протоколе.
Параметр `expiry` принимается для совместимости; срок жизни URL определяется
наличием временного файла.

При передаче в MDdoc сохраняются исходные имя, MIME и байты.
Повторный `/ocr` использует полученный `job_id`. Ответ содержит весь Markdown
в одном `pages[0].markdown`, `model: "mddoc"` и `usage_info.pages_processed`
со счётчиком страниц MDdoc. Маркеры страниц, таблицы, ссылки, пробелы и
предупреждения сохраняются. LibreChat поэтому не добавляет искусственные
`# PAGE` для разделов Office.

Ошибки файлов и недоступного результата (400, 404, 409, 410, 413, 415, 422,
429) передаются с объяснением MDdoc. Ошибки связи, авторизации адаптера
в MDdoc, некорректный ответ и задача failed дают 502. Таймаут даёт 504.
Задача done с предупреждением в error возвращается как частичный успех.

Удаление в LibreChat **не удаляет задачу MDdoc**: иллюстрации в Markdown и
дедупликация зависят от её сохранности. Срок хранения результата определяет
MDdoc. При отключении клиента опрос прекращается, задача продолжает работу.
Локальные файлы живут до DELETE или пересоздания контейнера.

## SSE-прогресс

```bash
curl -H "Authorization: Bearer $OCR_API_KEY" \
  -F "purpose=ocr" -F "file=@document.pdf" \
  http://localhost:8000/files

# FILE_ID — id из ответа загрузки.
curl -N -H "Authorization: Bearer $OCR_API_KEY" \
  "http://localhost:8000/files/$FILE_ID/events"
```

Событие progress содержит текущий снимок MDdoc:

```text
event: progress
data: {"job_id":7,"status":"recognizing","page_unit":"pdf_page","page_count":10,"pages_done":3,"figures_total":2,"figures_done":1,"position":null,"retry_rounds":0,"retry_at":null,"error":null}
```

Стадии: queued, parsing, recognizing, assembling, затем done или failed.
Первый снимок отправляется сразу, следующие — при изменении. Каждые 15 секунд
без изменений отправляется комментарий `: heartbeat`, в том числе во время
медленного запроса статуса.

Событие done содержит последний снимок и завершает поток. Результат
забирается обычным POST /ocr. Событие error содержит status_code, detail,
последний известный снимок в job и завершает поток. Ошибки до открытия SSE
возвращаются обычным HTTP-ответом. Повторное подключение получает актуальный
снимок; история событий не хранится.

Счётчики передаются без общего процента: при повторной обработке MDdoc может
уменьшить pages_done. Поле page_unit различает печатные страницы, разделы,
слайды и листы.

Текущий Mistral OCR-клиент LibreChat ожидает JSON от /ocr. Для отображения
SSE в интерфейсе LibreChat нужна отдельная интеграция. Endpoint можно читать
через `curl -N` или fetch с Bearer-заголовком. При наличии reverse proxy
разрешите долгие соединения; ответ содержит `X-Accel-Buffering: no`.

## Локальный запуск и тесты

Нужен Python 3.11+; контейнер использует Python 3.12.

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements-dev.txt
pytest -q

# Заполните ключи и адрес MDdoc в копии .env.example.
cp .env.example .env
uvicorn app:app --host 0.0.0.0 --port 8000 --env-file .env
```

Контрактные тесты подменяют MDdoc и проверяют передачу исходников,
цикл LibreChat, Markdown, ошибки, SSE, heartbeat и отключение клиента.
Для проверки с реальным MDdoc загрузите документ, прочитайте SSE и вызовите
/ocr; в LibreChat проверьте загрузку того же документа как текста.

При обновлении пересоздайте контейнер и повторите незавершённые загрузки:
старые временные метаданные не содержат job_id. Старые переменные LLM,
Tesseract, Office-конвертации и ограничения страниц удалены из конфигурации.
