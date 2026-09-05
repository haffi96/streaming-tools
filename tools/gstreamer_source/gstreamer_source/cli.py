"""Command-line entry point for gstreamer-source."""

from __future__ import annotations

import argparse
import logging
import sys

import gi

gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst

from .cameras import CameraError, enumerate_cameras, format_camera_list, select_camera
from .encoders import (
    ENCODER_KEYS,
    PROFILES,
    EncoderError,
    encoder_preference,
    list_encoders,
)
from .pipeline import BuiltPipeline, PipelineConfig, PipelineError, build_pipeline
from .platform import platform_info

log = logging.getLogger("gstreamer_source")


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="gstreamer-source",
        description=(
            "Camera / test-pattern source producing H.264 (byte-stream or avc) or raw "
            "NV12 over TCP or into a file, with SEI timestamp metadata."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    info = p.add_argument_group("discovery")
    info.add_argument(
        "--list-cameras", action="store_true", help="list detected cameras and exit"
    )
    info.add_argument(
        "--list-encoders", action="store_true", help="list H.264 encoders and exit"
    )

    src = p.add_argument_group("source")
    src.add_argument(
        "--camera",
        nargs="?",
        const="",
        default=None,
        metavar="INDEX|DEVICE|NAME",
        help="use a camera instead of the test pattern; with no value, prompt "
        "(or auto-pick the only camera)",
    )
    src.add_argument(
        "--pattern", default="ball", help="videotestsrc pattern for the test source"
    )
    src.add_argument("--width", type=int, default=1280)
    src.add_argument("--height", type=int, default=720)
    src.add_argument("--fps", type=int, default=30)

    enc = p.add_argument_group("encoding")
    enc.add_argument(
        "--codec",
        choices=["h264", "nv12"],
        default="h264",
        help="h264 or raw NV12 frames",
    )
    enc.add_argument(
        "--stream-format",
        choices=["byte-stream", "avc"],
        default="byte-stream",
        help="H.264 framing: Annex B start codes or 4-byte length prefixes",
    )
    enc.add_argument(
        "--encoder",
        choices=["auto", *ENCODER_KEYS],
        default="auto",
        help="H.264 encoder; auto picks the best available for the platform",
    )
    enc.add_argument(
        "--bitrate", type=int, default=2000, metavar="KBPS", help="target bitrate"
    )
    enc.add_argument(
        "--profile", choices=list(PROFILES), default="baseline", help="H.264 profile"
    )
    enc.add_argument(
        "--threads",
        type=int,
        default=4,
        metavar="N",
        help="x264 encoder threads (default 4; 1 avoids frame-threading delay)",
    )
    enc.add_argument(
        "--sliced-threads",
        "--sliced_threads",
        dest="sliced_threads",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="x264: encode each frame as one slice per thread (lowest encode "
        "latency, several VCL NALs per frame). Fine for the hybrid-bridge "
        "passthrough; breaks livekit-cli, which treats every VCL NAL as a "
        "frame. Default off: one slice per frame",
    )
    enc.add_argument(
        "--sei-metadata",
        "--sei_metadata",
        dest="sei_metadata",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="inject SEI timestamp/frame-id NAL before every access unit (h264 only)",
    )

    out = p.add_argument_group("output")
    out.add_argument("--output", choices=["tcp", "file"], default="tcp")
    out.add_argument("--bind", default="0.0.0.0", help="TCP listen address")
    out.add_argument("--port", type=int, default=5004, help="TCP listen port")
    out.add_argument(
        "--path",
        default=None,
        help="output file (file output; default depends on codec)",
    )
    out.add_argument(
        "--duration",
        type=float,
        default=None,
        metavar="SECONDS",
        help="send EOS and exit after this long (default: run until Ctrl+C)",
    )
    p.add_argument(
        "--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"]
    )
    return p


def _print_encoders(plat) -> None:
    preferred = encoder_preference(plat)
    print(f"H.264 encoders (auto order for {plat.value}: {' > '.join(preferred)}):")
    for status in list_encoders(plat):
        mark = "yes" if status.available else "no "
        element = status.element or "/".join(status.spec.elements)
        print(f"  [{mark}] {status.key:<7} {element:<16} {status.spec.description}")
        if not status.available:
            print(f"        {status.reason}")


def _run(built: BuiltPipeline, duration: float | None) -> int:
    loop = GLib.MainLoop()
    exit_code = 0
    bus = built.pipeline.get_bus()
    bus.add_signal_watch()

    def on_message(_bus, msg):
        nonlocal exit_code
        if msg.type == Gst.MessageType.EOS:
            log.info("EOS")
            loop.quit()
        elif msg.type == Gst.MessageType.ERROR:
            err, debug = msg.parse_error()
            log.error("%s", err.message)
            if debug:
                log.debug("%s", debug)
            exit_code = 1
            loop.quit()
        elif msg.type == Gst.MessageType.WARNING:
            warn, _ = msg.parse_warning()
            log.warning("%s", warn.message)

    bus.connect("message", on_message)

    if duration:

        def send_eos():
            log.info("Duration reached, sending EOS")
            built.pipeline.send_event(Gst.Event.new_eos())
            return False

        GLib.timeout_add(int(duration * 1000), send_eos)

    ret = built.pipeline.set_state(Gst.State.PLAYING)
    if ret == Gst.StateChangeReturn.FAILURE:
        log.error("pipeline refused to start")
        built.pipeline.set_state(Gst.State.NULL)
        return 1
    log.info("Running. Press Ctrl+C to stop.")
    try:
        loop.run()
    except KeyboardInterrupt:
        log.info("Interrupted")
    finally:
        built.pipeline.set_state(Gst.State.NULL)
        if built.sei_injector:
            log.info("Injected SEI into %d frames", built.sei_injector.frame_count)
    return exit_code


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    logging.basicConfig(
        level=args.log_level,
        format="[%(asctime)s] | %(levelname)-8s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    Gst.init(None)
    info = platform_info()
    plat = info.platform
    log.info("Platform: %s", info.describe())

    if args.list_cameras or args.list_encoders:
        if args.list_cameras:
            print(format_camera_list(enumerate_cameras(plat)))
        if args.list_encoders:
            _print_encoders(plat)
        return 0

    if args.fps < 1 or args.width < 1 or args.height < 1:
        log.error("--fps, --width and --height must be >= 1")
        return 2
    if not 1 <= args.port <= 65535:
        log.error("--port must be within 1..65535")
        return 2

    path = args.path
    if args.output == "file" and not path:
        path = "output_sei.h264" if args.codec == "h264" else "output.nv12"

    camera = None
    if args.camera is not None:
        try:
            camera = select_camera(enumerate_cameras(plat), args.camera)
        except CameraError as exc:
            log.error("%s", exc)
            return 1
        log.info("Source: camera %s", camera.describe())
    else:
        log.info("Source: videotestsrc pattern=%s", args.pattern)

    cfg = PipelineConfig(
        output=args.output,
        path=path,
        bind=args.bind,
        port=args.port,
        codec=args.codec,
        stream_format=args.stream_format,
        encoder=args.encoder,
        bitrate_kbps=args.bitrate,
        profile=args.profile,
        threads=args.threads,
        sliced_threads=args.sliced_threads,
        width=args.width,
        height=args.height,
        fps=args.fps,
        pattern=args.pattern,
        sei_metadata=args.sei_metadata,
        camera=camera,
    )

    try:
        built = build_pipeline(cfg, plat)
    except (EncoderError, PipelineError) as exc:
        log.error("%s", exc)
        return 1

    log.info("Video: %dx%d@%d", cfg.width, cfg.height, cfg.fps)
    if built.encoder:
        log.info(
            "Encoder: %s (%s) %s profile, %d kbps, %s, SEI %s%s",
            built.encoder.key,
            built.encoder.element,
            cfg.profile,
            cfg.bitrate_kbps,
            cfg.stream_format,
            "on" if built.sei_injector else "off",
            (
                f", threads={cfg.threads} "
                + (
                    "sliced (multi-slice frames)"
                    if cfg.sliced_threads
                    else "single-slice frames"
                )
                if built.encoder.key == "x264"
                else ""
            ),
        )
    else:
        log.info("Codec: raw NV12")
    if cfg.output == "tcp":
        log.info("Output: tcp://%s:%d", cfg.bind, cfg.port)
    else:
        log.info("Output: file %s", cfg.path)
    log.info("Pipeline: %s", built.describe())

    return _run(built, args.duration)


if __name__ == "__main__":
    sys.exit(main())
