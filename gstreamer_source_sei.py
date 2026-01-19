#!/usr/bin/env python3
"""Generate H.264 streams with SEI timestamp metadata for E2E latency measurement.

Supports two output modes:
- file: Write to H.264 file (Annex B byte-stream format)
- tcp: Stream via TCP server sink

SEI timestamp format:
- UUID: 3fa85f6457174562b3fc2c963f66afa6 (16 bytes)
- Timestamp: 8 bytes, big-endian, microseconds since Unix epoch

Usage:
    # File output (for testing with parse_h264_sei.py)
    uv run python gstreamer_source_sei.py --output file --path test.h264 --duration 5

    # TCP output (for live streaming tests)
    uv run python gstreamer_source_sei.py --output tcp --port 5004

    # Camera source instead of test pattern
    uv run python gstreamer_source_sei.py --camera --output file --path camera.h264
"""

import argparse
import platform
import struct
import sys
import time

import gi

gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst

# UUID for SEI timestamp messages (matches sample file and publisher/viewer)
SEI_UUID = bytes.fromhex("3fa85f6457174562b3fc2c963f66afa6")


def create_sei_nalu(timestamp_us: int | None = None) -> bytes:
    """Create an SEI NAL unit containing a timestamp.

    Args:
        timestamp_us: Timestamp in microseconds. If None, uses current time.

    Returns:
        Bytes containing a complete SEI NAL unit with timestamp.
    """
    if timestamp_us is None:
        timestamp_us = int(time.time() * 1_000_000)

    # Build SEI payload: UUID (16 bytes) + timestamp (8 bytes, big-endian)
    payload = SEI_UUID + struct.pack(">Q", timestamp_us)
    payload_type = 5  # user_data_unregistered
    payload_size = len(payload)  # 24 bytes

    # Build SEI NAL unit
    sei_nalu = bytearray()

    # Annex B start code
    sei_nalu.extend([0x00, 0x00, 0x00, 0x01])

    # NAL header: nal_ref_idc=0, nal_unit_type=6 (SEI)
    sei_nalu.append(0x06)

    # Payload type (single byte since 5 < 255)
    sei_nalu.append(payload_type)

    # Payload size (single byte since 24 < 255)
    sei_nalu.append(payload_size)

    # Payload data
    sei_nalu.extend(payload)

    # RBSP trailing bits (stop bit + alignment)
    sei_nalu.append(0x80)

    return bytes(sei_nalu)


class SeiInjector:
    """Handles SEI timestamp injection via pad probe."""

    def __init__(self):
        self.probe_id: int = 0
        self.frame_count: int = 0

    def probe_callback(
        self, pad: Gst.Pad, info: Gst.PadProbeInfo
    ) -> Gst.PadProbeReturn:
        """Probe callback to inject SEI NAL unit before each frame."""
        buffer = info.get_buffer()
        if not buffer:
            return Gst.PadProbeReturn.OK

        # Create SEI NAL unit with current timestamp
        sei_nalu = create_sei_nalu()

        # Map the original buffer to read its data
        success, map_info = buffer.map(Gst.MapFlags.READ)
        if not success:
            return Gst.PadProbeReturn.OK

        original_data = bytes(map_info.data)
        buffer.unmap(map_info)

        # Create new buffer with SEI + original data
        new_data = sei_nalu + original_data
        new_buffer = Gst.Buffer.new_wrapped(new_data)

        # Copy buffer metadata
        new_buffer.pts = buffer.pts
        new_buffer.dts = buffer.dts
        new_buffer.duration = buffer.duration
        new_buffer.offset = buffer.offset
        new_buffer.offset_end = buffer.offset_end

        # Remove probe temporarily to avoid recursion
        pad.remove_probe(self.probe_id)

        # Push modified buffer
        pad.push(new_buffer)

        # Re-add the probe
        self.probe_id = pad.add_probe(Gst.PadProbeType.BUFFER, self.probe_callback)

        self.frame_count += 1
        if self.frame_count % 30 == 0:
            print(f"Injected SEI into {self.frame_count} frames")

        # Drop the original buffer
        return Gst.PadProbeReturn.DROP


def get_camera_source():
    """Get the appropriate camera source element based on the platform."""
    system = platform.system()

    if system == "Darwin":  # macOS
        return ("avfvideosrc", {"device-index": 0})
    elif system == "Linux":
        return ("v4l2src", {"device": "/dev/video0"})
    else:
        return None


def create_pipeline(
    output_mode: str,
    output_path: str | None,
    port: int,
    use_camera: bool,
    fps: int,
    duration: int | None,
) -> tuple[Gst.Pipeline, SeiInjector] | None:
    """Create GStreamer pipeline for H.264 stream with SEI timestamps.

    Args:
        output_mode: 'file' or 'tcp'
        output_path: Path for file output
        port: Port for TCP output
        use_camera: Use camera instead of test pattern
        fps: Frames per second
        duration: Duration in seconds (file mode only)

    Returns:
        Tuple of (pipeline, sei_injector) or None on failure.
    """
    pipeline = Gst.Pipeline.new("sei-timestamp-pipeline")

    # Source
    if use_camera:
        camera_info = get_camera_source()
        if camera_info is None:
            print(f"ERROR: Camera not supported on {platform.system()}")
            return None

        element_name, properties = camera_info
        src = Gst.ElementFactory.make(element_name, "source")
        if src is None:
            print(f"ERROR: Could not create {element_name}")
            return None

        for prop, value in properties.items():
            src.set_property(prop, value)

        videoconvert = Gst.ElementFactory.make("videoconvert", "convert")
        videorate = Gst.ElementFactory.make("videorate", "rate")
        videoscale = Gst.ElementFactory.make("videoscale", "scale")
        print(f"Using camera ({element_name}) @ {fps} FPS")
    else:
        src = Gst.ElementFactory.make("videotestsrc", "source")
        src.set_property("pattern", "ball")
        src.set_property("is-live", True)
        videoconvert = None
        videorate = None
        videoscale = None

        # For file output with duration, use num-buffers
        if output_mode == "file" and duration:
            src.set_property("num-buffers", fps * duration)

        print(f"Using test pattern @ {fps} FPS")

    # Caps filter for resolution and framerate
    capsfilter = Gst.ElementFactory.make("capsfilter", "caps")
    caps = Gst.Caps.from_string(
        f"video/x-raw,format=I420,width=1280,height=720,framerate={fps}/1"
    )
    capsfilter.set_property("caps", caps)

    # Encoder
    encoder = Gst.ElementFactory.make("x264enc", "encoder")
    encoder.set_property("speed-preset", "ultrafast")
    # encoder.set_property("tune", "zerolatency") # FIXME: doesn't work with lk cli
    encoder.set_property("key-int-max", fps)  # Keyframe every second
    encoder.set_property("bframes", 0)

    # H.264 parser
    parser = Gst.ElementFactory.make("h264parse", "parser")
    parser.set_property("config-interval", 1)  # Insert SPS/PPS frequently

    # Output caps - byte-stream for file, avc for TCP
    output_caps = Gst.ElementFactory.make("capsfilter", "output-caps")
    if output_mode == "file":
        output_caps_str = Gst.Caps.from_string("video/x-h264,stream-format=byte-stream")
    else:
        # TODO: output_caps_str = Gst.Caps.from_string("video/x-h264,stream-format=avc")
        output_caps_str = Gst.Caps.from_string("video/x-h264,stream-format=byte-stream")
    output_caps.set_property("caps", output_caps_str)

    # Queue
    queue = Gst.ElementFactory.make("queue", "queue")

    # Sink
    if output_mode == "file":
        sink = Gst.ElementFactory.make("filesink", "sink")
        sink.set_property("location", output_path)
        print(f"Output: {output_path}")
    else:
        sink = Gst.ElementFactory.make("tcpserversink", "sink")
        sink.set_property("host", "0.0.0.0")
        sink.set_property("port", port)
        sink.set_property("sync", False)
        print(f"Output: TCP port {port}")

    # Check all elements created
    elements = [src, capsfilter, encoder, parser, output_caps, queue, sink]
    if use_camera:
        elements.extend([videoconvert, videorate, videoscale])

    if not all(elements):
        print("ERROR: Not all elements could be created")
        return None

    # Add elements to pipeline
    pipeline.add(src)
    if use_camera:
        pipeline.add(videoconvert)
        pipeline.add(videorate)
        pipeline.add(videoscale)
    pipeline.add(capsfilter)
    pipeline.add(encoder)
    pipeline.add(parser)
    pipeline.add(output_caps)
    pipeline.add(queue)
    pipeline.add(sink)

    # Link elements
    if use_camera:
        src.link(videoconvert)
        videoconvert.link(videorate)
        videorate.link(videoscale)
        videoscale.link(capsfilter)
    else:
        src.link(capsfilter)

    capsfilter.link(encoder)
    encoder.link(parser)
    parser.link(output_caps)
    output_caps.link(queue)
    queue.link(sink)

    # Setup SEI injection on encoder output
    sei_injector = SeiInjector()
    src_pad = encoder.get_static_pad("src")
    if src_pad:
        sei_injector.probe_id = src_pad.add_probe(
            Gst.PadProbeType.BUFFER, sei_injector.probe_callback
        )
        print("SEI injection enabled (big-endian microseconds)")
    else:
        print("WARNING: Could not setup SEI injection")

    return pipeline, sei_injector


def main():
    parser = argparse.ArgumentParser(
        description="Generate H.264 streams with SEI timestamp metadata"
    )
    parser.add_argument(
        "--output",
        choices=["file", "tcp"],
        default="file",
        help="Output mode: 'file' or 'tcp' (default: file)",
    )
    parser.add_argument(
        "--path",
        type=str,
        default="output_sei.h264",
        help="Output file path (file mode only, default: output_sei.h264)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=5004,
        help="TCP port (tcp mode only, default: 5004)",
    )
    parser.add_argument(
        "--camera",
        action="store_true",
        help="Use camera instead of test pattern",
    )
    parser.add_argument(
        "--fps",
        type=int,
        default=30,
        help="Frames per second (default: 30)",
    )
    parser.add_argument(
        "--duration",
        type=int,
        default=5,
        help="Duration in seconds (file mode only, default: 5)",
    )

    args = parser.parse_args()

    # Initialize GStreamer
    Gst.init(None)

    # Create pipeline
    result = create_pipeline(
        output_mode=args.output,
        output_path=args.path,
        port=args.port,
        use_camera=args.camera,
        fps=args.fps,
        duration=args.duration if args.output == "file" else None,
    )

    if result is None:
        sys.exit(1)

    pipeline, sei_injector = result

    # Setup bus for messages
    loop = GLib.MainLoop()
    bus = pipeline.get_bus()
    bus.add_signal_watch()

    def on_message(bus, msg):
        if msg.type == Gst.MessageType.EOS:
            print(
                f"\nEOS reached. Injected SEI into {sei_injector.frame_count} frames."
            )
            loop.quit()
        elif msg.type == Gst.MessageType.ERROR:
            error, debug = msg.parse_error()
            print(f"ERROR: {error}")
            print(f"Debug: {debug}")
            loop.quit()

    bus.connect("message", on_message)

    # Start pipeline
    print("Starting pipeline...")
    pipeline.set_state(Gst.State.PLAYING)

    try:
        loop.run()
    except KeyboardInterrupt:
        print(f"\nInterrupted. Injected SEI into {sei_injector.frame_count} frames.")
    finally:
        pipeline.set_state(Gst.State.NULL)


if __name__ == "__main__":
    main()
