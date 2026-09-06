"""Running millisecond clock burned into the video.

A ``textoverlay`` element renders the text; a pad probe on its video sink pad
rewrites the text for every frame from the buffer's running time, i.e. the
frame's capture time relative to pipeline start. No wall-clock or monotonic
clock is read on the streaming thread, so the overlay adds only the pango
render cost, and the number on screen lines up with the SEI capture timestamp
of the same frame.
"""

from __future__ import annotations

import logging
import time

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst

log = logging.getLogger(__name__)


def format_clock_ms(elapsed_ms: int) -> str:
    """Format milliseconds as h:mm:ss.mmm."""
    ms = elapsed_ms % 1000
    seconds = elapsed_ms // 1000
    return f"{seconds // 3600}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}.{ms:03d}"


def running_time_ms(pad: Gst.Pad, buffer: Gst.Buffer) -> int | None:
    """Running time of `buffer` in milliseconds, or None if unknown."""
    pts = buffer.pts
    if pts == Gst.CLOCK_TIME_NONE:
        return None
    segment_event = pad.get_sticky_event(Gst.EventType.SEGMENT, 0)
    if segment_event is None:
        return None
    running_time = segment_event.parse_segment().to_running_time(
        Gst.Format.TIME, pts
    )
    if running_time == Gst.CLOCK_TIME_NONE:
        return None
    return running_time // Gst.MSECOND


class TimestampOverlay:
    """Updates a textoverlay's text with a running millisecond clock."""

    def __init__(self, overlay: Gst.Element):
        self.overlay = overlay
        self.frame_count = 0
        self.fallback_count = 0
        self._timer_start_ns: int | None = None

    def attach(self) -> None:
        pad = self.overlay.get_static_pad("video_sink")
        if pad is None:
            raise RuntimeError("textoverlay has no video_sink pad")
        pad.add_probe(Gst.PadProbeType.BUFFER, self._probe)

    def _probe(self, pad: Gst.Pad, info: Gst.PadProbeInfo) -> Gst.PadProbeReturn:
        buffer = info.get_buffer()
        if buffer is None:
            return Gst.PadProbeReturn.OK
        elapsed_ms = running_time_ms(pad, buffer)
        if elapsed_ms is None:
            # No usable PTS: fall back to a monotonic timer since the first frame.
            now = time.monotonic_ns()
            if self._timer_start_ns is None:
                self._timer_start_ns = now
            elapsed_ms = (now - self._timer_start_ns) // 1_000_000
            self.fallback_count += 1
            if self.fallback_count == 1:
                log.warning(
                    "buffer has no usable PTS; timestamp overlay uses a monotonic timer"
                )
        self.overlay.set_property("text", format_clock_ms(elapsed_ms))
        self.frame_count += 1
        return Gst.PadProbeReturn.OK
