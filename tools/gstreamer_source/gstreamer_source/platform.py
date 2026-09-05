"""Platform detection (macOS, Jetson, generic Linux)."""

from __future__ import annotations

import os
import platform as _platform
from dataclasses import dataclass
from enum import Enum


class Platform(Enum):
    MACOS = "macos"
    LINUX = "linux"
    JETSON = "jetson"
    UNKNOWN = "unknown"


_DEVICE_TREE_MODEL_PATHS = (
    "/proc/device-tree/model",
    "/sys/firmware/devicetree/base/model",
)


def device_tree_model() -> str | None:
    """Return the board model string from the device tree, if present.

    On Jetson this is e.g. "NVIDIA Jetson Orin Nano Developer Kit" or
    "NVIDIA Jetson AGX Thor Developer Kit".
    """
    for path in _DEVICE_TREE_MODEL_PATHS:
        try:
            with open(path, "rb") as f:
                model = f.read().rstrip(b"\x00").decode("utf-8", "replace").strip()
        except OSError:
            continue
        if model:
            return model
    return None


def is_jetson() -> bool:
    if os.path.exists("/etc/nv_tegra_release"):
        return True
    try:
        if "tegra" in os.uname().release.lower():
            return True
    except AttributeError:
        pass
    model = (device_tree_model() or "").lower()
    return "jetson" in model or "tegra" in model


def detect_platform() -> Platform:
    system = _platform.system()
    if system == "Darwin":
        return Platform.MACOS
    if system == "Linux":
        return Platform.JETSON if is_jetson() else Platform.LINUX
    return Platform.UNKNOWN


@dataclass(frozen=True)
class PlatformInfo:
    platform: Platform
    model: str | None
    machine: str

    def describe(self) -> str:
        parts = [self.platform.value, self.machine]
        if self.model:
            parts.append(self.model)
        return " / ".join(parts)


def platform_info() -> PlatformInfo:
    plat = detect_platform()
    model: str | None = None
    if plat == Platform.MACOS:
        mac_ver = _platform.mac_ver()[0]
        model = f"macOS {mac_ver}" if mac_ver else None
    else:
        model = device_tree_model()
    return PlatformInfo(platform=plat, model=model, machine=_platform.machine())
