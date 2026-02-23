# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project does

Captures JPEG snapshots from all cameras on a Reolink NVR on a configurable interval, then stitches them into dual-resolution H.264 MP4 timelapse videos using FFmpeg.

## Commands

```bash
# Install dependencies
uv sync

# Run locally (reads .env)
uv run python -m reolink_timelapse test      # verify NVR connectivity, list cameras
uv run python -m reolink_timelapse capture   # start capture loop
uv run python -m reolink_timelapse stitch    # encode frames → MP4

# Docker
docker compose build
docker compose run --rm timelapse test
docker compose run --rm timelapse capture
docker compose run --rm timelapse stitch --every-n-frames 4 --fps 24
docker compose up -d && docker compose logs -f
```

Copy `.env.example` to `.env` and fill in `NVR_HOST`, `NVR_USERNAME`, `NVR_PASSWORD`.

## Architecture

`src/reolink_timelapse/` contains five modules:

- **`__main__.py`** — CLI entry point. Parses args, configures logging, installs SIGTERM/SIGINT handlers, dispatches to `capture` or `stitch`.
- **`config.py`** — Pydantic Settings model; all config comes from env vars / `.env`.
- **`nvr.py`** — Async HTTP client for the Reolink API. Owns all session logic.
- **`capture.py`** — Snapshot loop: fetches all channels sequentially, saves timestamped JPEGs under `data/frames/ch<id>_<name>/`, logs storage estimates.
- **`stitch.py`** — Builds an FFmpeg concat demuxer list and runs a single FFmpeg pass that writes both full-resolution and 720p MP4s.

## NVR API constraints (critical)

The NVR firmware is old (v2.0.0.280) with a hard session limit of ~2 concurrent tokens:

- **rspCode -5** = session limit hit → wait 30 s, retry up to 5×
- **rspCode -6** = bad/expired token → invalidate token and re-login
- **rspCode -12** = transient "NVR busy" → do NOT invalidate token; just retry the snapshot
- Always **logout before login** to free the slot immediately
- Snapshot requests must be **sequential with ≥0.5 s gaps**; parallel requests cause -12 errors
- Channel detail calls (`GetOsd`, `GetEnc`) must also be sequential at startup

`asyncio.Lock` with double-checked locking in `nvr.py` prevents login stampedes when multiple coroutines need a token simultaneously.

## Docker notes

- `stop_grace_period: 30s` in `docker-compose.yml` is intentional — gives Python time to logout before SIGKILL.
- Base image: `cgr.dev/chainguard/python:latest-dev` (Wolfi); FFmpeg installed via `apk`.
- `/data` is chowned to `nonroot` before the `VOLUME` declaration so the host mount is writable.

## Key configuration variables

| Variable | Default | Purpose |
|---|---|---|
| `CAPTURE_INTERVAL_SECONDS` | `15` | Seconds between snapshot rounds |
| `DURATION_HOURS` | `18` | Auto-stop after N hours |
| `CAPTURE_CHANNELS` | `` | Comma-separated channel IDs to capture (empty = all) |
| `STITCH_EVERY_N_FRAMES` | `1` | Frame downsample factor |
| `OUTPUT_FPS` | `24` | Output video framerate |
| `DATA_DIR` | `/data` | Root for frames and output videos |
