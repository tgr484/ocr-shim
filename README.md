# OCR-шим для LibreChat (сканы PDF → Qwen vision/Tesseract, без облака)

Сервис притворяется Mistral OCR API. LibreChat 0.8.6 умеет ходить на кастомный
`baseURL` только через стратегию **`mistral_ocr`** (стратегия `custom_ocr` в этой
версии НЕ подключена в диспетчере и молча откатывается на `document_parser`).
Поэтому в конфиге используем `mistral_ocr` + свой `baseURL`.

Внутри: PDF → постранично в PNG (PyMuPDF) → OCR-движок. По умолчанию используется
vision-модель Qwen через LiteLLM (OpenAI-совместимый `/v1/chat/completions`).
Экспериментально можно переключиться на Tesseract.

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

По умолчанию OCR выполняет LLM:
```
OCR_ENGINE=llm
```

Экспериментальный Tesseract выключен по умолчанию. Чтобы распознавать через
Tesseract русский, английский и китайский упрощенный текст, добавьте:
```
OCR_ENGINE=tesseract
ENABLE_TESSERACT_OCR=true
TESSERACT_LANGS=rus+eng+chi_sim
TESSERACT_TIMEOUT=120
```
Для возврата к LLM достаточно поставить `OCR_ENGINE=llm` или убрать эти
переменные. В Docker-образ уже добавлены `tesseract-ocr` и языковые пакеты
`rus`, `eng`, `chi_sim`.

Экспериментальный режим сравнения LLM OCR и Tesseract выключен по умолчанию.
Чтобы включить его, разрешите Tesseract и задайте отдельную модель-судью:
```
ENABLE_TESSERACT_OCR=true
ENABLE_OCR_COMPARISON=true
OCR_COMPARISON_MODEL=<алиас-модели-судьи-в-LiteLLM>
OCR_COMPARISON_TIMEOUT=120
MAX_OCR_COMPARISON_TEXT_CHARS=20000
```
В этом режиме каждая страница распознается обоими движками, затем два текста
отправляются в `OCR_COMPARISON_MODEL`. Судья оценивает опечатки, пропуски,
лишний текст, порядок строк, таблицы и структуру, возвращая JSON с победителем
`llm`, `tesseract` или `tie`, оценками и кратким объяснением. Текст, который
получит LibreChat, остается текстом выбранного `OCR_ENGINE`; сравнение
записывается в логи и в `usage_info.ocr_comparison`.
`MAX_OCR_COMPARISON_TEXT_CHARS` ограничивает размер каждого текста OCR,
переданного судье по одной странице.

Экспериментальное распознавание Office-документов выключено по умолчанию.
Чтобы включить OCR для Word/PowerPoint и LibreOffice/OpenDocument (`.doc`,
`.docx`, `.ppt`, `.pptx`, `.odt`, `.ott`, `.odp`, `.otp`), добавьте в окружение
сервиса:
```
ENABLE_OFFICE_OCR=true
OFFICE_CONVERT_TIMEOUT=120
OFFICE_OCR_TEXT_THRESHOLD=50
MAX_UPLOAD_BYTES=62914560
MAX_RENDERED_PAGES=80
MAX_OCR_PAGES=60
MAX_EXTRACTED_TEXT_CHARS=300000
```
Office-файл сначала конвертируется в PDF через LibreOffice, затем проходит
обычный пайплайн PDF → PNG → выбранный OCR-движок. Для `.docx`/`.pptx` и
`.odt`/`.ott`/`.odp`/`.otp` шим сначала смотрит наличие встроенного текста: если
найдено 50 или больше символов без пробелов, OCR пропускается, а найденный текст
возвращается LibreChat напрямую в формате Mistral OCR response. Text-only ответ
нормализуется в markdown-подобный вид: абзацы разделяются пустыми строками,
слайды получают заголовки `# Slide N`. `MAX_EXTRACTED_TEXT_CHARS` ограничивает
размер такого ответа.

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
docker compose logs -f ocr-shim                  # увидите "OCR file ...: N page(s)" и итоговое время документа
```

## Заметки по вашей среде
- Базовый образ `python:3.12-slim` тянется с Docker Hub. Если он недоступен —
  замените `FROM` в Dockerfile на образ из `docker.sphere.rn-t.ru/docker-rnd-ai/`,
  и при необходимости настройте pip на внутренний индекс (PyMuPDF ставится из wheels).
- Тяжёлые многостраничные сканы: крутите `DOCUMENT_CONCURRENCY` (одновременные
  документы), `CONCURRENCY` (параллельные страницы внутри одного документа) и
  `OCR_DPI` (200 — баланс качества/скорости; для мелкого шрифта 300).
  `DOCUMENT_CONCURRENCY=1` означает, что документы распознаются по очереди.
  `CONCURRENCY=4` означает до четырёх одновременно обрабатываемых страниц;
  порядок страниц в ответе при этом сохраняется.
- Лимиты защиты: `MAX_UPLOAD_BYTES=62914560` разрешает файлы до 60 MB,
  `MAX_RENDERED_PAGES=80` ограничивает число страниц после PDF/Office-рендера,
  `MAX_OCR_PAGES=60` ограничивает число страниц, отправляемых в OCR-движок.
  `MAX_EXTRACTED_TEXT_CHARS=300000` ограничивает text-only ответ для Office.
  При превышении hard-лимитов сервис возвращает 413 с явной причиной; text-only
  ответ обрезается с явной пометкой в конце.
- В логах для каждого документа есть итоговая метрика длительности:
  `OCR file <id>: completed <N> page(s) via engine=<llm|tesseract> in <seconds>s`.
  Для LLM и Tesseract также логируется длительность обработки каждой страницы.
- При `ENABLE_OCR_COMPARISON=true` дополнительно логируется результат LLM-судьи
  по каждой странице и итоговая сводка вида
  `OCR comparison file <id>: completed <N> page(s) in <seconds>s summary=...`.
- Tesseract OCR — экспериментальный отключаемый режим. Он быстрее и локальнее,
  но обычно хуже сохраняет таблицы/структуру, чем vision-модель. Языки задаются
  через `TESSERACT_LANGS`; значение по умолчанию `rus+eng+chi_sim`.
- Office OCR — экспериментальный режим. Он требует LibreOffice в Docker-образе
  и может отличаться по вёрстке от исходного файла, особенно для сложных
  презентаций, нестандартных шрифтов и документов с внешними объектами.
  Порог `OFFICE_OCR_TEXT_THRESHOLD` защищает обычные `.docx`/`.pptx` и ODF-файлы
  от ненужного OCR; для них шим возвращает извлеченный текст без обращения к
  vision-модели. Старые бинарные `.doc`/`.ppt` сразу идут в OCR, потому что
  быстрый preflight без LibreOffice для них не реализован.
  Каждая LibreOffice-конвертация запускается с отдельным временным профилем, а
  конвертация/рендеринг страниц выполняются вне event loop.
- Качество распознавания = качество vision-модели. Промпт можно переопределить
  через env `OCR_PROMPT`.
