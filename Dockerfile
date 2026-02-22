# ── Stage 1: build Python virtual environment ──────────────────────────────
FROM cgr.dev/chainguard/python:latest-dev AS builder
USER root

# Bring in the uv binary (statically linked, works on any Linux)
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# Install dependencies first so this layer is cached separately from source
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# Install the package itself
COPY src/ src/
RUN uv sync --frozen --no-dev

# ── Stage 2: minimal runtime image ─────────────────────────────────────────
# cgr.dev/chainguard/python:latest-dev is Wolfi-based (apk) — significantly
# fewer CVEs than Debian/Ubuntu images.  The -dev variant is required here
# because we need apk to install ffmpeg.
FROM cgr.dev/chainguard/python:latest-dev
USER root

# ffmpeg is available as a Wolfi package
RUN apk add --no-cache ffmpeg

WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/src   /app/src

# Frames + videos are written here; mount a host directory to persist them
VOLUME ["/data"]

ENV PATH="/app/.venv/bin:$PATH"
ENV VIRTUAL_ENV="/app/.venv"

USER nonroot

# ENTRYPOINT + CMD lets you override just the subcommand at runtime:
#   docker compose up                          → capture (default)
#   docker compose run timelapse stitch ...   → stitch
ENTRYPOINT ["python", "-m", "reolink_timelapse"]
CMD ["capture"]
