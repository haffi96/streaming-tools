"""SEI timestamp / frame-id metadata (UUID + LKTS packet trailer) and injection.

The payload format is shared with the LiveKit publisher/viewer tooling and with
the parse-h264 tool; keep it byte-for-byte stable.

The user timestamp carries the frame's *capture* time: the buffer PTS (set by
the source at capture) converted from the pipeline clock to wall-clock Unix
microseconds. Latency measured against it therefore includes capture-to-encoder
queuing and encode time, not just transport. If a buffer has no usable PTS the
current time is used instead.

The same probe can also terminate every access unit with an AUD NAL
(``au_terminator``). A receiver that parses the byte stream (the hybrid-bridge
publisher's ``h264parse``, ``parse-h264``) otherwise only learns that frame N
is complete when the first NAL of frame N+1 arrives, one frame interval later;
an AUD at the tail of each AU closes it as soon as its last byte is in.

H.264 and H.265 carry the same SEI payload; only the NAL headers differ:

  H.264 SEI:  06                         nal_unit_type 6
  H.265 SEI:  4E 01                      prefix SEI, nal_unit_type 39, layer 0, tid 1
  H.264 AUD:  09 F0                      primary_pic_type 7 + stop bit
  H.265 AUD:  46 01 50                   nal_unit_type 35, pic_type 2 + stop bit

followed (SEI only) by payloadType 5 (user_data_unregistered), payloadSize,
UUID, LKTS trailer and the 0x80 RBSP stop byte. Emulation prevention bytes are
inserted where the payload would otherwise contain a start code.

Framing follows the stream: a 4-byte start code for byte-stream, a 4-byte
big-endian length for the length-prefixed formats (``avc``, and ``hvc1`` /
``hev1`` caps for H.265).
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

# Access unit delimiter (nal_unit_type 9): nal_ref_idc 0, primary_pic_type 7
# ("any slice types") followed by the RBSP stop bit. An AUD is always exactly
# these two bytes, so a parser can complete it without waiting for the next
# start code.
AUD_NAL = bytes([0x09, 0xF0])

# HEVC access unit delimiter: 2-byte NAL header (forbidden_zero_bit 0,
# nal_unit_type 35, nuh_layer_id 0, nuh_temporal_id_plus1 1), then pic_type 2
# ("I, P, B") in 3 bits followed by the RBSP stop bit: 010 1 0000 = 0x50.
# Always exactly three bytes.
HEVC_AUD_NAL = bytes([0x46, 0x01, 0x50])

# NAL headers of the SEI NAL: H.264 nal_unit_type 6; HEVC prefix SEI
# (nal_unit_type 39, nuh_layer_id 0, nuh_temporal_id_plus1 1).
SEI_NAL_HEADER = {"h264": bytes([0x06]), "h265": bytes([0x4E, 0x01])}
AUD_NALS = {"h264": AUD_NAL, "h265": HEVC_AUD_NAL}

# stream_format values that mean 4-byte length prefixes rather than start codes.
LENGTH_PREFIXED_FORMATS = ("avc", "hvc1", "hev1")


def _frame_nal(nal: bytes, stream_format: str) -> bytes:
    """Prefix a NAL with a 4-byte length or a 4-byte Annex B start code."""
    if stream_format in LENGTH_PREFIXED_FORMATS:
        return struct.pack(">I", len(nal)) + nal
    return b"\x00\x00\x00\x01" + nal


def add_emulation_prevention(rbsp: bytes) -> bytes:
    """Insert emulation_prevention_three_byte (0x03) after every 00 00 that is
    followed by a byte <= 0x03, so the NAL payload cannot contain a start code."""
    out = bytearray()
    zeros = 0
    for byte in rbsp:
        if zeros >= 2 and byte <= 0x03:
            out.append(0x03)
            zeros = 0
        out.append(byte)
        zeros = zeros + 1 if byte == 0 else 0
    return bytes(out)


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
    codec: str = "h264",
) -> bytes:
    """Create an SEI NAL unit containing timestamp/frame ID metadata.

    Args:
        timestamp_us: Timestamp in microseconds. If None, uses current time.
        frame_id: Optional frame ID to include in the packet trailer.
        stream_format: 'byte-stream' for Annex B or 'avc' (also 'hvc1'/'hev1')
            for 4-byte length-prefixed
        codec: 'h264' (SEI NAL type 6) or 'h265' (prefix SEI NAL type 39)

    Returns:
        Bytes containing a complete SEI NAL unit with timestamp/frame ID metadata.
    """
    if timestamp_us is None:
        timestamp_us = int(time.time() * 1_000_000)

    # Build SEI payload: UUID (16 bytes) + LKTS packet trailer
    payload = SEI_UUID + append_packet_trailer(timestamp_us, frame_id)
    payload_type = 5  # user_data_unregistered
    payload_size = len(payload)

    rbsp = bytearray()
    # Payload type (single byte since 5 < 255)
    rbsp.append(payload_type)
    # Payload size (single byte for the current timestamp/frame ID payload)
    rbsp.append(payload_size)
    rbsp.extend(payload)
    # RBSP trailing bits (stop bit + alignment)
    rbsp.append(0x80)

    # NAL header: H.264 nal_ref_idc=0, nal_unit_type=6 (SEI); HEVC prefix SEI
    # (39). The XOR-ed trailer can contain 00 00 0x runs (e.g. a 0xFFFF in
    # the timestamp), so the payload gets emulation prevention.
    nal = SEI_NAL_HEADER[codec] + add_emulation_prevention(bytes(rbsp))
    return _frame_nal(nal, stream_format)


def create_aud_nalu(stream_format: str = "byte-stream", codec: str = "h264") -> bytes:
    """Return an access unit delimiter NAL in the stream's framing."""
    return _frame_nal(AUD_NALS[codec], stream_format)


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
    """Rewrites every access unit via a pad probe: an SEI NAL (capture
    timestamp + frame id, see capture_time_us) is prepended when
    ``sei_metadata`` is set, and an AUD NAL is appended when ``au_terminator``
    is set. Attach it to a pad that carries AU-aligned buffers.
    """

    def __init__(
        self,
        stream_format: str = "byte-stream",
        sei_metadata: bool = True,
        au_terminator: bool = False,
        codec: str = "h264",
    ):
        self.stream_format = stream_format
        self.sei_metadata = sei_metadata
        self.au_terminator = au_terminator
        self.codec = codec
        self._aud = create_aud_nalu(stream_format, codec) if au_terminator else b""
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

        sei_nalu = b""
        if self.sei_metadata:
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
                codec=self.codec,
            )

        success, map_info = buffer.map(Gst.MapFlags.READ)
        if not success:
            return Gst.PadProbeReturn.OK
        try:
            original_data = bytes(map_info.data)
        finally:
            buffer.unmap(map_info)

        new_buffer = Gst.Buffer.new_wrapped(sei_nalu + original_data + self._aud)
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
            log.debug("Rewrote %d access units", self.frame_count)

        return Gst.PadProbeReturn.DROP
