FROM python:3.12-slim-bookworm AS builder
COPY --from=ghcr.io/astral-sh/uv:0.9.3 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
WORKDIR /app
COPY backend/pyproject.toml backend/uv.lock ./
RUN uv sync --locked --no-dev --no-install-project
COPY backend/map_sync ./map_sync
COPY backend/map_api ./map_api
COPY backend/map_render ./map_render
COPY backend/map_tiles ./map_tiles
RUN uv sync --locked --no-dev --no-editable

FROM builder AS tiles-builder
RUN uv sync --locked --no-dev --no-editable --extra tiles

FROM python:3.12-slim-bookworm AS tiles-worker
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends libcairo2 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 map && useradd --uid 10001 --gid map --no-create-home map
RUN mkdir /tiles && chown 10001:10001 /tiles
COPY --from=tiles-builder /app/.venv /app/.venv
COPY deploy/tiles_entrypoint.py /app/tiles_entrypoint.py
USER 10001:10001
ENTRYPOINT ["python", "/app/tiles_entrypoint.py"]
CMD ["--help"]

FROM tiles-worker AS update-worker
USER root
RUN mkdir /updates && chown 10001:10001 /updates
COPY scripts/update-map.py /app/update-map.py
USER 10001:10001
ENV UPDATE_STATE_DIR=/updates
ENTRYPOINT ["python", "/app/update-map.py", "--local"]
CMD []

FROM python:3.12-slim-bookworm
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
RUN groupadd --gid 10001 map && useradd --uid 10001 --gid map --no-create-home map
RUN mkdir /tiles && chown 10001:10001 /tiles
COPY --from=builder /app/.venv /app/.venv
COPY deploy/entrypoint.py /app/entrypoint.py
COPY deploy/storage_setup.py /app/storage_setup.py
USER 10001:10001
EXPOSE 8000
ENTRYPOINT ["python", "/app/entrypoint.py"]
CMD ["api"]
