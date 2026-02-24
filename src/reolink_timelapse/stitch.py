"""
Timelapse stitch step.

Reads saved JPEG frames from <data_dir>/frames/ and encodes MP4 files into
<data_dir>/videos/.

Two files are produced per channel:
  timelapse_<channel>.mp4        — original resolution
  timelapse_<channel>_720p.mp4  — scaled to 720p (for sharing)

Downsampling example
--------------------
If you captured at 1 frame / 15 s and want 1 frame / minute in the output:
    every_n_frames = 4   (keep every 4th frame)

Output duration math
--------------------
  selected_frames = total_frames / every_n_frames
  video_seconds   = selected_frames / output_fps

  e.g. 18 h × (60/15) frames/min = 4 320 captured frames per camera
       every_n_frames=4  →  1 080 selected frames
       output_fps=24     →  1 080 / 24 = 45 s of video
"""

import asyncio
import logging
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

# Scale filter for the 720p output: height = 720, width auto-calculated and
# rounded to the nearest even number (required by libx264).
_SCALE_720P = "scale=-2:720"


def _date_suffix(frames: list[Path]) -> str:
    """Return a date suffix derived from the first and last frame filenames.

    Frame names are YYYYMMDD_HHMMSS_mmm.jpg.  If both dates are the same,
    returns '_YYYY-MM-DD'; otherwise '_YYYY-MM-DD_YYYY-MM-DD'.
    """
    def fmt(stem: str) -> str:
        d = stem[:8]
        return f"{d[:4]}-{d[4:6]}-{d[6:8]}"

    first = fmt(frames[0].stem)
    last = fmt(frames[-1].stem)
    return f"_{first}" if first == last else f"_{first}_{last}"


async def _encode_channel(
    frame_dir: Path,
    output_full: Path,
    output_720p: Path,
    every_n_frames: int,
    output_fps: int,
) -> None:
    frames = sorted(frame_dir.glob("*.jpg"))
    if not frames:
        logger.warning(f"No frames in {frame_dir} — skipping")
        return

    selected = frames[::every_n_frames]

    suffix = _date_suffix(selected)
    output_full = output_full.with_stem(output_full.stem + suffix)
    output_720p = output_720p.with_stem(output_720p.stem + suffix)
    video_s = len(selected) / output_fps
    logger.info(
        f"{frame_dir.name}: {len(frames)} frames, "
        f"every_n_frames={every_n_frames} → {len(selected)} selected, "
        f"output ≈ {video_s:.1f}s at {output_fps} fps"
    )

    # Build ffmpeg concat demuxer input file.
    # Each entry: "file '/abs/path.jpg'\nduration <secs>"
    # The last file must be repeated without a duration to avoid a 1-frame
    # green flash at the end (ffmpeg concat demuxer quirk).
    frame_dur = 1.0 / output_fps
    lines: list[str] = []
    for f in selected:
        lines.append(f"file '{f.absolute()}'")
        lines.append(f"duration {frame_dur:.6f}")
    lines.append(f"file '{selected[-1].absolute()}'")

    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as tmp:
        tmp.write("\n".join(lines))
        concat_file = tmp.name

    # Single ffmpeg call, two outputs — input is decoded only once.
    #   Output 1: full resolution
    #   Output 2: scaled to 720p
    try:
        cmd = [
            "ffmpeg", "-y",
            "-f", "concat", "-safe", "0", "-i", concat_file,
            # ── full resolution ──
            "-c:v", "libx264", "-preset", "slow", "-crf", "18",
            "-pix_fmt", "yuv420p", "-movflags", "+faststart",
            str(output_full),
            # ── 720p ──
            "-vf", _SCALE_720P,
            "-c:v", "libx264", "-preset", "slow", "-crf", "18",
            "-pix_fmt", "yuv420p", "-movflags", "+faststart",
            str(output_720p),
        ]
        logger.info(f"Encoding {output_full.name} + {output_720p.name} ...")
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            logger.error(f"ffmpeg error:\n{stderr.decode()}")
            raise RuntimeError(f"ffmpeg failed for {frame_dir.name}")

        full_mb = output_full.stat().st_size / 1_000_000
        p720_mb = output_720p.stat().st_size / 1_000_000
        logger.info(
            f"Encoded → {output_full.name} ({full_mb:.1f} MB)"
            f"  +  {output_720p.name} ({p720_mb:.1f} MB)"
        )
    finally:
        Path(concat_file).unlink(missing_ok=True)


async def run_stitch(data_dir: str, every_n_frames: int, output_fps: int) -> None:
    frames_base = Path(data_dir) / "frames"
    video_dir = Path(data_dir) / "videos"
    video_dir.mkdir(parents=True, exist_ok=True)

    if not frames_base.exists():
        logger.error(f"Frames directory not found: {frames_base}")
        return

    channel_dirs = sorted(d for d in frames_base.iterdir() if d.is_dir())
    if not channel_dirs:
        logger.error(f"No channel sub-directories found under {frames_base}")
        return

    for ch_dir in channel_dirs:
        stem = f"timelapse_{ch_dir.name}"
        await _encode_channel(
            ch_dir,
            output_full=video_dir / f"{stem}.mp4",
            output_720p=video_dir / f"{stem}_720p.mp4",
            every_n_frames=every_n_frames,
            output_fps=output_fps,
        )

    logger.info(f"All videos written to {video_dir}")
