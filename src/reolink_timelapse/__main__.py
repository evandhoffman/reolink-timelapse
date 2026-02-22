import argparse
import asyncio
import logging
import signal
import sys
from datetime import datetime, timedelta

from reolink_timelapse.capture import run_capture
from reolink_timelapse.config import Settings
from reolink_timelapse.nvr import ReolinkNVR
from reolink_timelapse.stitch import run_stitch

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


async def cmd_capture(settings: Settings) -> None:
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop_event.set)

    end_time = datetime.now() + timedelta(hours=settings.duration_hours)
    total_frames_est = int(settings.duration_hours * 3600 / settings.capture_interval)
    storage_lo = total_frames_est * 0.3   # MB (compressed 1080p)
    storage_hi = total_frames_est * 4.0   # MB (4K)

    logger.info(
        f"=== Capture starting ===\n"
        f"  NVR          : {settings.nvr_host}\n"
        f"  Interval     : {settings.capture_interval}s\n"
        f"  Duration     : {settings.duration_hours}h  (until {end_time:%Y-%m-%d %H:%M:%S})\n"
        f"  Data dir     : {settings.data_dir}\n"
        f"  Est. frames  : ~{total_frames_est:,} per camera\n"
        f"  Est. storage : ~{storage_lo/1024:.1f}–{storage_hi/1024:.1f} GB per camera"
    )

    async with ReolinkNVR(
        settings.nvr_host, settings.nvr_username, settings.nvr_password
    ) as nvr:
        channels = await nvr.get_online_channels()
        if not channels:
            logger.error("No online cameras found — check NVR connection and credentials.")
            sys.exit(1)

        logger.info(f"Found {len(channels)} camera(s)")

        async def _auto_stop() -> None:
            await asyncio.sleep(settings.duration_hours * 3600)
            logger.info("Capture duration reached — stopping.")
            stop_event.set()

        stop_task = asyncio.create_task(_auto_stop())
        await run_capture(nvr, channels, settings.data_dir, settings.capture_interval, stop_event)
        stop_task.cancel()

    logger.info("Capture complete. Run 'stitch' to encode videos.")


async def cmd_test(settings: Settings) -> None:
    print(f"Connecting to NVR at {settings.nvr_host} ...")
    try:
        async with ReolinkNVR(
            settings.nvr_host, settings.nvr_username, settings.nvr_password
        ) as nvr:
            channels = await nvr.get_online_channels()
            print(f"\n✓ Login successful\n")
            if not channels:
                print("  No online cameras found.")
            else:
                print(f"  {'Ch':>3}  {'Name':<20}  {'Resolution':<12}  {'FPS'}")
                print(f"  {'--':>3}  {'----':<20}  {'----------':<12}  {'---'}")
                for ch in channels:
                    print(
                        f"  {ch['channel']:>3}  {ch.get('name', '?'):<20}"
                        f"  {ch.get('resolution', '?'):<12}  {ch.get('fps', '?')}"
                    )
                print(f"\n  {len(channels)} camera(s) online")
    except Exception as exc:
        print(f"\n✗ Connection failed: {exc}")
        sys.exit(1)


async def cmd_stitch(settings: Settings, sample_rate: int, output_fps: int) -> None:
    logger.info(
        f"=== Stitch starting ===\n"
        f"  Data dir     : {settings.data_dir}\n"
        f"  Sample rate  : every {sample_rate} frame(s)\n"
        f"  Output FPS   : {output_fps}"
    )
    await run_stitch(settings.data_dir, sample_rate, output_fps)
    logger.info("Stitch complete.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Reolink NVR timelapse tool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Modes:
  capture   Pull snapshots from all online cameras and save to disk.
            Runs for DURATION_HOURS then exits (or until SIGTERM).

  test      Verify NVR connectivity and list online cameras.

  stitch    Read saved frames and encode an MP4 per camera.
            Can be run while capture is still running.

Examples:
  # Verify credentials and see what cameras are available
  docker compose run --rm timelapse test

  # Capture for 18 hours at one frame every 15 seconds
  docker compose run timelapse capture

  # Encode: use every 4th frame at 24 fps
  docker compose run timelapse stitch --sample-rate 4 --fps 24
""",
    )
    subparsers = parser.add_subparsers(dest="mode", required=True)

    subparsers.add_parser("test", help="Test NVR connectivity and list cameras")
    subparsers.add_parser("capture", help="Capture snapshots from NVR")

    sp_stitch = subparsers.add_parser("stitch", help="Encode captured frames into MP4")
    sp_stitch.add_argument(
        "--sample-rate",
        type=int,
        default=None,
        metavar="N",
        help="Use every Nth captured frame (default: SAMPLE_RATE env var, then 1)",
    )
    sp_stitch.add_argument(
        "--fps",
        type=int,
        default=None,
        metavar="FPS",
        help="Output video frame rate (default: OUTPUT_FPS env var, then 24)",
    )

    args = parser.parse_args()
    settings = Settings()

    if args.mode == "test":
        asyncio.run(cmd_test(settings))
    elif args.mode == "capture":
        asyncio.run(cmd_capture(settings))
    elif args.mode == "stitch":
        sample_rate = args.sample_rate if args.sample_rate is not None else settings.sample_rate
        output_fps = args.fps if args.fps is not None else settings.output_fps
        asyncio.run(cmd_stitch(settings, sample_rate, output_fps))


if __name__ == "__main__":
    main()
