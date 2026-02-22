"""
Snapshot capture loop.

Frames are saved to:
  <data_dir>/frames/ch<id>_<name>/<YYYYMMDD_HHMMSS_mmm>.jpg

Using timestamp-based filenames means:
  - Frames persist safely across restarts (new files are appended)
  - The stitch step can sort chronologically by filename
  - You can inspect / delete individual frames on the host
"""

import asyncio
import logging
import re
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)


def _safe_name(name: str) -> str:
    return re.sub(r"[^\w-]", "_", name)


def _fmt_bytes(n: float) -> str:
    if n < 1_000_000:
        return f"{n/1_000:.0f} KB"
    elif n < 1_000_000_000:
        return f"{n/1_000_000:.1f} MB"
    else:
        return f"{n/1_000_000_000:.2f} GB"


def _fmt_remaining(end_time: datetime) -> str:
    secs = max(0.0, (end_time - datetime.now()).total_seconds())
    h, rem = divmod(int(secs), 3600)
    m = rem // 60
    return f"{h}h {m:02d}m"


async def _capture_one(nvr, channel_id: int, frame_dir: Path) -> int:
    """Capture one snapshot. Returns bytes written, or 0 on failure."""
    try:
        data = await nvr.capture_snapshot(channel_id)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]  # ms precision
        (frame_dir / f"{ts}.jpg").write_bytes(data)
        return len(data)
    except Exception as exc:
        logger.warning(f"Channel {channel_id}: snapshot failed: {exc}")
        return 0


async def run_capture(
    nvr,
    channels: list[dict],
    data_dir: str,
    interval: float,
    stop_event: asyncio.Event,
    end_time: datetime,
) -> dict[int, Path]:
    """Capture snapshots from all channels until stop_event fires."""

    frame_dirs: dict[int, Path] = {}
    for ch in channels:
        cid = ch["channel"]
        name = _safe_name(ch.get("name") or f"ch{cid}")
        d = Path(data_dir) / "frames" / f"ch{cid}_{name}"
        d.mkdir(parents=True, exist_ok=True)
        frame_dirs[cid] = d
        logger.info(f"  Channel {cid} ({ch.get('name', '?')}) → {d}")

    counts: dict[int, int] = {ch["channel"]: 0 for ch in channels}
    total_bytes: dict[int, int] = {ch["channel"]: 0 for ch in channels}

    while not stop_event.is_set():
        # Capture channels sequentially with a small pause between each.
        # Firing all snapshots simultaneously overloads the NVR and causes
        # "get config failed" (-12) errors on some channels.
        for ch in channels:
            cid = ch["channel"]
            nbytes = await _capture_one(nvr, cid, frame_dirs[cid])
            if nbytes:
                counts[cid] += 1
                total_bytes[cid] += nbytes
            if not stop_event.is_set():
                await asyncio.sleep(0.5)

        # Per-channel storage estimate based on actual average frame size
        remaining_secs = max(0.0, (end_time - datetime.now()).total_seconds())
        remaining_frames = remaining_secs / interval
        lines = [f"Sleeping {interval:.0f}s | {_fmt_remaining(end_time)} remaining"]
        for ch in channels:
            cid = ch["channel"]
            n = counts[cid]
            if n:
                avg = total_bytes[cid] / n
                est_remaining = avg * remaining_frames
                est_total = total_bytes[cid] + est_remaining
                lines.append(
                    f"  ch{cid} {ch.get('name', '?'):<20}"
                    f"  {n:>5} frames"
                    f"  avg {_fmt_bytes(avg):>8}/frame"
                    f"  → ~{_fmt_bytes(est_remaining)} remaining"
                    f"  (~{_fmt_bytes(est_total)} total)"
                )
            else:
                lines.append(f"  ch{cid} {ch.get('name', '?'):<20}  no frames yet")
        logger.info("\n".join(lines))

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass

    logger.info(f"Capture finished. Frame counts: {counts}")
    return frame_dirs
