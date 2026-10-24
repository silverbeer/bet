# syntax=docker/dockerfile:1
#
# The BET API image: `bet-api` (one uvicorn worker) plus the `bet` CLI, so
# `bet init` and `bet user create` can run in the same image (kubectl exec or an
# init container). See docs/decisions/0002-api-service.md.
#
# The image holds code only. The warehouse, config and every secret arrive at
# run time: data on a volume at /data, BET_JWT_SIGNING_KEY from the environment.

ARG PYTHON_IMAGE=python:3.14.9-slim-trixie

FROM ghcr.io/astral-sh/uv:0.13.5 AS uv

# ------------------------------------------------------------------ builder
FROM ${PYTHON_IMAGE} AS builder

COPY --from=uv /uv /usr/local/bin/uv

# The venv is built where it will live in the runtime stage, against the same
# interpreter, so it can be copied across unchanged.
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv

WORKDIR /build

# Dependencies first: this layer is rebuilt only when the lockfile changes.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --frozen --no-dev --no-install-project

# README.md and LICENSE.md are package metadata (pyproject `readme`, `license`).
# Migrations are package data under src/bet/database/migrations/.
COPY pyproject.toml uv.lock README.md LICENSE.md ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable

# ------------------------------------------------------------------ runtime
FROM ${PYTHON_IMAGE} AS runtime

LABEL org.opencontainers.image.title="bet" \
      org.opencontainers.image.description="BET API: personal betting intelligence, served over HTTP" \
      org.opencontainers.image.source="https://github.com/silverbeer/bet" \
      org.opencontainers.image.licenses="PolyForm-Noncommercial-1.0.0"

# Fixed ids so a Kubernetes securityContext (runAsUser, fsGroup) can match them.
ARG BET_UID=10001
ARG BET_GID=10001
RUN groupadd --system --gid "${BET_GID}" bet \
 && useradd --system --uid "${BET_UID}" --gid bet --home-dir /home/bet \
      --create-home --shell /usr/sbin/nologin bet \
 && install -d -o bet -g bet -m 0700 /data

COPY --from=builder /opt/venv /opt/venv

# Everything BET writes lands under /data: the warehouse and archives
# (BET_DATA_DIR), the config file `bet init` records the identity in, and logs.
# Keeping config on the volume lets an init container's `bet init` and the
# server see the same file. HOME is the user's own, so nothing writes to /.
ENV PATH=/opt/venv/bin:${PATH} \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOME=/home/bet \
    XDG_CONFIG_HOME=/data/config \
    XDG_STATE_HOME=/data/state \
    XDG_CACHE_HOME=/tmp/cache \
    BET_DATA_DIR=/data

USER ${BET_UID}:${BET_GID}
WORKDIR /home/bet

EXPOSE 8000

# Liveness only (/healthz never touches the database). Kubernetes ignores this
# and uses its own probes; it is here for `docker run`. Slim has no curl.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4)"]

# Exec form, so SIGTERM reaches uvicorn directly. bet-api refuses to start
# unless WEB_CONCURRENCY is unset or 1. Override the command to run the CLI:
#   docker run ... ghcr.io/silverbeer/bet bet init
CMD ["bet-api", "--host", "0.0.0.0", "--port", "8000"]
