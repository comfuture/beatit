FROM python:3.12-slim-bookworm
COPY --from=ghcr.io/astral-sh/uv:0.9.26 /uv /uvx /bin/
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg git libgomp1 build-essential pkg-config libopus-dev \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --extra cpu --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --extra cpu --no-dev
ENV PATH="/app/.venv/bin:$PATH" DRUM_DATA_DIR=/app/data
EXPOSE 8000
CMD ["drum-score", "serve", "--host", "0.0.0.0"]
