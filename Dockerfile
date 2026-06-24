# Если внешний Docker Hub недоступен — замените базовый образ на ваш внутренний,
# например: FROM docker.sphere.rn-t.ru/docker-rnd-ai/python:3.12-slim
FROM python:3.12-slim

WORKDIR /app

# PyMuPDF ставится из колёс (wheels), системные libs не нужны.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

EXPOSE 8000
# 1 worker достаточно: внутри асинхронная конкуррентность по страницам.
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
