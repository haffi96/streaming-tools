"""Camera discovery and selection.

Uses GstDeviceMonitor everywhere (avfvideosrc on macOS, v4l2src on Linux) and
maps Jetson CSI sensors to nvarguscamerasrc.
"""

from __future__ import annotations

import glob
import logging
import os
import sys
from dataclasses import dataclass, field

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst

from .platform import Platform

log = logging.getLogger(__name__)


class CameraError(RuntimeError):
    pass


@dataclass
class Camera:
    index: int
    name: str
    kind: str  # "avf" | "v4l2" | "csi"
    element: str
    properties: dict[str, object] = field(default_factory=dict)

    @property
    def location(self) -> str:
        return ", ".join(f"{k}={v}" for k, v in self.properties.items())

    def describe(self) -> str:
        return f"{self.name} [{self.kind}] {self.element} {self.location}"


def _monitor_devices() -> list[Gst.Device]:
    monitor = Gst.DeviceMonitor.new()
    monitor.add_filter("Video/Source", None)
    monitor.start()
    try:
        return list(monitor.get_devices() or [])
    finally:
        monitor.stop()


def _is_csi_device_name(name: str) -> bool:
    lowered = name.lower()
    return "vi-output" in lowered or "tegra" in lowered


def _avf_device_index(device: Gst.Device, fallback: int) -> int:
    element = device.create_element(None)
    if element is not None and element.find_property("device-index") is not None:
        return int(element.get_property("device-index"))
    return fallback


def _v4l2_path(device: Gst.Device) -> str | None:
    props = device.get_properties()
    if props is None:
        return None
    for key in ("device.path", "api.v4l2.path", "object.path"):
        path = props.get_string(key)
        if path and path.startswith("/dev/"):
            return path
    return None


def _sysfs_v4l2_name(path: str) -> str:
    node = os.path.basename(path)
    try:
        with open(f"/sys/class/video4linux/{node}/name") as f:
            return f.read().strip() or path
    except OSError:
        return path


def enumerate_cameras(plat: Platform) -> list[Camera]:
    cameras: list[Camera] = []
    devices = _monitor_devices()

    if plat == Platform.MACOS:
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

    have_argus = plat == Platform.JETSON and Gst.ElementFactory.find("nvarguscamerasrc")
    csi_sensor = 0
    seen_paths: set[str] = set()

    for device in sorted(devices, key=lambda d: _v4l2_path(d) or ""):
        name = device.get_display_name()
        path = _v4l2_path(device)
        if plat == Platform.JETSON and _is_csi_device_name(name):
            if have_argus:
                cameras.append(
                    Camera(
                        index=len(cameras),
                        name=f"{name} (CSI)",
                        kind="csi",
                        element="nvarguscamerasrc",
                        properties={"sensor-id": csi_sensor},
                    )
                )
            csi_sensor += 1
            continue
        if not path:
            continue
        seen_paths.add(path)
        cameras.append(
            Camera(
                index=len(cameras),
                name=name,
                kind="v4l2",
                element="v4l2src",
                properties={"device": path},
            )
        )

    if not devices:
        # No v4l2 device provider (plugin missing): fall back to /dev/video*.
        for path in sorted(glob.glob("/dev/video*")):
            if path in seen_paths:
                continue
            cameras.append(
                Camera(
                    index=len(cameras),
                    name=_sysfs_v4l2_name(path),
                    kind="v4l2",
                    element="v4l2src",
                    properties={"device": path},
                )
            )
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
