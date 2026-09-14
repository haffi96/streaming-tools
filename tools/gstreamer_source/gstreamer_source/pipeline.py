"""Pipeline construction.

Shape:
  source -> NV12 caps -> leaky 1-buffer queue -> [textoverlay (--timestamps)]
         -> [nvvidconv] -> encoder -> [profile caps] -> h264parse
         -> stream-format caps (SEI / AU-terminator probe) -> tcpserversink | filesink

With --codec nv12 the encoder stage is skipped and raw NV12 frames go to the sink.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst

from .cameras import Camera
from .encoders import EncoderStatus, configure_encoder, resolve_encoder
from .overlay import TimestampOverlay
from .platform import Platform
from .sei import SeiInjector

log = logging.getLogger(__name__)


class PipelineError(RuntimeError):
    pass


@dataclass
class PipelineConfig:
    output: str = "tcp"  # tcp | file
    path: str | None = None
    bind: str = "0.0.0.0"
    port: int = 5004
    codec: str = "h264"  # h264 | nv12
    stream_format: str = "byte-stream"  # byte-stream | avc
    encoder: str = "auto"
    bitrate_kbps: int = 2000
    profile: str = "baseline"
    threads: int | None = None  # x264 only; None -> 4 if sliced_threads else 1
    sliced_threads: bool = False  # x264 only; True -> multiple slices per frame
    width: int = 1280
    height: int = 720
    fps: int = 30
    pattern: str = "ball"
    sei_metadata: bool = True
    # Append an AUD NAL to every access unit so a byte-stream receiver can
    # complete the frame without waiting for the next one (see sei.py).
    au_terminator: bool = True
    timestamps: bool = False  # burn a running millisecond clock into the frames
    # Generate test-pattern frames on the wall-clock frame grid (frame k at
    # k / fps seconds since the Unix epoch) and snap SEI timestamps to it, so
    # several instances started at different times emit identical timestamps.
    align_frames: bool = False
    camera: Camera | None = None
    camera_format: str = "auto"  # auto | raw | mjpeg (v4l2 cameras only)


@dataclass
class BuiltPipeline:
    pipeline: Gst.Pipeline
    sei_injector: SeiInjector | None
    encoder: EncoderStatus | None
    chain: list[str]
    timestamp_overlay: TimestampOverlay | None = None
    align_fps: int | None = None  # set with --align-frames; see align_start_time

    def describe(self) -> str:
        return " ! ".join(self.chain)


# How far ahead of "now" the aligned frame 0 is placed, to cover the start-up
# of the pipeline. If start-up takes longer the first frames are released late
# but keep their exact grid timestamps.
ALIGN_LEAD_NS = 300_000_000


def align_start_time(pipeline: Gst.Pipeline, fps: int) -> int:
    """Pin the pipeline to CLOCK_REALTIME and set its base time to an upcoming
    wall-clock frame instant.

    In --align-frames mode the test source runs non-live, so frame n carries
    PTS n / fps exactly, and a clocksync element releases it at base_time +
    PTS, i.e. at (k + n) / fps seconds since the Unix epoch for an integer k.
    (A live videotestsrc cannot be used: it stamps its first frame with
    whatever running time its thread starts at, which fixes an arbitrary
    phase.) Call before setting the pipeline to PLAYING; returns the base
    time in Unix ns.
    """
    clock = Gst.SystemClock.obtain()
    clock.set_property("clock-type", Gst.ClockType.REALTIME)
    pipeline.use_clock(clock)
    # Stop GstPipeline from recomputing base_time on PAUSED -> PLAYING.
    pipeline.set_start_time(Gst.CLOCK_TIME_NONE)
    now_ns = time.time_ns() + ALIGN_LEAD_NS
    k = -(-now_ns * fps // Gst.SECOND)  # ceil
    base_time = k * Gst.SECOND // fps
    pipeline.set_base_time(base_time)
    return base_time


class _Chain:
    """Ordered list of elements that get added and linked in sequence."""

    def __init__(self, pipeline: Gst.Pipeline):
        self.pipeline = pipeline
        self.elements: list[Gst.Element] = []
        self.chain: list[str] = []

    def make(
        self,
        factory: str,
        props: dict[str, object] | None = None,
        label: str | None = None,
    ) -> Gst.Element:
        element = Gst.ElementFactory.make(factory, None)
        if element is None:
            raise PipelineError(f"GStreamer element '{factory}' is not available")
        for name, value in (props or {}).items():
            if element.find_property(name) is None:
                log.debug("%s has no property '%s'; skipping", factory, name)
                continue
            if isinstance(value, bool):
                Gst.util_set_object_arg(element, name, "true" if value else "false")
            else:
                Gst.util_set_object_arg(element, name, str(value))
        self.pipeline.add(element)
        self.elements.append(element)
        self.chain.append(label or factory)
        return element

    def caps(self, caps_str: str) -> Gst.Element:
        element = Gst.ElementFactory.make("capsfilter", None)
        if element is None:
            raise PipelineError("GStreamer element 'capsfilter' is not available")
        element.set_property("caps", Gst.Caps.from_string(caps_str))
        self.pipeline.add(element)
        self.elements.append(element)
        self.chain.append(caps_str)
        return element

    def link(self) -> None:
        for a, b in zip(self.elements, self.elements[1:]):
            if not a.link(b):
                raise PipelineError(
                    f"failed to link {a.get_factory().get_name()} -> {b.get_factory().get_name()}"
                )


def build_pipeline(cfg: PipelineConfig, plat: Platform) -> BuiltPipeline:
    pipeline = Gst.Pipeline.new("gstreamer-source")
    chain = _Chain(pipeline)
    geometry = f"width={cfg.width},height={cfg.height},framerate={cfg.fps}/1"
    raw_caps = f"video/x-raw,format=NV12,{geometry}"
    nvmm_caps = f"video/x-raw(memory:NVMM),format=NV12,{geometry}"
    nvmm = False  # True while buffers live in NVMM (Jetson)

    # ---- source ----
    cam = cfg.camera
    if cfg.align_frames and cam is not None:
        raise PipelineError(
            "--align-frames schedules test-pattern frames on the wall clock and "
            "cannot be used with a camera (its capture times are its own)"
        )
    if cam is None:
        chain.make(
            "videotestsrc",
            {"pattern": cfg.pattern, "is-live": not cfg.align_frames},
        )
        chain.caps(raw_caps)
        if cfg.align_frames:
            # Non-live source: PTS is exactly n / fps. clocksync holds every
            # frame until base_time + PTS on the (wall) pipeline clock.
            chain.make("clocksync")
    elif cam.kind == "csi":
        chain.make(cam.element, dict(cam.properties))
        chain.caps(nvmm_caps)
        nvmm = True
    else:
        props: dict[str, object] = dict(cam.properties)
        props["do-timestamp"] = True
        chain.make(cam.element, props)
        has_mjpeg = any(f in ("MJPG", "JPEG") for f in cam.formats)
        if cfg.camera_format == "mjpeg" and not has_mjpeg and cam.formats:
            raise PipelineError(
                f"camera {cam.name} ({cam.path}) has no MJPEG format "
                f"(offers {','.join(cam.formats)})"
            )
        if cfg.camera_format == "raw" and cam.compressed_only:
            raise PipelineError(
                f"camera {cam.name} ({cam.path}) only offers compressed formats "
                f"{','.join(cam.formats)}"
            )
        if cam.compressed_only and not has_mjpeg:
            raise PipelineError(
                f"camera {cam.name} ({cam.path}) only offers compressed formats "
                f"{','.join(cam.formats)}; this tool re-encodes raw frames, so pick "
                "a node with a raw or MJPEG format (see --list-cameras)"
            )
        # UVC cameras usually reach 720p/1080p at 30 fps only via MJPEG; their
        # raw modes are small or slow. Prefer MJPEG unless told otherwise.
        use_mjpeg = cfg.camera_format == "mjpeg" or (
            cfg.camera_format == "auto" and has_mjpeg
        )
        if use_mjpeg:
            chain.caps(f"image/jpeg,{geometry}")
            chain.make("jpegdec")
        chain.make("videoconvert")
        chain.make("videorate", {"drop-only": True, "skip-to-first": True})
        chain.make("videoscale")
        chain.caps(raw_caps)

    # Keep only the newest frame between capture and encoder.
    chain.make(
        "queue",
        {
            "leaky": "downstream",
            "max-size-buffers": 1,
            "max-size-time": 0,
            "max-size-bytes": 0,
        },
    )

    # ---- timestamp overlay ----
    timestamp_overlay: TimestampOverlay | None = None
    if cfg.timestamps:
        if nvmm:
            # textoverlay blends into system memory; the encoder stage below
            # converts back to NVMM if it needs it.
            chain.make("nvvidconv")
            chain.caps(raw_caps)
            nvmm = False
        overlay = chain.make(
            "textoverlay",
            {
                "text": "0:00:00.000",
                "font-desc": f"Monospace Bold {max(12, cfg.height // 20)}",
                "valignment": "top",
                "halignment": "left",
                "xpad": 16,
                "ypad": 16,
                "shaded-background": True,
            },
            label="textoverlay",
        )
        timestamp_overlay = TimestampOverlay(overlay)

    # ---- codec ----
    sei_pad_owner: Gst.Element | None = None
    encoder_status: EncoderStatus | None = None

    if cfg.codec == "nv12":
        if nvmm:
            chain.make("nvvidconv")
            chain.caps(raw_caps)
    else:
        encoder_status = resolve_encoder(cfg.encoder, plat)
        if encoder_status.spec.needs_nvmm and not nvmm:
            chain.make("nvvidconv")
            chain.caps(nvmm_caps)
        elif nvmm and not encoder_status.spec.needs_nvmm:
            chain.make("nvvidconv")
            chain.caps(raw_caps)

        assert encoder_status.element is not None
        encoder = chain.make(encoder_status.element, label=encoder_status.element)
        profile_caps = configure_encoder(
            encoder_status,
            encoder,
            fps=cfg.fps,
            bitrate_kbps=cfg.bitrate_kbps,
            profile=cfg.profile,
            stream_format=cfg.stream_format,
            threads=cfg.threads,
            sliced_threads=cfg.sliced_threads,
            # With the terminator every AU already ends in an AUD; x264's own
            # leading AUD would make it two per frame.
            aud=not cfg.au_terminator,
        )
        if profile_caps:
            chain.caps(profile_caps)

        # config-interval=-1: SPS/PPS before every IDR, only if the encoder
        # did not already emit them.
        chain.make("h264parse", {"config-interval": -1})
        sei_pad_owner = chain.caps(
            f"video/x-h264,stream-format={cfg.stream_format},alignment=au"
        )

    # ---- sink ----
    if cfg.output == "file":
        if not cfg.path:
            raise PipelineError("file output requires --path")
        chain.make("filesink", {"location": cfg.path, "sync": False})
    elif cfg.output == "tcp":
        chain.make("tcpserversink", {"host": cfg.bind, "port": cfg.port, "sync": False})
    else:
        raise PipelineError(f"unknown output '{cfg.output}'")

    chain.link()

    if timestamp_overlay is not None:
        timestamp_overlay.attach()

    sei_injector: SeiInjector | None = None
    if (
        cfg.codec == "h264"
        and (cfg.sei_metadata or cfg.au_terminator)
        and sei_pad_owner is not None
    ):
        src_pad = sei_pad_owner.get_static_pad("src")
        if src_pad is None:
            raise PipelineError("could not get pad for SEI injection")
        sei_injector = SeiInjector(
            cfg.stream_format,
            sei_metadata=cfg.sei_metadata,
            au_terminator=cfg.au_terminator,
            grid_fps=cfg.fps if cfg.align_frames else None,
        )
        sei_injector.attach(src_pad)

    return BuiltPipeline(
        pipeline,
        sei_injector,
        encoder_status,
        chain.chain,
        timestamp_overlay,
        align_fps=cfg.fps if cfg.align_frames else None,
    )
