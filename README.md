# reolink-timelapse

Captures snapshots from all online cameras on a Reolink NVR and stitches them
into standard H.264 MP4 timelapse videos.

## How it works

```
┌──────────────────────────────────────────────────────┐
│  capture mode                                        │
│  • Polls every camera via the NVR HTTP snapshot API  │
│  • Saves JPEGs to  data/frames/ch<N>_<name>/         │
│  • Filenames are timestamps → safe across restarts   │
│  • Runs for DURATION_HOURS then exits (or on SIGTERM)│
└──────────────────────────────────────────────────────┘
                          ↓
┌──────────────────────────────────────────────────────┐
│  stitch mode                                         │
│  • Reads saved frames, applies downsampling          │
│  • Encodes H.264 MP4 with ffmpeg                     │
│  • Writes to  data/videos/timelapse_<name>_<ts>.mp4  │
│  • Can be run while capture is still going           │
└──────────────────────────────────────────────────────┘
```

Frames persist on the **host** via a Docker volume mount, so a container
restart never loses data.

---

## Quick start

### 1. Configure

```bash
cp .env.example .env
# edit .env with your NVR IP, username, and password
```

### 2. Build the image

```bash
docker compose build
```

### 3. Capture

```bash
# Runs in foreground; Ctrl-C (or SIGTERM) stops it cleanly
docker compose run --rm timelapse capture
```

Or to start detached and let it run for `DURATION_HOURS`:

```bash
docker compose up -d
docker compose logs -f
```

### 4. Stitch

```bash
# Use every captured frame, output at 24 fps
docker compose run --rm timelapse stitch

# Use every 4th frame (effective 1 frame/min if captured at 1/15s), 24 fps
docker compose run --rm timelapse stitch --every-n-frames 4 --fps 24
```

Videos appear in `./data/videos/`.

---

## Configuration

All settings can be provided via `.env` or environment variables.

| Variable                  | Default | Description                                                |
|---------------------------|---------|------------------------------------------------------------|
| `NVR_HOST`                | —       | NVR IP or hostname (required)                              |
| `NVR_USERNAME`            | —       | NVR login username (required)                              |
| `NVR_PASSWORD`            | —       | NVR login password (required)                              |
| `CAPTURE_INTERVAL_SECONDS`| `15`    | Seconds between snapshots                                  |
| `DURATION_HOURS`          | `18`    | Auto-stop capture after this many hours                    |
| `CAPTURE_CHANNELS`        | (all)   | Comma-separated channel numbers to capture, e.g. `0,2,3`  |
| `STITCH_EVERY_N_FRAMES`   | `1`     | Stitch: use every Nth captured frame (1 = all)             |
| `OUTPUT_FPS`              | `24`    | Stitch: output video frame rate                            |
| `DATA_DIR`                | `/data` | Container path for frames + videos (mount a host dir here) |

### Framerate / compression math

```
video_seconds = (total_captured_frames / STITCH_EVERY_N_FRAMES) / OUTPUT_FPS

Example — 18 h capture, CAPTURE_INTERVAL_SECONDS=15, STITCH_EVERY_N_FRAMES=4, OUTPUT_FPS=24:
  captured   = 18 × 3600 / 15     = 4 320 frames per camera
  selected   = 4 320 / 4          = 1 080 frames
  video      = 1 080 / 24         ≈ 45 seconds per camera
```

To target a specific output length, rearrange:

```
STITCH_EVERY_N_FRAMES = total_captured_frames / (target_seconds × OUTPUT_FPS)
```

### Storage estimate

Each snapshot is a full-resolution JPEG from the camera:

| Resolution | Approx JPEG size |
|------------|-----------------|
| 1080p      | ~300–600 KB     |
| 4K         | ~1–4 MB         |

At `CAPTURE_INTERVAL=15` for 18 hours: **4 320 frames per camera**
- 1080p: ~1.3–2.6 GB per camera
- 4K:    ~4–17 GB per camera

---

## Docker image

The runtime image is based on
[`cgr.dev/chainguard/python`](https://hub.docker.com/r/chainguard/python)
(Wolfi-based) for a minimal CVE surface.  `ffmpeg` is installed from the Wolfi
package repository.

---

## Development (without Docker)

```bash
# Install uv  (https://docs.astral.sh/uv/)
uv sync

# Capture
uv run python -m reolink_timelapse capture

# Stitch
uv run python -m reolink_timelapse stitch --sample-rate 4 --fps 24
```
