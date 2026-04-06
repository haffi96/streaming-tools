#!/usr/bin/env python3
"""Parse H.264 SEI timestamp metadata for latency analysis.

Extracts user_data_unregistered SEI messages (type 5) and displays timestamps.

SEI timestamp format:
- UUID: 3fa85f6457174562b3fc2c963f66afa6 (16 bytes)
- Timestamp: 8 bytes, big-endian, microseconds since Unix epoch

Usage:
    # File mode
    uv run python parse_h264_sei.py samples/annux-d-sample_timestamp_sei.h264 --stream-format byte-stream

    # TCP mode (connect to streaming source)
    uv run python parse_h264_sei.py --tcp --host localhost --port 5004 --stream-format byte-stream
"""

import argparse
import socket
import struct
import time
from datetime import datetime, timezone
from pathlib import Path

# UUID for SEI timestamp messages (matches sample file and publisher/viewer)
SEI_UUID = bytes.fromhex("3fa85f6457174562b3fc2c963f66afa6")


def detect_stream_format(data: bytes) -> str:
    """Detect H.264 stream format from data.

    Args:
        data: H.264 data

    Returns:
        'byte-stream' for Annex B format, 'avc' for length-prefixed format
    """
    if len(data) < 4:
        return "byte-stream"

    # Check for Annex B start codes
    if data[:4] == b"\x00\x00\x00\x01" or data[:3] == b"\x00\x00\x01":
        return "byte-stream"

    # Check if first 4 bytes could be a valid length prefix
    length = (data[0] << 24) | (data[1] << 16) | (data[2] << 8) | data[3]
    if 0 < length < len(data) and len(data) > 4:
        nal_type = data[4] & 0x1F
        if 1 <= nal_type <= 12:
            return "avc"

    return "byte-stream"


def find_avc_nalus(data: bytes, length_size: int = 4) -> list[tuple[int, int]]:
    """Find all NAL units in AVC format (length-prefixed) data.

    Args:
        data: H.264 AVC format data
        length_size: Size of length prefix (usually 4 bytes)

    Returns:
        List of (start_position, nalu_length) tuples
    """
    nalus = []
    i = 0
    while i + length_size <= len(data):
        if length_size == 4:
            nalu_len = (
                (data[i] << 24) | (data[i + 1] << 16) | (data[i + 2] << 8) | data[i + 3]
            )
        elif length_size == 2:
            nalu_len = (data[i] << 8) | data[i + 1]
        else:
            break

        nalu_start = i + length_size
        if nalu_start + nalu_len > len(data):
            break

        nalus.append((nalu_start, nalu_len))
        i = nalu_start + nalu_len

    return nalus


def parse_h264_sei(filename: str, verbose: bool = False):
    """Parse H.264 file and extract SEI timestamps.

    Supports both Annex B (byte-stream) and AVC (length-prefixed) formats.

    Args:
        filename: Path to H.264 file
        verbose: Print additional debug info
    """
    with open(filename, "rb") as f:
        data = f.read()

    print(f"Parsing: {filename}")
    print(f"File size: {len(data)} bytes")

    stream_format = detect_stream_format(data)
    print(f"Format: {stream_format}")
    print()

    frame_num = 0
    prev_ts = None
    sei_count = 0

    if stream_format == "avc":
        # AVC format - length-prefixed NAL units
        nalus = find_avc_nalus(data)
        for nalu_start, nalu_len in nalus:
            if nalu_start >= len(data):
                continue

            nal_type = data[nalu_start] & 0x1F

            if nal_type == 6:  # SEI
                sei_count += 1
                sei_payload = data[nalu_start + 1 : nalu_start + nalu_len]
                result = parse_sei_message(sei_payload, frame_num, prev_ts, verbose)
                if result is not None:
                    prev_ts = result
                    frame_num += 1
    else:
        # Annex B format - start code delimited
        i = 0
        while i < len(data) - 4:
            if data[i : i + 4] == b"\x00\x00\x00\x01":
                start = i + 4
            elif data[i : i + 3] == b"\x00\x00\x01":
                start = i + 3
            else:
                i += 1
                continue

            nal_type = data[start] & 0x1F

            if nal_type == 6:  # SEI
                sei_count += 1
                end = start + 1
                while end < len(data) - 3:
                    if data[end : end + 3] == b"\x00\x00\x01":
                        break
                    end += 1

                sei_payload = data[start + 1 : end]
                result = parse_sei_message(sei_payload, frame_num, prev_ts, verbose)
                if result is not None:
                    prev_ts = result
                    frame_num += 1

            i = start

    print()
    print(f"Total SEI NAL units found: {sei_count}")
    print(f"Frames with timestamp: {frame_num}")


def parse_sei_message(
    payload: bytes,
    frame_num: int,
    prev_ts: int | None,
    verbose: bool,
) -> int | None:
    """Parse SEI message and extract timestamp.

    Args:
        payload: SEI payload bytes (after NAL header)
        frame_num: Current frame number for display
        prev_ts: Previous timestamp for delta calculation
        verbose: Print debug info

    Returns:
        Timestamp in microseconds if found, None otherwise
    """
    i = 0
    while i < len(payload) - 1:
        # Parse payload_type (may be multi-byte)
        sei_type = 0
        while i < len(payload) and payload[i] == 0xFF:
            sei_type += 255
            i += 1
        if i < len(payload):
            sei_type += payload[i]
            i += 1

        # Parse payload_size (may be multi-byte)
        sei_size = 0
        while i < len(payload) and payload[i] == 0xFF:
            sei_size += 255
            i += 1
        if i < len(payload):
            sei_size += payload[i]
            i += 1

        if verbose:
            print(f"  SEI type={sei_type}, size={sei_size}")

        # Check for user_data_unregistered (type 5)
        if sei_type == 5 and i + sei_size <= len(payload):
            uuid = payload[i : i + 16]
            user_data = payload[i + 16 : i + sei_size]

            if verbose:
                print(f"  UUID: {uuid.hex()}")
                print(f"  Data: {user_data.hex()}")

            # Check UUID matches and data is 8 bytes
            if len(user_data) == 8 and uuid == SEI_UUID:
                # Parse timestamp (8 bytes, big-endian, microseconds)
                ts_us = struct.unpack(">Q", user_data)[0]
                ts_sec = ts_us / 1_000_000

                try:
                    dt = datetime.fromtimestamp(ts_sec, tz=timezone.utc)
                    dt_str = dt.isoformat()
                except (ValueError, OSError):
                    dt_str = f"<invalid: {ts_us} us>"

                delta_str = ""
                if prev_ts is not None:
                    delta_ms = (ts_us - prev_ts) / 1000  # us to ms
                    delta_str = f"  Δ {delta_ms:.2f}ms"

                print(f"Frame {frame_num:4d}: {dt_str}{delta_str}")
                return ts_us

        i += sei_size

    return None


def parse_tcp_stream(
    host: str, port: int, stream_format: str = "byte-stream", verbose: bool = False
):
    """Parse H.264 stream from TCP connection and extract SEI timestamps.

    Supports both Annex B (byte-stream) and AVC (length-prefixed) formats.

    Args:
        host: TCP server host
        port: TCP server port
        stream_format: 'byte-stream' for Annex B or 'avc' for length-prefixed
        verbose: Print additional debug info
    """
    print(f"Connecting to {host}:{port}...")
    print(f"Format: {stream_format}")

    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.connect((host, port))
        print(f"Connected to {host}:{port}")
        print()
    except ConnectionRefusedError:
        print(f"ERROR: Connection refused to {host}:{port}")
        return 1
    except Exception as e:
        print(f"ERROR: Failed to connect: {e}")
        return 1

    buffer = bytearray()
    frame_num = 0
    prev_ts = None
    sei_count = 0

    try:
        while True:
            # Read data from socket
            try:
                data = sock.recv(65536)
                if not data:
                    print("\nConnection closed by server")
                    break
                buffer.extend(data)
            except socket.timeout:
                continue
            except KeyboardInterrupt:
                raise

            if stream_format == "avc":
                # Process AVC format - length-prefixed NAL units
                while len(buffer) >= 4:
                    # Read 4-byte length prefix
                    nalu_len = (
                        (buffer[0] << 24)
                        | (buffer[1] << 16)
                        | (buffer[2] << 8)
                        | buffer[3]
                    )

                    # Check if we have the complete NAL unit
                    if len(buffer) < 4 + nalu_len:
                        break

                    # Extract NAL unit (skip length prefix)
                    nal_data = bytes(buffer[4 : 4 + nalu_len])
                    buffer = buffer[4 + nalu_len :]

                    if len(nal_data) == 0:
                        continue

                    nal_type = nal_data[0] & 0x1F

                    if nal_type == 6:  # SEI
                        sei_count += 1
                        sei_payload = nal_data[1:]
                        result = parse_sei_message_live(
                            sei_payload, frame_num, prev_ts, verbose
                        )
                        if result is not None:
                            prev_ts = result
                            frame_num += 1
            else:
                # Process byte-stream format - start code delimited
                while True:
                    # Find first start code
                    start_idx = -1
                    for i in range(len(buffer) - 3):
                        if buffer[i : i + 4] == b"\x00\x00\x00\x01":
                            start_idx = i
                            break
                        elif buffer[i : i + 3] == b"\x00\x00\x01":
                            start_idx = i
                            break

                    if start_idx == -1:
                        # No start code found, keep last 3 bytes (might be partial)
                        if len(buffer) > 3:
                            buffer = buffer[-3:]
                        break

                    # Find next start code
                    nal_start = start_idx + (
                        4
                        if buffer[start_idx : start_idx + 4] == b"\x00\x00\x00\x01"
                        else 3
                    )
                    next_idx = -1
                    for i in range(nal_start, len(buffer) - 3):
                        if buffer[i : i + 4] == b"\x00\x00\x00\x01":
                            next_idx = i
                            break
                        elif buffer[i : i + 3] == b"\x00\x00\x01":
                            next_idx = i
                            break

                    if next_idx == -1:
                        # No complete NAL unit yet, wait for more data
                        break

                    # Extract NAL unit
                    nal_data = bytes(buffer[nal_start:next_idx])
                    buffer = buffer[next_idx:]

                    if len(nal_data) == 0:
                        continue

                    nal_type = nal_data[0] & 0x1F

                    if nal_type == 6:  # SEI
                        sei_count += 1
                        sei_payload = nal_data[1:]
                        result = parse_sei_message_live(
                            sei_payload, frame_num, prev_ts, verbose
                        )
                        if result is not None:
                            prev_ts = result
                            frame_num += 1

    except KeyboardInterrupt:
        print("\n\nInterrupted")
    finally:
        sock.close()
        print()
        print(f"Total SEI NAL units found: {sei_count}")
        print(f"Frames with timestamp: {frame_num}")

    return 0


def parse_sei_message_live(
    payload: bytes,
    frame_num: int,
    prev_ts: int | None,
    verbose: bool,
) -> int | None:
    """Parse SEI message and extract timestamp with E2E latency calculation.

    Args:
        payload: SEI payload bytes (after NAL header)
        frame_num: Current frame number for display
        prev_ts: Previous timestamp for delta calculation
        verbose: Print debug info

    Returns:
        Timestamp in microseconds if found, None otherwise
    """
    i = 0
    while i < len(payload) - 1:
        # Parse payload_type (may be multi-byte)
        sei_type = 0
        while i < len(payload) and payload[i] == 0xFF:
            sei_type += 255
            i += 1
        if i < len(payload):
            sei_type += payload[i]
            i += 1

        # Parse payload_size (may be multi-byte)
        sei_size = 0
        while i < len(payload) and payload[i] == 0xFF:
            sei_size += 255
            i += 1
        if i < len(payload):
            sei_size += payload[i]
            i += 1

        if verbose:
            print(f"  SEI type={sei_type}, size={sei_size}")

        # Check for user_data_unregistered (type 5)
        if sei_type == 5 and i + sei_size <= len(payload):
            uuid = payload[i : i + 16]
            user_data = payload[i + 16 : i + sei_size]

            if verbose:
                print(f"  UUID: {uuid.hex()}")
                print(f"  Data: {user_data.hex()}")

            # Check UUID matches and data is 8 bytes
            if len(user_data) == 8 and uuid == SEI_UUID:
                # Parse timestamp (8 bytes, big-endian, microseconds)
                ts_us = struct.unpack(">Q", user_data)[0]

                # Calculate E2E latency
                current_us = int(time.time() * 1_000_000)
                latency_ms = (current_us - ts_us) / 1000

                # Frame delta
                delta_str = ""
                if prev_ts is not None:
                    delta_ms = (ts_us - prev_ts) / 1000
                    delta_str = f"  Δ {delta_ms:.1f}ms"

                print(f"Frame {frame_num:4d}: latency={latency_ms:6.1f}ms{delta_str}")
                return ts_us

        i += sei_size

    return None


def main():
    parser = argparse.ArgumentParser(description="Parse H.264 SEI timestamp metadata")
    parser.add_argument(
        "file",
        type=str,
        nargs="?",
        default=None,
        help="Path to H.264 file",
    )
    parser.add_argument(
        "--tcp",
        action="store_true",
        help="Connect to TCP server instead of reading file",
    )
    parser.add_argument(
        "--host",
        type=str,
        default="localhost",
        help="TCP server host (default: localhost)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=5004,
        help="TCP server port (default: 5004)",
    )
    parser.add_argument(
        "--stream-format",
        choices=["byte-stream", "avc"],
        default="byte-stream",
        help="H.264 stream format for TCP: 'byte-stream' (Annex B) or 'avc' (length-prefixed)",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Print verbose debug info",
    )

    args = parser.parse_args()

    if args.tcp:
        # TCP mode
        return parse_tcp_stream(args.host, args.port, args.stream_format, args.verbose)
    else:
        # File mode
        file_path = args.file or "samples/annux-d-sample_timestamp_sei.h264"
        path = Path(file_path)
        if not path.exists():
            print(f"ERROR: File not found: {file_path}")
            return 1
        parse_h264_sei(str(path), args.verbose)
        return 0


if __name__ == "__main__":
    exit(main())
