"""
Timelapse stitch step.

Reads saved JPEG frames from <data_dir>/frames/ and encodes MP4 files into
<data_dir>/videos/.

Downsampling example
--------------------
If you captured at 1 frame / 15 s and want 1 frame / minute in the output:
    sample_rate = 4   (keep every 4th frame)

Output duration math
--------------------
  selected_frames = total_frames / sample_rate
  video_seconds   = selected_frames / output_fps

  e.g. 18 h × (60/15) frames/min = 4 320 captured frames per camera
       sample_rate=4  →  1 080 selected frames
       output_fps=24  →  1 080 / 24 = 45 s of video
"""

import asyncio
import logging
import tempfile
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)


async def _encode_channel(
    frame_dir: Path,
    output_path: Path,
    sample_rate: int,
    output_fps: int,
) -> None:
    frames = sorted(frame_dir.glob("*.jpg"))
    if not frames:
        logger.warning(f"No frames in {frame_dir} — skipping")
        return

    selected = frames[::sample_rate]
    video_s = len(selected) / output_fps
    logger.info(
        f"{frame_dir.name}: {len(frames)} frames, "
        f"sample_rate={sample_rate} → {len(selected)} selected, "
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
    # Repeat last frame (no duration) so ffmpeg flushes it properly
    lines.append(f"file '{selected[-1].absolute()}'")

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".txt", delete=False
    ) as tmp:
        tmp.write("\n".join(lines))
        concat_file = tmp.name

    try:
        cmd = [
            "ffmpeg", "-y",
            "-f", "concat",
            "-safe", "0",
            "-i", concat_file,
            "-c:v", "libx264",
            "-preset", "slow",
            "-crf", "18",
            "-pix_fmt", "yuv420p",
            "-movflags", "+faststart",
            str(output_path),
        ]
        logger.info(f"Running: {' '.join(cmd)}")
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            logger.error(f"ffmpeg error:\n{stderr.decode()}")
            raise RuntimeError(f"ffmpeg failed for {output_path}")

        size_mb = output_path.stat().st_size / 1_000_000
        logger.info(f"Encoded → {output_path} ({size_mb:.1f} MB)")
    finally:
        Path(concat_file).unlink(missing_ok=True)


async def run_stitch(data_dir: str, sample_rate: int, output_fps: int) -> None:
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

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    for ch_dir in channel_dirs:
        output = video_dir / f"timelapse_{ch_dir.name}_{timestamp}.mp4"
        await _encode_channel(ch_dir, output, sample_rate, output_fps)

    logger.info(f"All videos written to {video_dir}")
