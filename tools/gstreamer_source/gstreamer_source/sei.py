"""SEI timestamp / frame-id metadata (UUID + LKTS packet trailer) and injection.

The payload format is shared with the LiveKit publisher/viewer tooling and with
the parse-h264 tool; keep it byte-for-byte stable.

The user timestamp carries the frame's *capture* time: the buffer PTS (set by
the source at capture) converted from the pipeline clock to wall-clock Unix
microseconds. Latency measured against it therefore includes capture-to-encoder
queuing and encode time, not just transport. If a buffer has no usable PTS the
current time is used instead.
"""

from __future__ import annotations

import logging
import struct
import time

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst

log = logging.getLogger(__name__)

# UUID for SEI timestamp messages (matches sample file and publisher/viewer)
SEI_UUID = bytes.fromhex("3fa85f6457174562b3fc2c963f66afa6")
PACKET_TRAILER_MAGIC = b"LKTS"
TAG_USER_TIMESTAMP = 0x01
TAG_FRAME_ID = 0x02


def append_packet_trailer(timestamp_us: int, frame_id: int = 0) -> bytes:
    """Build the LKTS packet trailer expected by the LiveKit Go SDK."""
    trailer = bytearray()

    if timestamp_us != 0:
        trailer.append(TAG_USER_TIMESTAMP ^ 0xFF)
        trailer.append(8 ^ 0xFF)
        for byte in struct.pack(">Q", timestamp_us):
            trailer.append(byte ^ 0xFF)

    if frame_id != 0:
        trailer.append(TAG_FRAME_ID ^ 0xFF)
        trailer.append(4 ^ 0xFF)
        for byte in struct.pack(">I", frame_id):
            trailer.append(byte ^ 0xFF)

    if not trailer:
        return b""

    trailer_len = len(trailer) + 1 + len(PACKET_TRAILER_MAGIC)
    trailer.append(trailer_len ^ 0xFF)
    trailer.extend(PACKET_TRAILER_MAGIC)
    return bytes(trailer)


def create_sei_nalu(
    timestamp_us: int | None = None,
    frame_id: int = 0,
    stream_format: str = "byte-stream",
) -> bytes:
    """Create an SEI NAL unit containing timestamp/frame ID metadata.

    Args:
        timestamp_us: Timestamp in microseconds. If None, uses current time.
        frame_id: Optional frame ID to include in the packet trailer.
        stream_format: 'byte-stream' for Annex B or 'avc' for length-prefixed

    Returns:
        Bytes containing a complete SEI NAL unit with timestamp/frame ID metadata.
    """
    if timestamp_us is None:
        timestamp_us = int(time.time() * 1_000_000)

    # Build SEI payload: UUID (16 bytes) + LKTS packet trailer
    payload = SEI_UUID + append_packet_trailer(timestamp_us, frame_id)
    payload_type = 5  # user_data_unregistered
    payload_size = len(payload)

    nal_content = bytearray()
    # NAL header: nal_ref_idc=0, nal_unit_type=6 (SEI)
    nal_content.append(0x06)
    # Payload type (single byte since 5 < 255)
    nal_content.append(payload_type)
    # Payload size (single byte for the current timestamp/frame ID payload)
    nal_content.append(payload_size)
    nal_content.extend(payload)
    # RBSP trailing bits (stop bit + alignment)
    nal_content.append(0x80)

    sei_nalu = bytearray()
    if stream_format == "avc":
        # AVC format: 4-byte big-endian length prefix
        sei_nalu.extend(struct.pack(">I", len(nal_content)))
    else:
        # Annex B format: start code
        sei_nalu.extend([0x00, 0x00, 0x00, 0x01])
    sei_nalu.extend(nal_content)
    return bytes(sei_nalu)


def capture_time_us(pad: Gst.Pad, buffer: Gst.Buffer) -> int | None:
    """Wall-clock capture time of `buffer` in microseconds, or None if unknown.

    base_time + running_time(PTS) is the capture instant in the pipeline
    clock's domain; its age relative to the clock's current time is subtracted
    from wall-clock now, so the result is independent of which clock the
    pipeline uses.
    """
    pts = buffer.pts
    if pts == Gst.CLOCK_TIME_NONE:
        return None
    # Encoders (GstVideoEncoder) may shift output PTS by a large constant and
    # compensate in the segment, so go through running time, not raw PTS.
    segment_event = pad.get_sticky_event(Gst.EventType.SEGMENT, 0)
    if segment_event is None:
        return None
    segment = segment_event.parse_segment()
    running_time = segment.to_running_time(Gst.Format.TIME, pts)
    if running_time == Gst.CLOCK_TIME_NONE:
        return None
    element = pad.get_parent_element()
    if element is None:
        return None
    clock = element.get_clock()
    base_time = element.get_base_time()
    if clock is None or base_time == Gst.CLOCK_TIME_NONE:
        return None
    age_ns = clock.get_time() - (base_time + running_time)
    return (time.time_ns() - age_ns) // 1000


class SeiInjector:
    """Injects an SEI NAL unit in front of every access unit via a pad probe.

    The SEI timestamp is the frame's capture time (see capture_time_us).
    """

    def __init__(self, stream_format: str = "byte-stream"):
        self.stream_format = stream_format
        self.probe_id: int = 0
        self.frame_count: int = 0
        self.next_frame_id: int = 1
        self.fallback_count: int = 0

    def attach(self, pad: Gst.Pad) -> None:
        self.probe_id = pad.add_probe(Gst.PadProbeType.BUFFER, self.probe_callback)

    def probe_callback(
        self, pad: Gst.Pad, info: Gst.PadProbeInfo
    ) -> Gst.PadProbeReturn:
        buffer = info.get_buffer()
        if not buffer:
            return Gst.PadProbeReturn.OK

        frame_id = self.next_frame_id
        self.next_frame_id = (self.next_frame_id % 0xFFFFFFFF) + 1

        timestamp_us = capture_time_us(pad, buffer)
        if timestamp_us is None:
            self.fallback_count += 1
            if self.fallback_count == 1:
                log.warning(
                    "buffer has no usable PTS; SEI timestamp falls back to current time"
                )
        sei_nalu = create_sei_nalu(
            timestamp_us=timestamp_us,
            frame_id=frame_id,
            stream_format=self.stream_format,
        )

        success, map_info = buffer.map(Gst.MapFlags.READ)
        if not success:
            return Gst.PadProbeReturn.OK
        try:
            original_data = bytes(map_info.data)
        finally:
            buffer.unmap(map_info)

        new_buffer = Gst.Buffer.new_wrapped(sei_nalu + original_data)
        new_buffer.pts = buffer.pts
        new_buffer.dts = buffer.dts
        new_buffer.duration = buffer.duration
        new_buffer.offset = buffer.offset
        new_buffer.offset_end = buffer.offset_end

        # Remove the probe while pushing to avoid re-entering this callback.
        pad.remove_probe(self.probe_id)
        pad.push(new_buffer)
        self.probe_id = pad.add_probe(Gst.PadProbeType.BUFFER, self.probe_callback)

        self.frame_count += 1
        if self.frame_count % 300 == 0:
            log.debug("Injected SEI into %d frames", self.frame_count)

        return Gst.PadProbeReturn.DROP
