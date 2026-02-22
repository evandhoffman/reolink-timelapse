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


async def _capture_one(nvr, channel_id: int, frame_dir: Path) -> bool:
    try:
        data = await nvr.capture_snapshot(channel_id)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]  # ms precision
        (frame_dir / f"{ts}.jpg").write_bytes(data)
        return True
    except Exception as exc:
        logger.warning(f"Channel {channel_id}: snapshot failed: {exc}")
        return False


async def run_capture(
    nvr,
    channels: list[dict],
    data_dir: str,
    interval: float,
    stop_event: asyncio.Event,
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

    while not stop_event.is_set():
        tasks = [
            _capture_one(nvr, ch["channel"], frame_dirs[ch["channel"]])
            for ch in channels
        ]
        results = await asyncio.gather(*tasks)

        for ch, ok in zip(channels, results):
            if ok:
                counts[ch["channel"]] += 1

        total = sum(counts.values())
        if total and total % (len(channels) * 10) == 0:
            logger.info(f"Frames captured so far: {counts}")

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass

    logger.info(f"Capture finished. Frame counts: {counts}")
    return frame_dirs
