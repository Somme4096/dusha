FROM ghcr.io/astral-sh/uv:python3.12-trixie-slim AS build

ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project --no-editable

COPY README.md LICENSE ./
COPY src ./src
RUN uv sync --locked --no-dev --no-editable

FROM python:3.12-slim-trixie

RUN groupadd --gid 1000 dusha && useradd --uid 1000 --gid 1000 --home-dir /data --no-create-home --shell /usr/sbin/nologin dusha

COPY --from=build /app/.venv /app/.venv
COPY deploy/healthcheck.py /usr/local/bin/healthcheck.py

ENV PATH=/app/.venv/bin:$PATH DUSHA_HOME=/data PYTHONUNBUFFERED=1

WORKDIR /data
RUN chown dusha:dusha /data
VOLUME /data

USER dusha
EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 CMD ["python", "/usr/local/bin/healthcheck.py"]

ENTRYPOINT ["/app/.venv/bin/dusha"]
CMD ["serve", "--home", "/data"]
