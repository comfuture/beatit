FROM node:24-bookworm-slim AS frontend
WORKDIR /web
COPY frontend/package*.json ./
RUN npm ci --ignore-scripts
COPY frontend ./
RUN npx nuxt generate

FROM rust:1.90.0-slim-bookworm AS rust

FROM python:3.12-slim-bookworm AS builder
COPY --from=ghcr.io/astral-sh/uv:0.9.26 /uv /uvx /bin/
COPY --from=rust /usr/local/cargo /usr/local/cargo
COPY --from=rust /usr/local/rustup /usr/local/rustup
ENV CARGO_HOME=/usr/local/cargo RUSTUP_HOME=/usr/local/rustup \
    PATH="/usr/local/cargo/bin:$PATH" UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
RUN apt-get update && apt-get install -y --no-install-recommends git build-essential pkg-config libopus-dev \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --extra cpu --no-dev --no-install-project --no-editable
COPY src ./src
COPY --from=frontend /web/.output/public ./src/transcription/static/web
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --extra cpu --no-dev --no-editable

FROM python:3.12-slim-bookworm AS colab-source
WORKDIR /bundle
COPY pyproject.toml uv.lock README.md .python-version ./
COPY src ./src
COPY scripts/build_colab_source.py /build_colab_source.py
RUN python /build_colab_source.py --root /bundle --output /beatit-source.tar

FROM python:3.12-slim-bookworm AS controller-builder
COPY --from=ghcr.io/astral-sh/uv:0.9.26 /uv /bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --only-group controller --no-install-project --python 3.12

FROM python:3.12-slim-bookworm AS colab-controller
RUN useradd --create-home --uid 10001 beatit \
    && mkdir -p /app/data /app/colab-state \
    && chown -R beatit:beatit /app/data /app/colab-state
WORKDIR /app
COPY --from=controller-builder /app/.venv /app/.venv
COPY --from=colab-source /beatit-source.tar /app/beatit-source.tar
COPY src ./src
ENV PATH="/app/.venv/bin:$PATH" PYTHONPATH=/app/src PYTHONUNBUFFERED=1 \
    HOME=/app/colab-state/home
USER beatit
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8001/health', timeout=3).read()"
CMD ["python", "-m", "transcription.colab_controller"]

FROM python:3.12-slim-bookworm AS web
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg libgomp1 libopus0 \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 beatit \
    && mkdir -p /app/data /home/beatit/.cache \
    && chown -R beatit:beatit /app/data /home/beatit
WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH" DRUM_DATA_DIR=/app/data \
    HOME=/home/beatit XDG_CACHE_HOME=/home/beatit/.cache PYTHONUNBUFFERED=1
USER beatit
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/jobs', timeout=3).read()"
CMD ["drum-score", "serve", "--host", "0.0.0.0"]
