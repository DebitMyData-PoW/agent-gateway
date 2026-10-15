FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app
RUN useradd --system --uid 10001 --home-dir /app --shell /usr/sbin/nologin gateway

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install .

COPY config ./config
COPY alembic.ini ./
COPY migrations ./migrations

USER gateway
EXPOSE 8000

CMD ["uvicorn", "--factory", "dmd_gateway.main:create_app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
