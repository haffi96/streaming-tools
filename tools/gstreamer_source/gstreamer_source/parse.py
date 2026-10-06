"""Inspect an H.264 / H.265 (or raw NV12) stream from a file or TCP server.

Every access unit (frame) is logged with its size, NAL types and keyframe flag,
so a stream can be verified even when it carries no SEI metadata. With
--sei-detection the gstreamer-source SEI (UUID 3fa85f6457174562b3fc2c963f66afa6,
LKTS packet trailer or legacy 8-byte timestamp) is decoded as well and each
frame line gains the embedded timestamp (file) or end-to-end latency (TCP).

The codec is auto-detected from the NAL headers (--codec forces it). For H.265
the 2-byte NAL headers are decoded, keyframes are IRAP pictures (types 16-21),
the SEI is the prefix SEI (type 39) and the 3-byte AUD (46 01 50) completes an
access unit the same way the 2-byte H.264 AUD does.

The latency shown is receive time minus the SEI timestamp, and the SEI
timestamp is the frame's capture time (the source buffer PTS, see sei.py). It
therefore spans capture delivery, format conversion, encoding and transport
combined; transport alone is not measured separately. Across machines it is
only meaningful if both clocks are synchronized.
"""

from __future__ import annotations

import argparse
import socket
import struct
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .sei import PACKET_TRAILER_MAGIC, SEI_UUID, TAG_FRAME_ID, TAG_USER_TIMESTAMP

LATENCY_NOTE = (
    "latency = receive time - SEI capture timestamp (source frame PTS); "
    "includes capture delivery, conversion, encode and transport, "
    "not transport alone; cross-machine values need synchronized clocks"
)

NAL_SLICE = 1
NAL_IDR = 5
NAL_SEI = 6
NAL_AUD = 9
NAL_NAMES = {
    1: "slice",
    2: "slice-A",
    3: "slice-B",
    4: "slice-C",
    5: "IDR",
    6: "SEI",
    7: "SPS",
    8: "PPS",
    9: "AUD",
    10: "EOSeq",
    11: "EOStream",
    12: "filler",
    14: "prefix",
    20: "slice-ext",
}
VCL_TYPES = {1, 2, 3, 4, 5}
SEI_USER_DATA_UNREGISTERED = 5

HEVC_NAL_AUD = 35
HEVC_NAL_PREFIX_SEI = 39
HEVC_NAL_SUFFIX_SEI = 40
HEVC_NAL_NAMES = {
    0: "TRAIL_N",
    1: "TRAIL_R",
    2: "TSA_N",
    3: "TSA_R",
    4: "STSA_N",
    5: "STSA_R",
    6: "RADL_N",
    7: "RADL_R",
    8: "RASL_N",
    9: "RASL_R",
    16: "BLA_W_LP",
    17: "BLA_W_RADL",
    18: "BLA_N_LP",
    19: "IDR_W_RADL",
    20: "IDR_N_LP",
    21: "CRA",
    32: "VPS",
    33: "SPS",
    34: "PPS",
    35: "AUD",
    36: "EOS",
    37: "EOB",
    38: "FD",
    39: "SEI",
    40: "suffix-SEI",
}
HEVC_VCL_TYPES = frozenset(range(32))
HEVC_IRAP_TYPES = frozenset(range(16, 22))
# Non-VCL NALs that open a new access unit when they follow a VCL NAL (H.265
# 7.4.2.4.4): VPS, SPS, PPS, AUD, prefix SEI, reserved 41..44, unspecified
# 48..55. Suffix SEI, EOS, EOB and filler data stay with the current AU.
HEVC_AU_START_TYPES = frozenset({32, 33, 34, 35, 39, 41, 42, 43, 44, *range(48, 56)})


@dataclass(frozen=True)
class NalSyntax:
    """Codec-specific NAL header layout and type tables."""

    codec: str  # h264 | h265
    header_len: int  # NAL header bytes (1 for H.264, 2 for H.265)
    names: dict[int, str]
    vcl_types: frozenset[int]
    keyframe_types: frozenset[int]
    sei_types: frozenset[int]
    aud_type: int
    aud_len: int  # an AUD NAL is always exactly this many bytes
    # Non-VCL types that start a new AU after VCL NALs; None = every non-VCL.
    au_start_types: frozenset[int] | None

    def nal_type(self, nal: bytes | bytearray) -> int:
        if self.header_len == 2:
            return (nal[0] >> 1) & 0x3F
        return nal[0] & 0x1F

    def first_slice_in_picture(self, nal: bytes) -> bool:
        # H.264: first_mb_in_slice is the first ue(v) of the slice header and
        # ue(v) == 0 is a single '1' bit. H.265: first_slice_segment_in_pic_flag
        # is the first bit of the slice segment header.
        return len(nal) > self.header_len and (nal[self.header_len] & 0x80) != 0

    def keyframe_label(self, types: list[int]) -> str:
        """Three-character keyframe marker for the frame line."""
        for t in types:
            if t not in self.keyframe_types:
                continue
            if self.codec == "h264" or t in (19, 20):
                return "IDR"
            return "CRA" if t == 21 else "BLA"
        return "   "


H264 = NalSyntax(
    codec="h264",
    header_len=1,
    names=NAL_NAMES,
    vcl_types=frozenset(VCL_TYPES),
    keyframe_types=frozenset({NAL_IDR}),
    sei_types=frozenset({NAL_SEI}),
    aud_type=NAL_AUD,
    aud_len=2,
    au_start_types=None,
)
H265 = NalSyntax(
    codec="h265",
    header_len=2,
    names=HEVC_NAL_NAMES,
    vcl_types=HEVC_VCL_TYPES,
    keyframe_types=HEVC_IRAP_TYPES,
    sei_types=frozenset({HEVC_NAL_PREFIX_SEI, HEVC_NAL_SUFFIX_SEI}),
    aud_type=HEVC_NAL_AUD,
    aud_len=3,
    au_start_types=HEVC_AU_START_TYPES,
)
SYNTAX = {"h264": H264, "h265": H265}

# ---------------------------------------------------------------------------
# SEI payload decoding
# ---------------------------------------------------------------------------


def parse_packet_trailer(data: bytes) -> dict[str, int] | None:
    """Parse an LKTS packet trailer payload."""
    if len(data) < 5 or data[-4:] != PACKET_TRAILER_MAGIC:
        return None

    trailer_len = data[-5] ^ 0xFF
    if trailer_len < 5 or trailer_len > len(data):
        return None

    tlv_region = data[-trailer_len:-5]
    pos = 0
    metadata: dict[str, int] = {}

    while pos + 2 <= len(tlv_region):
        tag = tlv_region[pos] ^ 0xFF
        length = tlv_region[pos + 1] ^ 0xFF
        pos += 2
        if pos + length > len(tlv_region):
            break

        value = bytes(byte ^ 0xFF for byte in tlv_region[pos : pos + length])
        if tag == TAG_USER_TIMESTAMP and length == 8:
            metadata["timestamp_us"] = int.from_bytes(value, "big")
        elif tag == TAG_FRAME_ID and length == 4:
            metadata["frame_id"] = int.from_bytes(value, "big")
        pos += length

    return metadata or None


def parse_user_data(uuid: bytes, user_data: bytes) -> tuple[int, str] | None:
    """Parse timestamp metadata from the SEI user_data payload.

    Returns (timestamp_us, format) where format is 'lkts' or 'legacy'.
    """
    if uuid != SEI_UUID:
        return None

    trailer_metadata = parse_packet_trailer(user_data)
    if trailer_metadata and "timestamp_us" in trailer_metadata:
        return trailer_metadata["timestamp_us"], "lkts"

    if len(user_data) == 8:
        return struct.unpack(">Q", user_data)[0], "legacy"

    return None


def remove_emulation_prevention(data: bytes) -> bytes:
    """Strip emulation_prevention_three_byte (00 00 03 -> 00 00) from a NAL payload."""
    if b"\x00\x00\x03" not in data:
        return data
    out = bytearray()
    zeros = 0
    for byte in data:
        if zeros >= 2 and byte == 0x03:
            zeros = 0
            continue
        out.append(byte)
        zeros = zeros + 1 if byte == 0 else 0
    return bytes(out)


@dataclass
class SeiTimestamp:
    timestamp_us: int
    format: str  # lkts | legacy
    frame_id: int | None = None


def extract_sei_timestamp(payload: bytes, verbose: bool = False) -> SeiTimestamp | None:
    """Walk the SEI messages in a SEI NAL payload (bytes after the NAL header)
    and return the first gstreamer-source timestamp found."""
    i = 0
    while i < len(payload) - 1:
        sei_type = 0
        while i < len(payload) and payload[i] == 0xFF:
            sei_type += 255
            i += 1
        if i < len(payload):
            sei_type += payload[i]
            i += 1

        sei_size = 0
        while i < len(payload) and payload[i] == 0xFF:
            sei_size += 255
            i += 1
        if i < len(payload):
            sei_size += payload[i]
            i += 1

        if verbose:
            print(f"    SEI type={sei_type}, size={sei_size}")

        if sei_type == SEI_USER_DATA_UNREGISTERED and i + sei_size <= len(payload):
            uuid = payload[i : i + 16]
            user_data = payload[i + 16 : i + sei_size]
            if verbose:
                print(f"    UUID: {uuid.hex()}")
                print(f"    Data: {user_data.hex()}")
            result = parse_user_data(uuid, user_data)
            if result is not None:
                ts_us, fmt = result
                trailer = parse_packet_trailer(user_data) or {}
                return SeiTimestamp(ts_us, fmt, trailer.get("frame_id"))

        i += sei_size

    return None


# ---------------------------------------------------------------------------
# NAL unit framing
# ---------------------------------------------------------------------------


def _plausible_h264_header(header: bytes | bytearray) -> bool:
    """Strict H.264 NAL header check: a type gstreamer-source / common encoders
    emit, with a nal_ref_idc that is legal for it."""
    if len(header) < 1 or header[0] & 0x80:
        return False
    nal_type = header[0] & 0x1F
    ref_idc = (header[0] >> 5) & 0x03
    if nal_type in (5, 7, 8):  # IDR, SPS, PPS: must be referenced
        return ref_idc != 0
    if nal_type in (6, 9, 10, 11, 12):  # SEI, AUD, end of seq/stream, filler
        return ref_idc == 0
    return nal_type == 1


def _plausible_h265_header(header: bytes | bytearray) -> bool:
    """Strict H.265 NAL header check: forbidden bit and nuh_layer_id 0,
    nuh_temporal_id_plus1 != 0, and a defined VCL / parameter set / SEI type."""
    if len(header) < 2 or header[0] & 0x81 or header[1] & 0xF8 or not header[1] & 0x07:
        return False
    return ((header[0] >> 1) & 0x3F) in HEVC_NAL_NAMES


def detect_stream_format(data: bytes | bytearray) -> str | None:
    """Return 'byte-stream', 'avc' (4-byte length prefixes, either codec) or
    None (no H.264/H.265 framing recognised)."""
    if len(data) < 6:
        return None
    if data[:4] == b"\x00\x00\x00\x01" or data[:3] == b"\x00\x00\x01":
        return "byte-stream"
    length = int.from_bytes(data[:4], "big")
    if 0 < length <= 4_000_000 and (
        ((data[4] & 0x80) == 0 and 1 <= (data[4] & 0x1F) <= 12)
        or _plausible_h265_header(data[4:6])
    ):
        # Plausible length prefix followed by a NAL header (forbidden_zero_bit clear).
        return "avc"
    if data.find(b"\x00\x00\x01", 0, 65536) != -1:
        # Joined mid-stream: a start code appears further in.
        return "byte-stream"
    return None


def nal_headers(
    data: bytes | bytearray, stream_format: str, limit: int = 32
) -> list[bytes]:
    """First two bytes of up to `limit` NALs in `data`, without consuming it.
    NALs need not be complete; only their headers are read."""
    headers: list[bytes] = []
    if stream_format == "avc":
        pos = 0
        while pos + 6 <= len(data) and len(headers) < limit:
            headers.append(bytes(data[pos + 4 : pos + 6]))
            pos += 4 + int.from_bytes(data[pos : pos + 4], "big")
        return headers
    pos = 0
    while len(headers) < limit:
        idx, code_len = find_start_code(data, pos)
        if idx == -1:
            break
        start = idx + code_len
        if start + 2 <= len(data):
            headers.append(bytes(data[start : start + 2]))
        pos = start
    return headers


def detect_codec(data: bytes | bytearray, stream_format: str) -> str | None:
    """Return 'h264' or 'h265' from the NAL headers in `data`, or None if no
    NAL header was found. Each header votes for the codec(s) it is a strict
    match for; ties go to H.264.

    The two header layouts overlap little: H.264 slices, SPS and AUD (0x41,
    0x65, 0x67, 0x09, ...) have odd first bytes, which H.265 forbids
    (nuh_layer_id 0); H.265 VPS/SPS/PPS/prefix SEI/AUD/TRAIL (0x40, 0x42,
    0x44, 0x4E, 0x46, 0x02 / 0x00 ...) are invalid or never-used H.264 types."""
    headers = nal_headers(data, stream_format)
    if not headers:
        return None
    h264 = sum(_plausible_h264_header(h) for h in headers)
    h265 = sum(_plausible_h265_header(h) for h in headers)
    return "h265" if h265 > h264 else "h264"


def find_start_code(data: bytes | bytearray, offset: int = 0) -> tuple[int, int]:
    """Return (index, length) of the next Annex B start code at or after offset,
    or (-1, 0). A 3-byte 00 00 01 preceded by 00 is reported as a 4-byte code."""
    idx = data.find(b"\x00\x00\x01", offset)
    if idx == -1:
        return -1, 0
    if idx > offset and data[idx - 1] == 0:
        return idx - 1, 4
    return idx, 3


def split_annexb(
    buffer: bytearray, final: bool, syntax: NalSyntax = H264
) -> Iterator[bytes]:
    """Yield complete NAL units from an Annex B buffer, consuming them.

    Leaves any trailing partial NAL in the buffer unless final is True.
    Trailing zero bytes (trailing_zero_8bits) are stripped from each NAL.
    """
    while True:
        start_idx, code_len = find_start_code(buffer)
        if start_idx == -1:
            if final:
                buffer.clear()
            elif len(buffer) > 3:
                del buffer[:-3]
            return
        nal_start = start_idx + code_len
        # An access unit delimiter is exactly two bytes in H.264 (header, then
        # primary_pic_type and the stop bit) and three in H.265 (2-byte
        # header, pic_type and the stop bit), so it is complete without the
        # next start code. Streams that terminate every AU with an AUD
        # (gstreamer-source --au-terminator) are then reported frame by frame
        # as each one arrives rather than when the next frame starts.
        aud_end = nal_start + syntax.aud_len
        if (
            len(buffer) >= aud_end
            and buffer[nal_start] & 0x80 == 0
            and syntax.nal_type(buffer[nal_start:aud_end]) == syntax.aud_type
        ):
            nal = bytes(buffer[nal_start:aud_end])
            del buffer[:aud_end]
            yield nal
            continue
        next_idx, _ = find_start_code(buffer, nal_start)
        if next_idx == -1:
            if not final:
                if start_idx:
                    del buffer[:start_idx]
                return
            next_idx = len(buffer)
        nal = bytes(buffer[nal_start:next_idx]).rstrip(b"\x00")
        del buffer[:next_idx]
        if nal:
            yield nal


def split_avc(buffer: bytearray, length_size: int = 4) -> Iterator[bytes]:
    """Yield complete NAL units from a length-prefixed buffer, consuming them."""
    while len(buffer) >= length_size:
        nalu_len = int.from_bytes(buffer[:length_size], "big")
        if len(buffer) < length_size + nalu_len:
            return
        nal = bytes(buffer[length_size : length_size + nalu_len])
        del buffer[: length_size + nalu_len]
        if nal:
            yield nal


def split_nalus(
    buffer: bytearray,
    stream_format: str,
    final: bool = False,
    syntax: NalSyntax = H264,
) -> Iterator[bytes]:
    if stream_format == "avc":
        return split_avc(buffer)
    return split_annexb(buffer, final, syntax)


# ---------------------------------------------------------------------------
# Access unit assembly
# ---------------------------------------------------------------------------


@dataclass
class AccessUnit:
    nals: list[bytes] = field(default_factory=list)
    # Wall-clock time (time.time()) at which the last NAL of this AU was
    # received; None in file mode.
    arrival: float | None = None
    syntax: NalSyntax = H264

    @property
    def size(self) -> int:
        return sum(len(n) for n in self.nals)

    @property
    def types(self) -> list[int]:
        return [self.syntax.nal_type(n) for n in self.nals]

    @property
    def is_keyframe(self) -> bool:
        return any(t in self.syntax.keyframe_types for t in self.types)

    @property
    def keyframe_label(self) -> str:
        return self.syntax.keyframe_label(self.types)

    def type_summary(self) -> str:
        runs: list[list] = []  # [name, count]
        for t in self.types:
            name = self.syntax.names.get(t, f"nal{t}")
            if runs and runs[-1][0] == name:
                runs[-1][1] += 1
            else:
                runs.append([name, 1])
        return ",".join(name if n == 1 else f"{name}x{n}" for name, n in runs)

    def sei_payloads(self) -> Iterator[bytes]:
        """SEI RBSPs (after the NAL header, emulation prevention removed)."""
        for n in self.nals:
            if self.syntax.nal_type(n) in self.syntax.sei_types:
                yield remove_emulation_prevention(n[self.syntax.header_len :])


class AccessUnitAssembler:
    """Groups NAL units into access units (frames).

    A new AU starts at an AUD, at a non-VCL NAL following VCL NALs (H.265:
    only parameter sets, AUD, prefix SEI and reserved types; suffix SEI, EOS,
    EOB and filler stay with the AU), or at a VCL NAL that starts a picture
    (H.264 first_mb_in_slice == 0, H.265 first_slice_segment_in_pic_flag)
    following VCL NALs (multi-slice frames).
    """

    def __init__(self, syntax: NalSyntax = H264) -> None:
        self.syntax = syntax
        self._current = AccessUnit(syntax=syntax)
        self._has_vcl = False

    def feed(self, nal: bytes, arrival: float | None = None) -> AccessUnit | None:
        syntax = self.syntax
        nal_type = syntax.nal_type(nal)
        is_vcl = nal_type in syntax.vcl_types
        if is_vcl:
            starts_au = syntax.first_slice_in_picture(nal)
        else:
            starts = syntax.au_start_types
            starts_au = starts is None or nal_type in starts
        completed: AccessUnit | None = None
        if self._has_vcl and starts_au:
            completed = self.flush()
        self._current.nals.append(nal)
        self._current.arrival = arrival
        if is_vcl:
            self._has_vcl = True
        return completed

    def flush(self) -> AccessUnit | None:
        if not self._current.nals:
            return None
        done = self._current
        self._current = AccessUnit(syntax=self.syntax)
        self._has_vcl = False
        return done


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def _iso(ts_us: int) -> str:
    try:
        return datetime.fromtimestamp(ts_us / 1_000_000, tz=timezone.utc).isoformat()
    except (ValueError, OSError, OverflowError):
        return f"<invalid: {ts_us} us>"


class ArrivalTracker:
    """Maps byte offsets in a receive buffer to the wall-clock arrival time of
    the chunk they came in with. Needed for Annex B, where a NAL is only known
    to be complete once the next start code arrives, possibly much later."""

    def __init__(self) -> None:
        self._chunks: list[tuple[int, float]] = []  # (end offset, arrival)

    def extend(self, length: int, arrival: float) -> None:
        end = (self._chunks[-1][0] if self._chunks else 0) + length
        self._chunks.append((end, arrival))

    def consume(self, length: int) -> float | None:
        """Drop the first `length` bytes; return the arrival time of the last one."""
        if length <= 0:
            return None
        arrival = next((t for end, t in self._chunks if end >= length), None)
        self._chunks = [(end - length, t) for end, t in self._chunks if end > length]
        return arrival


class FrameReporter:
    """Prints one line per access unit and a summary at the end."""

    def __init__(self, live: bool, sei_detection: bool, verbose: bool = False):
        self.live = live
        self.sei_detection = sei_detection
        self.verbose = verbose
        self.frames = 0
        self.keyframes = 0
        self.total_bytes = 0
        self.sei_nals = 0
        self.sei_frames = 0
        self.prev_sei_ts: int | None = None
        self.latency_sum_ms = 0.0
        self.started = time.monotonic()
        self.prev_wall: float | None = None
        self.fps_window: list[float] = []

    def frame(self, au: AccessUnit) -> None:
        # Use the arrival time of the frame's last NAL, not the print time: a
        # frame is only known to be complete once the next one starts, which
        # would otherwise add one frame interval to latency and skew fps.
        now = au.arrival if au.arrival is not None else time.time()
        self.frames += 1
        self.total_bytes += au.size
        if au.is_keyframe:
            self.keyframes += 1

        parts = [
            f"Frame {self.frames - 1:5d}:",
            f"{au.size:7d} B",
            au.keyframe_label,
            f"nals={au.type_summary()}",
        ]

        if self.live:
            self.fps_window.append(now)
            self.fps_window = [t for t in self.fps_window if now - t <= 1.0]
            if len(self.fps_window) > 1:
                span = self.fps_window[-1] - self.fps_window[0]
                fps = (len(self.fps_window) - 1) / span if span > 0 else 0.0
                parts.append(f"fps={fps:4.1f}")

        if self.sei_detection:
            parts.append(self._sei_part(au, now))

        print("  ".join(parts))

    def _sei_part(self, au: AccessUnit, now: float) -> str:
        found: SeiTimestamp | None = None
        for payload in au.sei_payloads():
            self.sei_nals += 1
            if found is None:
                found = extract_sei_timestamp(payload, self.verbose)
        if found is None:
            return "sei=none"

        self.sei_frames += 1
        ts = found.timestamp_us
        delta = (
            f"  Δ {(ts - self.prev_sei_ts) / 1000:.1f}ms"
            if self.prev_sei_ts is not None
            else ""
        )
        self.prev_sei_ts = ts
        frame_id = f"  id={found.frame_id}" if found.frame_id is not None else ""
        if self.live:
            latency_ms = (now * 1_000_000 - ts) / 1000
            self.latency_sum_ms += latency_ms
            return (
                f"latency={latency_ms:6.1f}ms  format={found.format}{frame_id}{delta}"
            )
        return f"ts={_iso(ts)}  format={found.format}{frame_id}{delta}"

    def summary(self) -> None:
        elapsed = time.monotonic() - self.started
        print()
        print(
            f"Frames: {self.frames}  (keyframes: {self.keyframes}, bytes: {self.total_bytes})"
        )
        if self.live and elapsed > 0 and self.frames:
            print(
                f"Average: {self.frames / elapsed:.1f} fps, "
                f"{self.total_bytes * 8 / elapsed / 1000:.0f} kbps over {elapsed:.1f}s"
            )
        if self.sei_detection:
            line = f"SEI NAL units: {self.sei_nals}, frames with timestamp: {self.sei_frames}"
            if self.live and self.sei_frames:
                line += f", mean latency {self.latency_sum_ms / self.sei_frames:.1f}ms"
            print(line)


class RawReporter:
    """Fallback for streams without H.264 framing: fixed-size NV12 frames or
    plain byte throughput."""

    def __init__(self, frame_bytes: int | None):
        self.frame_bytes = frame_bytes
        self.pending = 0
        self.frames = 0
        self.total_bytes = 0
        self.started = time.monotonic()
        self.last_report = self.started

    def feed(self, data: bytes) -> None:
        self.total_bytes += len(data)
        if self.frame_bytes:
            self.pending += len(data)
            while self.pending >= self.frame_bytes:
                self.pending -= self.frame_bytes
                self.frames += 1
                print(f"Frame {self.frames - 1:5d}: {self.frame_bytes:9d} B  raw")
            return
        now = time.monotonic()
        if now - self.last_report >= 1.0:
            rate = self.total_bytes / (now - self.started)
            print(
                f"Raw bytes: {self.total_bytes}  ({rate / 1e6:.2f} MB/s, no H.264 framing detected)"
            )
            self.last_report = now

    def summary(self) -> None:
        elapsed = time.monotonic() - self.started
        print()
        if self.frame_bytes:
            print(
                f"Raw frames: {self.frames}  (bytes: {self.total_bytes}, partial: {self.pending})"
            )
            if elapsed > 0 and self.frames:
                print(f"Average: {self.frames / elapsed:.1f} fps over {elapsed:.1f}s")
        else:
            print(
                f"Raw bytes: {self.total_bytes} over {elapsed:.1f}s (no H.264 framing detected)"
            )


# ---------------------------------------------------------------------------
# Modes
# ---------------------------------------------------------------------------


def parse_nv12_arg(value: str) -> int:
    try:
        w, h = (int(v) for v in value.lower().split("x"))
    except ValueError:
        raise argparse.ArgumentTypeError(
            "expected WIDTHxHEIGHT, e.g. 1280x720"
        ) from None
    return w * h * 3 // 2


def _resolve_codec(
    codec: str, data: bytes | bytearray, stream_format: str
) -> tuple[str, str]:
    """Return (codec, how) where how is 'forced' or 'auto-detected'."""
    if codec in SYNTAX:
        return codec, "forced"
    return detect_codec(data, stream_format) or "h264", "auto-detected"


def parse_file(
    filename: str,
    stream_format: str | None,
    sei_detection: bool,
    nv12_frame_bytes: int | None,
    verbose: bool,
    codec: str = "auto",
) -> int:
    data = Path(filename).read_bytes()
    detected = stream_format or (
        "nv12" if nv12_frame_bytes else detect_stream_format(data)
    )

    print(f"Parsing: {filename}")
    print(f"File size: {len(data)} bytes")
    print(f"Format: {detected or 'unknown (no H.264/H.265 framing detected)'}")

    if detected not in ("byte-stream", "avc"):
        print()
        raw = RawReporter(nv12_frame_bytes)
        raw.feed(data)
        raw.summary()
        return 0

    codec, how = _resolve_codec(codec, data, detected)
    print(f"Codec: {codec} ({how})")
    print()
    syntax = SYNTAX[codec]

    reporter = FrameReporter(live=False, sei_detection=sei_detection, verbose=verbose)
    assembler = AccessUnitAssembler(syntax)
    for nal in split_nalus(bytearray(data), detected, final=True, syntax=syntax):
        au = assembler.feed(nal)
        if au:
            reporter.frame(au)
    last = assembler.flush()
    if last:
        reporter.frame(last)
    reporter.summary()
    return 0


def parse_tcp_stream(
    host: str,
    port: int,
    stream_format: str | None,
    sei_detection: bool,
    nv12_frame_bytes: int | None,
    verbose: bool,
    codec: str = "auto",
) -> int:
    print("\n========================")
    print(f"Connecting to {host}:{port}...")
    try:
        sock = socket.create_connection((host, port))
    except OSError as exc:
        print(f"ERROR: Failed to connect to {host}:{port}: {exc}")
        return 1
    print(f"Connected to {host}:{port}")

    detected = stream_format or ("nv12" if nv12_frame_bytes else None)
    if detected:
        print(f"Format: {detected}")
    else:
        print("Format: detecting...")

    if sei_detection:
        print("========================")
        print(f"\nNote: {LATENCY_NOTE}")
        print("\n========================")

    buffer = bytearray()
    arrivals = ArrivalTracker()
    reporter: FrameReporter | None = None
    raw: RawReporter | None = None
    syntax: NalSyntax | None = None
    assembler: AccessUnitAssembler | None = None

    try:
        while True:
            data = sock.recv(65536)
            if not data:
                print("\nConnection closed by server")
                break
            buffer.extend(data)
            arrivals.extend(len(data), time.time())

            if detected is None:
                if len(buffer) < 8:
                    continue
                detected = detect_stream_format(buffer)
                if detected is None and len(buffer) < 65536:
                    continue
                print(
                    f"Format: {detected or 'unknown (no H.264/H.265 framing detected)'}"
                )
                if detected not in ("byte-stream", "avc"):
                    print()

            if detected in ("byte-stream", "avc"):
                if syntax is None:
                    # Auto-detection wants a few NAL headers; a single one
                    # (e.g. an H.264 SEI 06 05) can be ambiguous.
                    if (
                        codec not in SYNTAX
                        and len(nal_headers(buffer, detected)) < 3
                        and len(buffer) < 65536
                    ):
                        continue
                    resolved, how = _resolve_codec(codec, buffer, detected)
                    print(f"Codec: {resolved} ({how})")
                    print()
                    syntax = SYNTAX[resolved]
                    assembler = AccessUnitAssembler(syntax)
                assert assembler is not None
                if reporter is None:
                    reporter = FrameReporter(
                        live=True, sei_detection=sei_detection, verbose=verbose
                    )
                tracked = len(buffer)
                for nal in split_nalus(buffer, detected, syntax=syntax):
                    # The splitter has already removed this NAL (and anything
                    # before it) from the buffer; stamp it with the arrival of
                    # its last byte.
                    arrival = arrivals.consume(tracked - len(buffer))
                    tracked = len(buffer)
                    au = assembler.feed(nal, arrival)
                    if au:
                        reporter.frame(au)
                arrivals.consume(
                    tracked - len(buffer)
                )  # leading garbage dropped by the splitter
            else:
                if raw is None:
                    raw = RawReporter(nv12_frame_bytes)
                raw.feed(bytes(buffer))
                buffer.clear()
    except KeyboardInterrupt:
        print("\n\nInterrupted")
    finally:
        sock.close()
        if reporter is not None:
            reporter.summary()
        elif raw is not None:
            raw.summary()
        else:
            print(f"\nNo data received ({len(buffer)} bytes buffered)")
    return 0


def build_arg_parser(
    prog: str = "parse-h264", default_codec: str = "auto"
) -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog=prog,
        description=(
            "Inspect an H.264 / H.265 stream from a file or TCP server: logs every "
            "frame (size, NAL types, keyframe, fps) and optionally decodes SEI "
            "timestamps."
        ),
    )
    p.add_argument(
        "file", nargs="?", default=None, help="path to an H.264 / H.265 file"
    )
    p.add_argument(
        "--tcp",
        action="store_true",
        help="connect to a TCP server instead of reading a file",
    )
    p.add_argument(
        "--host", default="localhost", help="TCP server host (default: localhost)"
    )
    p.add_argument(
        "--port", type=int, default=5004, help="TCP server port (default: 5004)"
    )
    p.add_argument(
        "--stream-format",
        choices=["byte-stream", "avc"],
        default=None,
        help="framing ('avc' = 4-byte length prefixes, also for H.265 "
        "hvc1/hev1); auto-detected when omitted",
    )
    p.add_argument(
        "--codec",
        choices=["auto", "h264", "h265"],
        default=default_codec,
        help=f"codec of the NAL units; auto detects it from the NAL headers "
        f"(default: {default_codec})",
    )
    p.add_argument(
        "--nv12",
        type=parse_nv12_arg,
        default=None,
        metavar="WxH",
        help="treat the stream as raw NV12 frames of this size (e.g. 1280x720)",
    )
    p.add_argument(
        "--sei-detection",
        "--sei_detection",
        dest="sei_detection",
        action="store_true",
        help=(
            "decode gstreamer-source SEI timestamps (file: embedded capture time; "
            "TCP: latency from capture to receive, including encode, not transport alone)"
        ),
    )
    p.add_argument(
        "-v", "--verbose", action="store_true", help="print SEI message details"
    )
    return p


def main(
    argv: list[str] | None = None,
    prog: str = "parse-h264",
    default_codec: str = "auto",
) -> int:
    parser = build_arg_parser(prog, default_codec)
    args = parser.parse_args(argv)

    if args.tcp:
        return parse_tcp_stream(
            args.host,
            args.port,
            args.stream_format,
            args.sei_detection,
            args.nv12,
            args.verbose,
            args.codec,
        )
    if not args.file:
        parser.error("a file path is required unless --tcp is given")
    if not Path(args.file).exists():
        print(f"ERROR: File not found: {args.file}")
        return 1
    return parse_file(
        args.file,
        args.stream_format,
        args.sei_detection,
        args.nv12,
        args.verbose,
        args.codec,
    )


def main_h265(argv: list[str] | None = None) -> int:
    """`parse-h265`: parse-h264 with --codec defaulting to h265."""
    return main(argv, prog="parse-h265", default_codec="h265")


if __name__ == "__main__":
    sys.exit(main())
