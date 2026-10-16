FROM python:3.12-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.12.5 /uv /bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src
RUN uv sync --frozen --no-dev --extra otel

FROM python:3.12-slim
RUN useradd --uid 10001 --create-home app && mkdir -p /data && chown app:app /data
WORKDIR /app
COPY --from=build --chown=app:app /app /app
# SQLite (single-node) data lives on a volume; production should set MYELINATE_DATABASE_URL to Postgres.
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1 MYELINATE_ENV=prod \
    MYELINATE_DATABASE_URL=sqlite+aiosqlite:////data/myelinate.db
VOLUME ["/data"]
USER 10001
EXPOSE 8080
# Gateways answer /healthz; `myelinate worker` containers (MYELINATE_WORKER_HEARTBEAT_FILE set) use their heartbeat.
HEALTHCHECK --interval=15s --timeout=5s CMD ["myelinate", "healthcheck"]
CMD ["myelinate", "serve", "--host", "0.0.0.0", "--port", "8080"]
