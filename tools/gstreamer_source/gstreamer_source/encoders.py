"""H.264 encoder discovery, per-platform preference and configuration.

Mirrors the encoder table and settings used by the hybrid-bridge C++ publisher
so that streams produced here look like the real pipeline's output:
constrained-baseline, one IDR per second, no B-frames, SPS/PPS before IDRs.
"""

from __future__ import annotations

import glob
import logging
from dataclasses import dataclass

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst

from .platform import Platform

log = logging.getLogger(__name__)

PROFILES = ("baseline", "main", "high")


class EncoderError(RuntimeError):
    pass


@dataclass(frozen=True)
class EncoderSpec:
    key: str
    elements: tuple[str, ...]  # candidate factory names, first available wins
    description: str
    needs_nvmm: bool = False  # input must live in NVMM memory (Jetson)


ENCODERS: tuple[EncoderSpec, ...] = (
    EncoderSpec("x264", ("x264enc",), "software (libx264)"),
    EncoderSpec("vtenc", ("vtenc_h264_hw", "vtenc_h264"), "Apple VideoToolbox"),
    EncoderSpec("nvv4l2", ("nvv4l2h264enc",), "NVIDIA Jetson NVENC", needs_nvmm=True),
    EncoderSpec("nvenc", ("nvh264enc", "nvautogpuh264enc"), "NVIDIA NVENC (nvcodec plugin)"),
    EncoderSpec("v4l2", ("v4l2h264enc",), "V4L2 stateful hardware encoder"),
    EncoderSpec("va", ("vah264enc",), "VA-API (va plugin)"),
    EncoderSpec("vaapi", ("vaapih264enc",), "VA-API (legacy gstreamer-vaapi)"),
)
ENCODER_KEYS = tuple(spec.key for spec in ENCODERS)

# Device nodes that exist only when the Jetson SoC actually has an NVENC block.
# The nvv4l2h264enc plugin is installed on every Jetson image, including the
# Orin Nano, which has no H.264 hardware encoder.
_NVENC_DEVICE_GLOBS = ("/dev/nvhost-msenc", "/dev/v4l2-nvenc", "/dev/nvhost-nvenc*")


@dataclass(frozen=True)
class EncoderStatus:
    spec: EncoderSpec
    element: str | None
    available: bool
    reason: str

    @property
    def key(self) -> str:
        return self.spec.key


def spec_for(key: str) -> EncoderSpec:
    for spec in ENCODERS:
        if spec.key == key:
            return spec
    raise EncoderError(f"unknown encoder '{key}' (use auto, {', '.join(ENCODER_KEYS)})")


def element_available(factory_name: str) -> bool:
    return Gst.ElementFactory.find(factory_name) is not None


def encoder_preference(plat: Platform) -> list[str]:
    """Auto-selection order: hardware first, x264 last."""
    if plat == Platform.MACOS:
        return ["vtenc", "x264"]
    if plat == Platform.JETSON:
        return ["nvv4l2", "x264"]
    if plat == Platform.LINUX:
        return ["nvenc", "v4l2", "va", "vaapi", "x264"]
    return ["x264"]


def _nvenc_hardware_present() -> bool:
    return any(glob.glob(pattern) for pattern in _NVENC_DEVICE_GLOBS)


def _probe_ready(factory_name: str) -> str | None:
    """Instantiate the element and take it to READY; return an error string on failure."""
    element = Gst.ElementFactory.make(factory_name, None)
    if element is None:
        return "element could not be instantiated"
    try:
        ret = element.set_state(Gst.State.READY)
        if ret == Gst.StateChangeReturn.FAILURE:
            return "element failed to reach READY (missing hardware or driver?)"
    finally:
        element.set_state(Gst.State.NULL)
    return None


def encoder_status(spec: EncoderSpec, plat: Platform) -> EncoderStatus:
    element = next((name for name in spec.elements if element_available(name)), None)
    if element is None:
        return EncoderStatus(
            spec, None, False, f"plugin not installed ({'/'.join(spec.elements)})"
        )

    if (
        spec.key == "nvv4l2"
        and plat == Platform.JETSON
        and not _nvenc_hardware_present()
    ):
        return EncoderStatus(
            spec,
            element,
            False,
            "plugin installed but no NVENC device node found; this Jetson "
            "(e.g. Orin Nano) has no H.264 hardware encoder",
        )

    error = _probe_ready(element)
    if error:
        return EncoderStatus(spec, element, False, error)
    return EncoderStatus(spec, element, True, "ok")


def list_encoders(plat: Platform) -> list[EncoderStatus]:
    """All known encoders, preferred ones for this platform first."""
    order = encoder_preference(plat)
    keys = order + [k for k in ENCODER_KEYS if k not in order]
    return [encoder_status(spec_for(k), plat) for k in keys]


def resolve_encoder(requested: str, plat: Platform) -> EncoderStatus:
    """Pick the encoder to use. 'auto' takes the first available preferred one."""
    if requested in ("", "auto"):
        tried: list[str] = []
        for key in encoder_preference(plat):
            status = encoder_status(spec_for(key), plat)
            if status.available:
                return status
            tried.append(f"{key}: {status.reason}")
            log.debug("encoder %s unavailable: %s", key, status.reason)
        raise EncoderError("no usable H.264 encoder found:\n  " + "\n  ".join(tried))

    status = encoder_status(spec_for(requested), plat)
    if not status.available:
        raise EncoderError(f"encoder '{requested}' is not usable here: {status.reason}")
    return status


def _set(element: Gst.Element, name: str, value: object) -> None:
    """Set a property if the element has it; strings go through GStreamer's parser
    so enum/flags nicknames ("ultrafast", "zerolatency") and structures work."""
    if element.find_property(name) is None:
        log.debug("%s has no property '%s'; skipping", element.get_name(), name)
        return
    if isinstance(value, bool):
        Gst.util_set_object_arg(element, name, "true" if value else "false")
    else:
        Gst.util_set_object_arg(element, name, str(value))


def configure_encoder(
    status: EncoderStatus,
    element: Gst.Element,
    *,
    fps: int,
    bitrate_kbps: int,
    profile: str,
    stream_format: str,
    threads: int | None = None,
    sliced_threads: bool = False,
) -> str | None:
    """Apply low-latency settings; return a caps string to pin after the encoder
    (used by encoders that take the profile from caps), or None.

    ``threads``/``sliced_threads`` only affect x264. Sliced threads split every
    frame into one slice per thread (several VCL NALs per access unit), which
    cuts encode latency but breaks consumers that assume one VCL NAL per frame
    (the LiveKit Go SDK reader used by livekit-cli paces and packetizes each
    VCL NAL as a whole frame -> slow, partially green video). Off by default.
    Without sliced threads x264 frame-threads instead, which delays output by
    ``threads - 1`` frames (~100 ms at 30 fps with 4 threads), so ``threads``
    defaults to 1 in that mode and to 4 with sliced threads."""
    if profile not in PROFILES:
        raise EncoderError(f"unknown profile '{profile}' (use {', '.join(PROFILES)})")

    key = status.key
    caps_profile = "constrained-baseline" if profile == "baseline" else profile

    if key == "x264":
        _set(element, "speed-preset", "ultrafast")
        _set(element, "tune", "zerolatency")
        _set(element, "bitrate", bitrate_kbps)
        _set(element, "key-int-max", fps)
        _set(element, "bframes", 0)
        if threads is None:
            threads = 4 if sliced_threads else 1
        _set(element, "threads", threads)
        _set(element, "sliced-threads", sliced_threads)
        _set(element, "byte-stream", stream_format == "byte-stream")
        return f"video/x-h264,profile={caps_profile}"

    if key == "vtenc":
        _set(element, "realtime", True)
        _set(element, "allow-frame-reordering", False)
        _set(element, "bitrate", bitrate_kbps)
        _set(element, "max-keyframe-interval", fps)
        # VideoToolbox only negotiates "baseline", not "constrained-baseline".
        return f"video/x-h264,profile={profile}"

    if key == "nvv4l2":
        nv_profile = {"baseline": 0, "main": 2, "high": 4}[profile]
        _set(element, "bitrate", bitrate_kbps * 1000)
        _set(element, "control-rate", 1)  # CBR
        _set(element, "preset-level", 1)  # UltraFast
        _set(element, "profile", nv_profile)
        _set(element, "iframeinterval", fps)
        _set(element, "idrinterval", fps)
        _set(element, "insert-sps-pps", True)
        _set(element, "maxperf-enable", True)
        return None

    if key == "nvenc":
        # Desktop/datacenter NVENC via the nvcodec plugin (gst-plugins-bad).
        # Takes system-memory NV12 directly and picks the profile from caps.
        _set(element, "preset", "p1")
        _set(element, "tune", "ultra-low-latency")
        _set(element, "rc-mode", "cbr")
        _set(element, "bitrate", bitrate_kbps)
        _set(element, "gop-size", fps)
        _set(element, "bframes", 0)
        _set(element, "zerolatency", True)
        _set(element, "repeat-sequence-header", True)
        return f"video/x-h264,profile={caps_profile}"

    if key == "v4l2":
        v4l2_profile = {"baseline": 0, "main": 2, "high": 4}[profile]
        controls = (
            f"controls,video_bitrate={bitrate_kbps * 1000},"
            f"h264_profile={v4l2_profile},h264_i_frame_period={fps},"
            "repeat_sequence_header=1"
        )
        _set(element, "extra-controls", controls)
        return None

    if key == "va":
        _set(element, "rate-control", "cbr")
        _set(element, "bitrate", bitrate_kbps)
        _set(element, "key-int-max", fps)
        _set(element, "b-frames", 0)
        return f"video/x-h264,profile={caps_profile}"

    if key == "vaapi":
        _set(element, "rate-control", "cbr")
        _set(element, "bitrate", bitrate_kbps)
        _set(element, "keyframe-period", fps)
        _set(element, "max-bframes", 0)
        return f"video/x-h264,profile={caps_profile}"

    raise EncoderError(f"no configuration for encoder '{key}'")
