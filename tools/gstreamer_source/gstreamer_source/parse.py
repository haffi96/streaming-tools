"""Inspect an H.264 (or raw NV12) stream from a file or TCP server.

Every access unit (frame) is logged with its size, NAL types and keyframe flag,
so a stream can be verified even when it carries no SEI metadata. With
--sei-detection the gstreamer-source SEI (UUID 3fa85f6457174562b3fc2c963f66afa6,
LKTS packet trailer or legacy 8-byte timestamp) is decoded as well and each
frame line gains the embedded timestamp (file) or end-to-end latency (TCP).
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

NAL_SLICE = 1
NAL_IDR = 5
NAL_SEI = 6
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


def detect_stream_format(data: bytes | bytearray) -> str | None:
    """Return 'byte-stream', 'avc' or None (no H.264 framing recognised)."""
    if len(data) < 5:
        return None
    if data[:4] == b"\x00\x00\x00\x01" or data[:3] == b"\x00\x00\x01":
        return "byte-stream"
    length = int.from_bytes(data[:4], "big")
    if (
        0 < length <= 4_000_000
        and (data[4] & 0x80) == 0
        and 1 <= (data[4] & 0x1F) <= 12
    ):
        # Plausible length prefix followed by a NAL header (forbidden_zero_bit clear).
        return "avc"
    if data.find(b"\x00\x00\x01", 0, 65536) != -1:
        # Joined mid-stream: a start code appears further in.
        return "byte-stream"
    return None


def find_start_code(data: bytes | bytearray, offset: int = 0) -> tuple[int, int]:
    """Return (index, length) of the next Annex B start code at or after offset,
    or (-1, 0). A 3-byte 00 00 01 preceded by 00 is reported as a 4-byte code."""
    idx = data.find(b"\x00\x00\x01", offset)
    if idx == -1:
        return -1, 0
    if idx > offset and data[idx - 1] == 0:
        return idx - 1, 4
    return idx, 3


def split_annexb(buffer: bytearray, final: bool) -> Iterator[bytes]:
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
    buffer: bytearray, stream_format: str, final: bool = False
) -> Iterator[bytes]:
    if stream_format == "avc":
        return split_avc(buffer)
    return split_annexb(buffer, final)


# ---------------------------------------------------------------------------
# Access unit assembly
# ---------------------------------------------------------------------------


@dataclass
class AccessUnit:
    nals: list[bytes] = field(default_factory=list)
    # Wall-clock time (time.time()) at which the last NAL of this AU was
    # received; None in file mode.
    arrival: float | None = None

    @property
    def size(self) -> int:
        return sum(len(n) for n in self.nals)

    @property
    def types(self) -> list[int]:
        return [n[0] & 0x1F for n in self.nals]

    @property
    def is_keyframe(self) -> bool:
        return NAL_IDR in self.types

    def type_summary(self) -> str:
        out: list[str] = []
        for t in self.types:
            name = NAL_NAMES.get(t, f"nal{t}")
            if out and out[-1].split("x")[0] == name:
                count = int(out[-1].split("x")[1]) + 1 if "x" in out[-1] else 2
                out[-1] = f"{name}x{count}"
            else:
                out.append(name)
        return ",".join(out)

    def sei_payloads(self) -> Iterator[bytes]:
        for n in self.nals:
            if n[0] & 0x1F == NAL_SEI:
                yield n[1:]


def _first_mb_in_slice_is_zero(nal: bytes) -> bool:
    # first_mb_in_slice is the first ue(v) of the slice header; ue(v) == 0 is
    # encoded as a single '1' bit.
    return len(nal) > 1 and (nal[1] & 0x80) != 0


class AccessUnitAssembler:
    """Groups NAL units into access units (frames).

    A new AU starts at an AUD, at a non-VCL NAL following VCL NALs, or at a VCL
    NAL with first_mb_in_slice == 0 following VCL NALs (multi-slice frames).
    """

    def __init__(self) -> None:
        self._current = AccessUnit()
        self._has_vcl = False

    def feed(self, nal: bytes, arrival: float | None = None) -> AccessUnit | None:
        nal_type = nal[0] & 0x1F
        is_vcl = nal_type in VCL_TYPES
        completed: AccessUnit | None = None
        if self._has_vcl and (not is_vcl or _first_mb_in_slice_is_zero(nal)):
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
        self._current = AccessUnit()
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
            "IDR" if au.is_keyframe else "   ",
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


def parse_file(
    filename: str,
    stream_format: str | None,
    sei_detection: bool,
    nv12_frame_bytes: int | None,
    verbose: bool,
) -> int:
    data = Path(filename).read_bytes()
    detected = stream_format or (
        "nv12" if nv12_frame_bytes else detect_stream_format(data)
    )

    print(f"Parsing: {filename}")
    print(f"File size: {len(data)} bytes")
    print(f"Format: {detected or 'unknown (no H.264 framing detected)'}")
    print()

    if detected not in ("byte-stream", "avc"):
        raw = RawReporter(nv12_frame_bytes)
        raw.feed(data)
        raw.summary()
        return 0

    reporter = FrameReporter(live=False, sei_detection=sei_detection, verbose=verbose)
    assembler = AccessUnitAssembler()
    for nal in split_nalus(bytearray(data), detected, final=True):
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
) -> int:
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

    buffer = bytearray()
    arrivals = ArrivalTracker()
    reporter: FrameReporter | None = None
    raw: RawReporter | None = None
    assembler = AccessUnitAssembler()

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
                print(f"Format: {detected or 'unknown (no H.264 framing detected)'}")
                print()

            if detected in ("byte-stream", "avc"):
                if reporter is None:
                    reporter = FrameReporter(
                        live=True, sei_detection=sei_detection, verbose=verbose
                    )
                tracked = len(buffer)
                for nal in split_nalus(buffer, detected):
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


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="parse-h264",
        description=(
            "Inspect an H.264 stream from a file or TCP server: logs every frame "
            "(size, NAL types, keyframe, fps) and optionally decodes SEI timestamps."
        ),
    )
    p.add_argument("file", nargs="?", default=None, help="path to an H.264 file")
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
        help="H.264 framing; auto-detected when omitted",
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
        help="decode gstreamer-source SEI timestamps (file: embedded time, TCP: latency)",
    )
    p.add_argument(
        "-v", "--verbose", action="store_true", help="print SEI message details"
    )
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    if args.tcp:
        return parse_tcp_stream(
            args.host,
            args.port,
            args.stream_format,
            args.sei_detection,
            args.nv12,
            args.verbose,
        )
    if not args.file:
        parser.error("a file path is required unless --tcp is given")
    if not Path(args.file).exists():
        print(f"ERROR: File not found: {args.file}")
        return 1
    return parse_file(
        args.file, args.stream_format, args.sei_detection, args.nv12, args.verbose
    )


if __name__ == "__main__":
    sys.exit(main())
