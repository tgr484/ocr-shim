# Если внешний Docker Hub недоступен — замените базовый образ на ваш внутренний,
# например: FROM docker.sphere.rn-t.ru/docker-rnd-ai/python:3.12-slim
FROM python:3.12-slim

WORKDIR /app

# LibreOffice нужен только для экспериментального OCR Office-документов
# при ENABLE_OFFICE_OCR=true. Tesseract используется только при
# OCR_ENGINE=tesseract и ENABLE_TESSERACT_OCR=true.
RUN apt-get -o Acquire::Retries=5 -o Acquire::http::Timeout=30 update \
    && apt-get -o Acquire::Retries=5 -o Acquire::http::Timeout=30 install -y --no-install-recommends \
        libreoffice-impress \
        libreoffice-writer \
        tesseract-ocr \
        tesseract-ocr-chi-sim \
        tesseract-ocr-eng \
        tesseract-ocr-rus \
    && rm -rf /var/lib/apt/lists/*

# PyMuPDF ставится из колёс (wheels), системные libs не нужны.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

EXPOSE 8000
# 1 worker достаточно: внутри асинхронная конкуррентность по страницам.
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
