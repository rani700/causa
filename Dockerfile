# syntax=docker/dockerfile:1.7
# Causa: web UI + API (+ MCP and CLI) in one image. Built by CI only (.github/workflows).

# -- build: dependencies, the package, and the built-in data copy -----------------------------------
FROM python:3.12.11-slim-bookworm AS build
COPY --from=ghcr.io/astral-sh/uv:0.9.17 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /src

# Dependencies first: this layer is reused until uv.lock changes.
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev --no-install-project

COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev --no-editable

# The fallback copy: the simulator is deterministic, so this is the same company Exasol holds.
# Causa answers from it while Exasol is unreachable (an idle SaaS cluster stops itself).
COPY causa.yaml /app/causa.yaml
RUN CAUSA_BACKEND=duckdb CAUSA_DUCKDB_PATH=/app/data/causa.duckdb /app/.venv/bin/causa sim run

# -- runtime: no build tools, non-root -----------------------------------------------------------
FROM python:3.12.11-slim-bookworm
RUN useradd --uid 10001 --user-group --no-create-home --shell /usr/sbin/nologin causa
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
COPY --from=build /app/causa.yaml /app/causa.yaml
# DuckDB writes replay scratch tables into its file, so the copy belongs to the app user.
COPY --from=build --chown=causa:causa /app/data /app/data

ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    CAUSA_FALLBACK_DUCKDB_PATH=/app/data/causa.duckdb \
    CAUSA_DUCKDB_PATH=/app/data/causa.duckdb
USER causa
EXPOSE 8000
# uvicorn drains in-flight requests on SIGTERM.
CMD ["causa", "serve", "--host", "0.0.0.0", "--port", "8000"]
