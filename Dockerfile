FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    ORQUESTA_DATABASE_URL=sqlite+aiosqlite:////home/orquesta/orquesta.db

WORKDIR /app

COPY pyproject.toml README.md ./
COPY app ./app
RUN pip install . && useradd --create-home orquesta
USER orquesta

# Render, Railway y similares indican el puerto en $PORT.
ENV PORT=8000
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python -c "import os, urllib.request; urllib.request.urlopen(f'http://localhost:{os.environ[\"PORT\"]}/api/health')"

CMD ["sh", "-c", "exec uvicorn app.main:create_app --factory --host 0.0.0.0 --port ${PORT} --proxy-headers --forwarded-allow-ips='*'"]
