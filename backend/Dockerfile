# syntax=docker/dockerfile:1
# API FastAPI + pipeline de ingesta del Enterprise Multi-Commodity Pricing & Imbalance Engine.
# Contexto de build: ./backend   ->   docker build -t met-pricing-engine/backend ./backend
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    WAREHOUSE_DIR=/srv/data/warehouse

# El código vive en /srv/backend para que la raíz del proyecto que calcula app.core.config sea /srv
# y el Data Warehouse quede en /srv/data/warehouse (un volumen con nombre en docker-compose).
WORKDIR /srv/backend

COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY . .

RUN useradd --create-home --uid 10001 pricing \
    && mkdir -p /srv/data/warehouse \
    && chown -R pricing:pricing /srv
USER pricing

EXPOSE 8000

HEALTHCHECK --interval=10s --timeout=4s --start-period=40s --retries=6 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health', timeout=3)"]

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
