"""Pipeline construction.

Shape:
  source -> NV12 caps -> leaky 1-buffer queue
         -> [nvvidconv] -> encoder -> [profile caps] -> h264parse
         -> stream-format caps (SEI probe) -> tcpserversink | filesink

With --codec nv12 the encoder stage is skipped and raw NV12 frames go to the sink.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst

from .cameras import Camera
from .encoders import EncoderStatus, configure_encoder, resolve_encoder
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
    threads: int = 4  # x264 only
    sliced_threads: bool = False  # x264 only; True -> multiple slices per frame
    width: int = 1280
    height: int = 720
    fps: int = 30
    pattern: str = "ball"
    sei_metadata: bool = True
    camera: Camera | None = None


@dataclass
class BuiltPipeline:
    pipeline: Gst.Pipeline
    sei_injector: SeiInjector | None
    encoder: EncoderStatus | None
    chain: list[str]

    def describe(self) -> str:
        return " ! ".join(self.chain)


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
    if cam is None:
        chain.make("videotestsrc", {"pattern": cfg.pattern, "is-live": True})
        chain.caps(raw_caps)
    elif cam.kind == "csi":
        chain.make(cam.element, dict(cam.properties))
        chain.caps(nvmm_caps)
        nvmm = True
    else:
        props: dict[str, object] = dict(cam.properties)
        props["do-timestamp"] = True
        chain.make(cam.element, props)
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

    sei_injector: SeiInjector | None = None
    if cfg.codec == "h264" and cfg.sei_metadata and sei_pad_owner is not None:
        src_pad = sei_pad_owner.get_static_pad("src")
        if src_pad is None:
            raise PipelineError("could not get pad for SEI injection")
        sei_injector = SeiInjector(cfg.stream_format)
        sei_injector.attach(src_pad)

    return BuiltPipeline(pipeline, sei_injector, encoder_status, chain.chain)
