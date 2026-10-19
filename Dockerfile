FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    READBACK_DB=/data/readback.db

WORKDIR /app

COPY pyproject.toml README.md LICENSE ./
COPY src ./src

RUN pip install --no-cache-dir . \
    && useradd --create-home --uid 10001 --shell /usr/sbin/nologin readback \
    && mkdir -p /data \
    && chown -R readback:readback /data

USER readback

VOLUME ["/data"]
EXPOSE 5057

CMD ["readback", "serve", "--host", "0.0.0.0", "--port", "5057"]
