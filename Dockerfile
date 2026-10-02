FROM python:3.13-slim-bookworm AS builder

COPY --from=ghcr.io/astral-sh/uv:0.9.24 /uv /uvx /bin/

ENV CMAKE_BUILD_PARALLEL_LEVEL=2 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app

# The bundled Luau runtime is compiled from source during the image build.
# Limit parallel jobs to keep Railway builder memory use predictable.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        ca-certificates \
        cmake \
        git \
        ninja-build \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project

COPY main.py ./main.py
COPY bot ./bot
COPY vendor ./vendor

# Fail the image build if the archive, compiler, or Luau runtime cannot build.
RUN .venv/bin/python -c "from bot.bootstrap import ensure_luau_runtime; ensure_luau_runtime()"

FROM python:3.13-slim-bookworm AS runtime

ENV HOME=/tmp \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TMPDIR=/tmp

WORKDIR /app

# Keep only the native runtime library and TLS certificates in the final image.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        libstdc++6 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder --chown=10001:10001 /app/ /app/

# Run the bot without root; job temp files are created in /tmp.
USER 10001:10001

CMD ["/app/.venv/bin/python", "main.py"]