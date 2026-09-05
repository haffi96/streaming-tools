"""Camera discovery and selection.

macOS: GstDeviceMonitor (avfvideosrc).
Linux / Jetson: every /dev/video* node is queried directly with the V4L2
QUERYCAP / ENUM_FMT ioctls. GstDeviceMonitor is not used there because on
Jetson it is PipeWire-backed and hides the CSI sensor node (raw Bayer,
tegra-video driver) while listing the same UVC camera several times.
Capture nodes owned by the tegra-video driver are CSI sensors and are mapped
to nvarguscamerasrc (sensor-id in node order); metadata-only nodes are
skipped; the remaining nodes become v4l2src cameras annotated with their
pixel formats.
"""

from __future__ import annotations

import array
import fcntl
import glob
import logging
import os
import struct
import sys
from dataclasses import dataclass, field

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst

from .platform import Platform

log = logging.getLogger(__name__)


class CameraError(RuntimeError):
    pass


COMPRESSED_FORMATS = frozenset(
    {"MJPG", "JPEG", "H264", "H265", "HEVC", "VP80", "VP90", "AV01"}
)


@dataclass
class Camera:
    index: int
    name: str
    kind: str  # "avf" | "v4l2" | "csi"
    element: str
    properties: dict[str, object] = field(default_factory=dict)
    formats: tuple[str, ...] = ()  # V4L2 fourccs, empty when unknown
    path: str | None = None  # /dev/video* node (Linux)

    @property
    def location(self) -> str:
        return ", ".join(f"{k}={v}" for k, v in self.properties.items())

    @property
    def raw_formats(self) -> tuple[str, ...]:
        return tuple(f for f in self.formats if f not in COMPRESSED_FORMATS)

    @property
    def compressed_only(self) -> bool:
        return bool(self.formats) and not self.raw_formats

    def describe(self) -> str:
        fmts = f" formats={','.join(self.formats)}" if self.formats else ""
        node = f" ({self.path})" if self.path and self.kind == "csi" else ""
        return f"{self.name} [{self.kind}] {self.element} {self.location}{fmts}{node}"


# ---- V4L2 ioctl helpers (Linux only) ----
_VIDIOC_QUERYCAP = 0x80685600  # _IOR('V', 0, struct v4l2_capability) 104 bytes
_VIDIOC_ENUM_FMT = 0xC0405602  # _IOWR('V', 2, struct v4l2_fmtdesc) 64 bytes
_V4L2_CAP_VIDEO_CAPTURE = 0x00000001
_V4L2_CAP_VIDEO_CAPTURE_MPLANE = 0x00001000
_V4L2_CAP_DEVICE_CAPS = 0x80000000
_V4L2_BUF_TYPE_VIDEO_CAPTURE = 1
_V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE = 9


@dataclass
class V4l2Node:
    path: str
    driver: str
    card: str
    bus_info: str
    device_caps: int
    formats: tuple[str, ...]

    @property
    def is_capture(self) -> bool:
        return bool(
            self.device_caps
            & (_V4L2_CAP_VIDEO_CAPTURE | _V4L2_CAP_VIDEO_CAPTURE_MPLANE)
        )

    @property
    def is_tegra_csi(self) -> bool:
        return self.driver.startswith("tegra") or "vi-output" in self.card.lower()


def _cstr(raw: bytes) -> str:
    return raw.split(b"\0", 1)[0].decode(errors="replace").strip()


def _enum_formats(fd: int, buf_type: int) -> tuple[str, ...]:
    formats: list[str] = []
    for index in range(64):
        req = array.array(
            "B", struct.pack("<III32sIII3I", index, buf_type, 0, b"", 0, 0, 0, 0, 0, 0)
        )
        try:
            fcntl.ioctl(fd, _VIDIOC_ENUM_FMT, req, True)
        except OSError:
            break
        pixelformat = struct.unpack_from("<I", req, 44)[0]
        formats.append(_cstr(pixelformat.to_bytes(4, "little")) or f"{pixelformat:#x}")
    return tuple(formats)


def query_v4l2_node(path: str) -> V4l2Node | None:
    """QUERYCAP + ENUM_FMT on a /dev/video* node; None if it cannot be opened."""
    try:
        fd = os.open(path, os.O_RDWR | os.O_NONBLOCK)
    except OSError as exc:
        log.debug("cannot open %s: %s", path, exc)
        return None
    try:
        cap = array.array("B", bytes(104))
        try:
            fcntl.ioctl(fd, _VIDIOC_QUERYCAP, cap, True)
        except OSError as exc:
            log.debug("QUERYCAP failed on %s: %s", path, exc)
            return None
        driver, card, bus_info, _version, capabilities, device_caps = (
            struct.unpack_from("<16s32s32sIII", cap, 0)
        )
        if not capabilities & _V4L2_CAP_DEVICE_CAPS:
            device_caps = capabilities
        formats: tuple[str, ...] = ()
        if device_caps & _V4L2_CAP_VIDEO_CAPTURE:
            formats = _enum_formats(fd, _V4L2_BUF_TYPE_VIDEO_CAPTURE)
        elif device_caps & _V4L2_CAP_VIDEO_CAPTURE_MPLANE:
            formats = _enum_formats(fd, _V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE)
        return V4l2Node(
            path=path,
            driver=_cstr(driver),
            card=_cstr(card),
            bus_info=_cstr(bus_info),
            device_caps=device_caps,
            formats=formats,
        )
    finally:
        os.close(fd)


def _video_nodes() -> list[str]:
    def key(path: str) -> int:
        digits = "".join(ch for ch in os.path.basename(path) if ch.isdigit())
        return int(digits) if digits else 0

    return sorted(glob.glob("/dev/video[0-9]*"), key=key)


def _avf_device_index(device: Gst.Device, fallback: int) -> int:
    element = device.create_element(None)
    if element is not None and element.find_property("device-index") is not None:
        return int(element.get_property("device-index"))
    return fallback


def _sysfs_v4l2_name(path: str) -> str:
    node = os.path.basename(path)
    try:
        with open(f"/sys/class/video4linux/{node}/name") as f:
            return f.read().strip() or path
    except OSError:
        return path


def enumerate_cameras(plat: Platform) -> list[Camera]:
    cameras: list[Camera] = []

    if plat == Platform.MACOS:
        monitor = Gst.DeviceMonitor.new()
        monitor.add_filter("Video/Source", None)
        monitor.start()
        try:
            devices = list(monitor.get_devices() or [])
        finally:
            monitor.stop()
        for i, device in enumerate(devices):
            cameras.append(
                Camera(
                    index=len(cameras),
                    name=device.get_display_name(),
                    kind="avf",
                    element="avfvideosrc",
                    properties={"device-index": _avf_device_index(device, i)},
                )
            )
        return cameras

    if plat not in (Platform.LINUX, Platform.JETSON):
        log.warning("Camera enumeration not supported on %s", plat.value)
        return cameras

    have_argus = plat == Platform.JETSON and bool(
        Gst.ElementFactory.find("nvarguscamerasrc")
    )
    csi_sensor = 0
    unreadable: list[str] = []

    for path in _video_nodes():
        node = query_v4l2_node(path)
        if node is None:
            unreadable.append(path)
            continue
        if not node.is_capture:
            log.debug("skip %s (%s): not a video capture node", path, node.card)
            continue
        if plat == Platform.JETSON and node.is_tegra_csi:
            sensor = node.card.split(",", 1)[-1].strip() or node.card
            if have_argus:
                cameras.append(
                    Camera(
                        index=len(cameras),
                        name=f"{sensor} (CSI)",
                        kind="csi",
                        element="nvarguscamerasrc",
                        properties={"sensor-id": csi_sensor},
                        formats=node.formats,
                        path=path,
                    )
                )
            else:
                log.warning(
                    "%s is a CSI sensor (%s) but nvarguscamerasrc is not available",
                    path,
                    sensor,
                )
            csi_sensor += 1
            continue
        cameras.append(
            Camera(
                index=len(cameras),
                name=node.card,
                kind="v4l2",
                element="v4l2src",
                properties={"device": path},
                formats=node.formats,
                path=path,
            )
        )

    if unreadable:
        if not cameras:
            # Fall back to names from sysfs / the device monitor.
            for path in unreadable:
                cameras.append(
                    Camera(
                        index=len(cameras),
                        name=_sysfs_v4l2_name(path),
                        kind="v4l2",
                        element="v4l2src",
                        properties={"device": path},
                        path=path,
                    )
                )
            log.warning(
                "could not query %s (permissions? add the user to the 'video' group)",
                ", ".join(unreadable),
            )
        else:
            log.debug("unreadable video nodes: %s", ", ".join(unreadable))
    return cameras


def format_camera_list(cameras: list[Camera]) -> str:
    if not cameras:
        return "No cameras detected."
    lines = [f"Detected cameras ({len(cameras)}):"]
    for cam in cameras:
        lines.append(f"  [{cam.index}] {cam.describe()}")
    return "\n".join(lines)


def _prompt(cameras: list[Camera]) -> Camera:
    print(format_camera_list(cameras))
    while True:
        try:
            choice = input(
                f"Select camera [0-{len(cameras) - 1}] (default: 0): "
            ).strip()
        except (EOFError, KeyboardInterrupt):
            raise CameraError("camera selection cancelled") from None
        if choice == "":
            return cameras[0]
        if choice.isdigit() and 0 <= int(choice) < len(cameras):
            return cameras[int(choice)]
        print(f"Enter a number between 0 and {len(cameras) - 1}.")


def select_camera(cameras: list[Camera], spec: str) -> Camera:
    """Resolve a --camera value.

    spec == ""      -> only camera, or interactive prompt (first camera if no TTY)
    spec is digits  -> index into the detected list
    otherwise       -> exact device path / sensor id, or case-insensitive name match
    """
    if not cameras:
        raise CameraError("no cameras detected (use --list-cameras to check)")

    if spec == "":
        if len(cameras) == 1:
            return cameras[0]
        if not sys.stdin.isatty():
            log.info("stdin is not a TTY; using first camera")
            return cameras[0]
        return _prompt(cameras)

    if spec.isdigit():
        idx = int(spec)
        if 0 <= idx < len(cameras):
            return cameras[idx]
        raise CameraError(f"camera index {idx} out of range 0-{len(cameras) - 1}")

    exact = [c for c in cameras if any(str(v) == spec for v in c.properties.values())]
    if len(exact) == 1:
        return exact[0]

    by_name = [c for c in cameras if spec.lower() in c.name.lower()]
    if len(by_name) == 1:
        return by_name[0]
    if len(by_name) > 1:
        names = ", ".join(f"[{c.index}] {c.name}" for c in by_name)
        raise CameraError(f"'{spec}' matches several cameras: {names}")
    raise CameraError(f"no camera matches '{spec}'\n{format_camera_list(cameras)}")
