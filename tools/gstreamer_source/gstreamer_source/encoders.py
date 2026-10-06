"""H.264 / H.265 encoder discovery, per-platform preference and configuration.

Mirrors the encoder table and settings used by the hybrid-bridge C++ publisher
so that streams produced here look like the real pipeline's output:
constrained-baseline, one IDR per second, no B-frames, SPS/PPS before IDRs.
H.265 uses the same low-latency intent (Main profile, one IDR per second, no
frame reordering, VPS/SPS/PPS before IDRs).

Encoder keys name an encoder family ("vtenc", "nvenc", ...); each family has an
H.264 and usually an H.265 element. The software encoders are separate keys
(x264 for H.264, x265 for H.265).
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

CODECS = ("h264", "h265")
CODEC_LABELS = {"h264": "H.264", "h265": "H.265"}

# Profiles accepted per codec; the first one is the default. "baseline" means
# constrained-baseline for H.264.
H264_PROFILES = ("baseline", "main", "high")
H265_PROFILES = ("main",)
PROFILES_BY_CODEC = {"h264": H264_PROFILES, "h265": H265_PROFILES}
# Every --profile value, for the CLI choices.
PROFILES = tuple(dict.fromkeys(H264_PROFILES + H265_PROFILES))


class EncoderError(RuntimeError):
    pass


def default_profile(codec: str) -> str:
    return PROFILES_BY_CODEC[codec][0]


@dataclass(frozen=True)
class EncoderSpec:
    key: str
    elements: tuple[str, ...]  # candidate factory names, first available wins
    description: str
    needs_nvmm: bool = False  # input must live in NVMM memory (Jetson)
    codec: str = "h264"  # h264 | h265


ENCODERS: tuple[EncoderSpec, ...] = (
    EncoderSpec("x264", ("x264enc",), "software (libx264)"),
    EncoderSpec("vtenc", ("vtenc_h264_hw", "vtenc_h264"), "Apple VideoToolbox"),
    EncoderSpec("nvv4l2", ("nvv4l2h264enc",), "NVIDIA Jetson NVENC", needs_nvmm=True),
    EncoderSpec("nvenc", ("nvh264enc", "nvautogpuh264enc"), "NVIDIA NVENC (nvcodec plugin)"),
    EncoderSpec("v4l2", ("v4l2h264enc",), "V4L2 stateful hardware encoder"),
    EncoderSpec("va", ("vah264enc",), "VA-API (va plugin)"),
    EncoderSpec("vaapi", ("vaapih264enc",), "VA-API (legacy gstreamer-vaapi)"),
    EncoderSpec("x265", ("x265enc",), "software (libx265)", codec="h265"),
    EncoderSpec(
        "vtenc", ("vtenc_h265_hw", "vtenc_h265"), "Apple VideoToolbox", codec="h265"
    ),
    EncoderSpec(
        "nvv4l2",
        ("nvv4l2h265enc",),
        "NVIDIA Jetson NVENC",
        needs_nvmm=True,
        codec="h265",
    ),
    EncoderSpec(
        "nvenc",
        ("nvh265enc", "nvautogpuh265enc"),
        "NVIDIA NVENC (nvcodec plugin)",
        codec="h265",
    ),
    EncoderSpec(
        "v4l2", ("v4l2h265enc",), "V4L2 stateful hardware encoder", codec="h265"
    ),
    EncoderSpec("va", ("vah265enc",), "VA-API (va plugin)", codec="h265"),
    EncoderSpec(
        "vaapi", ("vaapih265enc",), "VA-API (legacy gstreamer-vaapi)", codec="h265"
    ),
)
# Every --encoder value across codecs, in table order.
ENCODER_KEYS = tuple(dict.fromkeys(spec.key for spec in ENCODERS))


def encoder_keys(codec: str = "h264") -> tuple[str, ...]:
    return tuple(spec.key for spec in ENCODERS if spec.codec == codec)


# Device nodes that exist only when the Jetson SoC actually has an NVENC block.
# The nvv4l2h264enc / nvv4l2h265enc plugins are installed on every Jetson
# image, including the Orin Nano, which has no video hardware encoder.
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


def spec_for(key: str, codec: str = "h264") -> EncoderSpec:
    for spec in ENCODERS:
        if spec.key == key and spec.codec == codec:
            return spec
    label = CODEC_LABELS.get(codec, codec)
    hint = ""
    if key == "x264" and codec == "h265":
        hint = "; x264 is H.264-only, use x265"
    elif key == "x265" and codec == "h264":
        hint = "; x265 is H.265-only, use x264"
    choices = ", ".join(encoder_keys(codec))
    raise EncoderError(f"no {label} encoder '{key}'{hint} (use auto, {choices})")


def element_available(factory_name: str) -> bool:
    return Gst.ElementFactory.find(factory_name) is not None


def encoder_preference(plat: Platform, codec: str = "h264") -> list[str]:
    """Auto-selection order: hardware first, the software encoder last."""
    software = "x265" if codec == "h265" else "x264"
    if plat == Platform.MACOS:
        return ["vtenc", software]
    if plat == Platform.JETSON:
        return ["nvv4l2", software]
    if plat == Platform.LINUX:
        return ["nvenc", "v4l2", "va", "vaapi", software]
    return [software]


def _nvenc_hardware_present() -> bool:
    return any(glob.glob(pattern) for pattern in _NVENC_DEVICE_GLOBS)


def _probe_ready(factory_name: str) -> str | None:
    """Instantiate the element and take it to READY; return an error string on failure."""
    element = Gst.ElementFactory.make(factory_name, None)
    if element is None:
        return "element could not be instantiated (plugin failed to load, e.g. missing library)"
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
            "(e.g. Orin Nano) has no H.264/H.265 hardware encoder",
        )

    error = _probe_ready(element)
    if error:
        return EncoderStatus(spec, element, False, error)
    return EncoderStatus(spec, element, True, "ok")


def list_encoders(plat: Platform, codec: str = "h264") -> list[EncoderStatus]:
    """All known encoders for `codec`, preferred ones for this platform first."""
    order = encoder_preference(plat, codec)
    keys = order + [k for k in encoder_keys(codec) if k not in order]
    return [encoder_status(spec_for(k, codec), plat) for k in keys]


def resolve_encoder(
    requested: str, plat: Platform, codec: str = "h264"
) -> EncoderStatus:
    """Pick the encoder to use. 'auto' takes the first available preferred one."""
    if requested in ("", "auto"):
        tried: list[str] = []
        for key in encoder_preference(plat, codec):
            status = encoder_status(spec_for(key, codec), plat)
            if status.available:
                return status
            tried.append(f"{key}: {status.reason}")
            log.debug("encoder %s unavailable: %s", key, status.reason)
        raise EncoderError(
            f"no usable {CODEC_LABELS.get(codec, codec)} encoder found:\n  "
            + "\n  ".join(tried)
        )

    status = encoder_status(spec_for(requested, codec), plat)
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
    aud: bool = True,
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
    defaults to 1 in that mode and to 4 with sliced threads.

    ``aud`` controls x264's own access unit delimiter at the front of every AU
    (its default); the pipeline turns it off when it terminates AUs itself.
    Hardware encoders do not emit AUDs.

    H.265 encoders are configured by ``_configure_h265`` (same contract)."""
    if status.spec.codec == "h265":
        return _configure_h265(
            status,
            element,
            fps=fps,
            bitrate_kbps=bitrate_kbps,
            profile=profile,
            aud=aud,
        )
    if profile not in H264_PROFILES:
        raise EncoderError(
            f"unknown H.264 profile '{profile}' (use {', '.join(H264_PROFILES)})"
        )

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
        _set(element, "aud", aud)
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


def _configure_h265(
    status: EncoderStatus,
    element: Gst.Element,
    *,
    fps: int,
    bitrate_kbps: int,
    profile: str,
    aud: bool,
) -> str | None:
    """H.265 counterpart of configure_encoder: Main profile, keyframe every
    ``fps`` frames, no B-frames / frame reordering, realtime rate control.

    ``aud``: x265 mirrors x264 (a leading AUD per AU unless the pipeline
    terminates AUs itself). Hardware encoders that can emit AUDs (nvv4l2
    ``insert-aud``, nvenc/va/vaapi ``aud``) get it switched off when the
    pipeline terminates AUs; otherwise their default is left alone."""
    if profile not in H265_PROFILES:
        raise EncoderError(
            f"profile '{profile}' is not an H.265 profile "
            f"(H.265 supports: {', '.join(H265_PROFILES)}; "
            f"{', '.join(p for p in H264_PROFILES if p not in H265_PROFILES)} "
            "are H.264-only)"
        )

    key = status.key
    caps = f"video/x-h265,profile={profile}"

    if key == "x265":
        # x265enc's element properties override option-string. zerolatency
        # already disables B-frames, lookahead and frame threading; open-gop=0
        # makes every keyframe an IDR rather than a CRA.
        _set(element, "speed-preset", "ultrafast")
        _set(element, "tune", "zerolatency")
        _set(element, "bitrate", bitrate_kbps)
        _set(element, "key-int-max", fps)
        _set(element, "option-string", f"bframes=0:open-gop=0:aud={int(aud)}")
        return caps

    if key == "vtenc":
        _set(element, "realtime", True)
        _set(element, "allow-frame-reordering", False)
        _set(element, "bitrate", bitrate_kbps)
        _set(element, "max-keyframe-interval", fps)
        return caps

    if key == "nvv4l2":
        _set(element, "bitrate", bitrate_kbps * 1000)
        _set(element, "control-rate", 1)  # CBR
        _set(element, "preset-level", 1)  # UltraFast
        _set(element, "profile", 0)  # Main
        _set(element, "iframeinterval", fps)
        _set(element, "idrinterval", fps)
        _set(element, "insert-sps-pps", True)
        _set(element, "maxperf-enable", True)
        if not aud:
            _set(element, "insert-aud", False)
        return None

    if key == "nvenc":
        _set(element, "preset", "p1")
        _set(element, "tune", "ultra-low-latency")
        _set(element, "rc-mode", "cbr")
        _set(element, "bitrate", bitrate_kbps)
        _set(element, "gop-size", fps)
        _set(element, "bframes", 0)
        _set(element, "zerolatency", True)
        _set(element, "repeat-sequence-header", True)
        if not aud:
            _set(element, "aud", False)
        return caps

    if key == "v4l2":
        controls = (
            f"controls,video_bitrate={bitrate_kbps * 1000},"
            f"hevc_profile=0,video_gop_size={fps},"
            "repeat_sequence_header=1"
        )
        _set(element, "extra-controls", controls)
        return None

    if key == "va":
        _set(element, "rate-control", "cbr")
        _set(element, "bitrate", bitrate_kbps)
        _set(element, "key-int-max", fps)
        _set(element, "b-frames", 0)
        if not aud:
            _set(element, "aud", False)
        return caps

    if key == "vaapi":
        _set(element, "rate-control", "cbr")
        _set(element, "bitrate", bitrate_kbps)
        _set(element, "keyframe-period", fps)
        _set(element, "max-bframes", 0)
        if not aud:
            _set(element, "aud", False)
        return caps

    raise EncoderError(f"no H.265 configuration for encoder '{key}'")
