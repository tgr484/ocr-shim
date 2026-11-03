# Сопровождение ocr-shim

ocr-shim — адаптер протокола Mistral OCR между LibreChat и MDdoc.
Разбор документов и OCR выполняет MDdoc.

## Поток

1. LibreChat отправляет multipart в POST /files.
2. Адаптер временно сохраняет исходник, передаёт его в POST /api/v1/jobs
   MDdoc и сохраняет связь случайного file_id с job_id.
3. LibreChat получает /files/{id}/url и передаёт его в /ocr.
4. Адаптер ждёт done, получает /api/v1/jobs/{job_id}/markdown и возвращает
   один элемент pages[].markdown без преобразования текста.
5. LibreChat вызывает DELETE /files/{id}; удаляются только локальные данные.

GET /files/{id}/events транслирует снимки MDdoc через SSE. Для отображения
событий в LibreChat нужна отдельная интеграция. Формат событий и примеры
приведены в README.

## Развёртывание

- Блок LibreChat: strategy=mistral_ocr, baseURL=http://ocr-shim:8000,
  apiKey=${OCR_API_KEY}, mistralModel=qwen-3.6-flash,
  allowedAddresses=["ocr-shim:8000"].
- mistralModel не выбирает модель MDdoc.
- В compose передать OCR_SHARED_SECRET=${OCR_API_KEY},
  MDDOC_URL=http://mddoc:8000, MDDOC_API_KEY=${MDDOC_API_KEY_OCRSHIM}.
- Оба сервиса должны быть доступны в сети internal.
- Пустой MDDOC_ENGINE оставляет выбор движка MDdoc.
- При HTTPS к MDdoc можно задать CA_CERT; доступа к LLM адаптеру не нужно.
- После обновления пересоздать контейнер и повторить незавершённые загрузки.
- Проверить /health, затем полный файловый цикл с работающим MDdoc.
- Для Office проверить MIME в fileConfig.ocr.supportedMimeTypes.

## Границы ответственности

В адаптере остаются файловый контракт, временное хранение, HTTP-клиент MDdoc,
ожидание результата, SSE и перевод ошибок. Форматы, модели, очередь,
дедупликация и повторы обработки принадлежат MDdoc.

Задача done с error означает частичный успех; failed — ошибку. Счётчики при
повторах могут уменьшаться. Не удалять задачи MDdoc при очистке LibreChat:
Markdown содержит ссылки на их иллюстрации.

Отдельные PNG/JPEG MDdoc пока отклоняет с 415; локальной обработки нет.
Локальные файлы удаляются по DELETE или при пересоздании контейнера;
результаты MDdoc хранятся по его retention.

## Проверки

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements-dev.txt
pytest -q
docker build -t ocr-shim:local .
```

## Режим иллюстраций PDF

OCR-shim передаёт `describe_pdf_figures=true|false` в POST /api/v1/jobs.
Значение по умолчанию выбирается `MDDOC_DESCRIBE_PDF_FIGURES` в адаптере;
поле формы POST /files переопределяет его для отдельной загрузки.
MDdoc хранит флаг в задаче и учитывает в дедупликации. Конфиг MDdoc не
меняется; требуется поддержка параметра API из MDdoc PR #22.
