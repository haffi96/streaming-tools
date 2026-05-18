#!/usr/bin/env python3
"""Generate H.264 streams with SEI timestamp/frame ID metadata for E2E latency measurement.

Supports output modes:
- file: Write to H.264 file (Annex B byte-stream format)
- tcp: Stream H.264 via TCP server sink
- raw-nv12: Stream raw NV12 frames via TCP server sink (fixed-size frames)

SEI metadata format:
- UUID: 3fa85f6457174562b3fc2c963f66afa6 (16 bytes)
- LKTS packet trailer carrying the user timestamp and frame ID

Usage:
    # File output (for testing with parse_h264_sei.py)
    uv run python gstreamer_source_sei.py --output file --path test.h264 --duration 5 --stream-format byte-stream

    # TCP output (for live streaming tests)
    uv run python gstreamer_source_sei.py --output tcp --port 5004 --stream-format byte-stream

    # Raw NV12 TCP output (fixed-size frames)
    uv run python gstreamer_source_sei.py --output raw-nv12 --port 5004 --width 1280 --height 720 --fps 30

    # Camera source instead of test pattern
    uv run python gstreamer_source_sei.py --camera --output file --path camera.h264 --stream-format byte-stream
"""

import argparse
import glob
import platform
import struct
import subprocess
import sys
import time
import logging

import gi

gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst

logging.basicConfig(
    level="INFO",
    format="[%(asctime)s] | %(levelname)-8s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler()],
)

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

    # Build NAL unit content (without framing)
    nal_content = bytearray()

    # NAL header: nal_ref_idc=0, nal_unit_type=6 (SEI)
    nal_content.append(0x06)

    # Payload type (single byte since 5 < 255)
    nal_content.append(payload_type)

    # Payload size (single byte for the current timestamp/frame ID payload)
    nal_content.append(payload_size)

    # Payload data
    nal_content.extend(payload)

    # RBSP trailing bits (stop bit + alignment)
    nal_content.append(0x80)

    # Add framing based on stream format
    sei_nalu = bytearray()

    if stream_format == "avc":
        # AVC format: 4-byte big-endian length prefix
        nal_length = len(nal_content)
        sei_nalu.extend(struct.pack(">I", nal_length))
    else:
        # Annex B format: start code
        sei_nalu.extend([0x00, 0x00, 0x00, 0x01])

    sei_nalu.extend(nal_content)

    return bytes(sei_nalu)


class SeiInjector:
    """Handles SEI metadata injection via pad probe."""

    def __init__(self, stream_format: str = "byte-stream"):
        self.stream_format = stream_format
        self.probe_id: int = 0
        self.frame_count: int = 0
        self.next_frame_id: int = 1

    def probe_callback(
        self, pad: Gst.Pad, info: Gst.PadProbeInfo
    ) -> Gst.PadProbeReturn:
        """Probe callback to inject SEI NAL unit before each frame."""
        buffer = info.get_buffer()
        if not buffer:
            return Gst.PadProbeReturn.OK

        frame_id = self.next_frame_id
        self.next_frame_id = (self.next_frame_id % 0xFFFFFFFF) + 1

        # Create SEI NAL unit with current timestamp/frame ID and matching format
        sei_nalu = create_sei_nalu(
            frame_id=frame_id,
            stream_format=self.stream_format,
        )

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
            logging.debug(f"Injected SEI into {self.frame_count} frames")

        # Drop the original buffer
        return Gst.PadProbeReturn.DROP


def enumerate_cameras_macos() -> list[dict]:
    """Enumerate available cameras on macOS using avfvideosrc."""
    cameras = []
    # Use GStreamer device monitor to discover video sources
    monitor = Gst.DeviceMonitor.new()
    monitor.add_filter("Video/Source", None)
    monitor.start()
    devices = monitor.get_devices()

    for i, device in enumerate(devices):
        display_name = device.get_display_name()
        properties = device.get_properties()
        device_index = i
        if properties:
            # avfvideosrc uses integer device-index
            idx = properties.get_value("device.api")
            if idx:
                pass  # just informational
        cameras.append(
            {
                "index": device_index,
                "name": display_name,
                "element": "avfvideosrc",
                "properties": {"device-index": device_index},
            }
        )

    monitor.stop()
    return cameras


def enumerate_cameras_linux() -> list[dict]:
    """Enumerate available cameras on Linux using v4l2."""
    cameras = []
    video_devices = sorted(glob.glob("/dev/video*"))

    for device_path in video_devices:
        name = device_path
        # Try to get a friendly name via v4l2-ctl
        try:
            result = subprocess.run(
                ["v4l2-ctl", "--device", device_path, "--info"],
                capture_output=True,
                text=True,
                timeout=2,
            )
            if result.returncode == 0:
                for line in result.stdout.splitlines():
                    line = line.strip()
                    if line.startswith("Card type"):
                        name = line.split(":", 1)[1].strip()
                        break
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass

        # Verify the device can actually capture video by checking capabilities
        try:
            result = subprocess.run(
                ["v4l2-ctl", "--device", device_path, "--all"],
                capture_output=True,
                text=True,
                timeout=2,
            )
            if result.returncode == 0 and "Video Capture" not in result.stdout:
                continue  # Skip non-capture devices (e.g. metadata nodes)
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass  # If v4l2-ctl not available, include the device anyway

        cameras.append(
            {
                "index": len(cameras),
                "name": f"{name} ({device_path})",
                "element": "v4l2src",
                "properties": {"device": device_path},
            }
        )

    return cameras


def enumerate_cameras() -> list[dict]:
    """Enumerate available cameras on the current platform."""
    system = platform.system()
    if system == "Darwin":
        return enumerate_cameras_macos()
    elif system == "Linux":
        return enumerate_cameras_linux()
    else:
        logging.warning(f"Camera enumeration not supported on {system}")
        return []


def prompt_camera_selection() -> tuple[str, dict] | None:
    """Prompt the user to pick from available cameras.

    Returns:
        Tuple of (element_name, properties_dict) or None if no camera selected.
    """
    cameras = enumerate_cameras()

    if not cameras:
        logging.error("No cameras found on this system.")
        return None

    if len(cameras) == 1:
        cam = cameras[0]
        logging.info(f"Found 1 camera: {cam['name']}")
        return (cam["element"], cam["properties"])

    print("\nAvailable cameras:")
    for cam in cameras:
        print(f"  [{cam['index']}] {cam['name']}")

    while True:
        try:
            choice = input(
                f"\nSelect camera [0-{len(cameras) - 1}] (default: 0): "
            ).strip()
            if choice == "":
                idx = 0
            else:
                idx = int(choice)
            if 0 <= idx < len(cameras):
                cam = cameras[idx]
                logging.info(f"Selected: {cam['name']}")
                return (cam["element"], cam["properties"])
            else:
                logging.error(f"Invalid choice. Enter a number between 0 and {len(cameras) - 1}.")
        except ValueError:
            logging.error("Invalid input. Enter a number.")
        except (EOFError, KeyboardInterrupt):
            logging.error("\nCancelled.")
            return None


def get_camera_source():
    """Get the appropriate camera source element by prompting the user."""
    return prompt_camera_selection()


def create_pipeline(
    output_mode: str,
    output_path: str | None,
    port: int,
    use_camera: bool,
    fps: int,
    width: int,
    height: int,
    duration: int | None,
    stream_format: str = "byte-stream",
) -> tuple[Gst.Pipeline, SeiInjector | None] | None:
    """Create GStreamer pipeline for H.264 stream with SEI metadata or raw NV12.

    Args:
        output_mode: 'file', 'tcp', or 'raw-nv12'
        output_path: Path for file output
        port: Port for TCP output
        use_camera: Use camera instead of test pattern
        fps: Frames per second
        width: Frame width
        height: Frame height
        duration: Duration in seconds (file mode only)
        stream_format: 'byte-stream' for Annex B or 'avc' for length-prefixed

    Returns:
        Tuple of (pipeline, sei_injector) or None on failure.
    """
    pipeline = Gst.Pipeline.new("sei-timestamp-pipeline")

    is_raw_nv12 = output_mode == "raw-nv12"

    # Source
    if use_camera:
        camera_info = get_camera_source()
        if camera_info is None:
            logging.error("No camera selected")
            return None

        element_name, properties = camera_info
        src = Gst.ElementFactory.make(element_name, "source")
        if src is None:
            logging.error(f"Could not create {element_name}")
            return None

        for prop, value in properties.items():
            src.set_property(prop, value)

        videoconvert = Gst.ElementFactory.make("videoconvert", "convert")
        videorate = Gst.ElementFactory.make("videorate", "rate")
        videoscale = Gst.ElementFactory.make("videoscale", "scale")
        logging.info(f"Using camera ({element_name}) @ {fps} FPS")
    else:
        src = Gst.ElementFactory.make("videotestsrc", "source")
        src.set_property("pattern", "ball")
        src.set_property("is-live", True)
        if is_raw_nv12:
            videoconvert = Gst.ElementFactory.make("videoconvert", "convert")
            videorate = Gst.ElementFactory.make("videorate", "rate")
            videoscale = Gst.ElementFactory.make("videoscale", "scale")
        else:
            videoconvert = None
            videorate = None
            videoscale = None

        # For file output with duration, use num-buffers
        if output_mode == "file" and duration:
            src.set_property("num-buffers", fps * duration)

        logging.info(f"Using test pattern @ {fps} FPS")

    # Caps filter for resolution and framerate
    capsfilter = Gst.ElementFactory.make("capsfilter", "caps")
    if is_raw_nv12:
        caps = Gst.Caps.from_string(
            f"video/x-raw,format=NV12,width={width},height={height},framerate={fps}/1"
        )
    else:
        caps = Gst.Caps.from_string(
            f"video/x-raw,format=I420,width={width},height={height},framerate={fps}/1"
        )
    capsfilter.set_property("caps", caps)

    if not is_raw_nv12:
        # Encoder
        encoder = Gst.ElementFactory.make("x264enc", "encoder")
        encoder.set_property("speed-preset", "ultrafast")
        # encoder.set_property("tune", "zerolatency") # doesn't work with lk cli
        encoder.set_property("key-int-max", fps)  # Keyframe every second
        encoder.set_property("bframes", 0)

        # H.264 parser
        parser = Gst.ElementFactory.make("h264parse", "parser")
        parser.set_property("config-interval", 1)  # Insert SPS/PPS frequently

        # Output caps based on stream format
        output_caps = Gst.ElementFactory.make("capsfilter", "output-caps")
        output_caps_str = Gst.Caps.from_string(
            f"video/x-h264,stream-format={stream_format}"
        )
        output_caps.set_property("caps", output_caps_str)

    # Queue
    queue = Gst.ElementFactory.make("queue", "queue")

    # Sink
    if output_mode == "file":
        sink = Gst.ElementFactory.make("filesink", "sink")
        sink.set_property("location", output_path)
        logging.info(f"Output: {output_path}")
    else:
        sink = Gst.ElementFactory.make("tcpserversink", "sink")
        sink.set_property("host", "0.0.0.0")
        sink.set_property("port", port)
        sink.set_property("sync", False)
        if is_raw_nv12:
            logging.info(f"Output: TCP port {port} (raw NV12)")
        else:
            logging.info(f"Output: TCP port {port}")

    # Check all elements created
    if is_raw_nv12:
        elements = [src, capsfilter, queue, sink]
    else:
        elements = [src, capsfilter, encoder, parser, output_caps, queue, sink]
    if use_camera:
        elements.extend([videoconvert, videorate, videoscale])
    elif is_raw_nv12:
        elements.extend([videoconvert, videorate, videoscale])

    if not all(elements):
        logging.error("Not all elements could be created")
        return None

    # Add elements to pipeline
    pipeline.add(src)
    if use_camera:
        pipeline.add(videoconvert)
        pipeline.add(videorate)
        pipeline.add(videoscale)
    elif is_raw_nv12:
        pipeline.add(videoconvert)
        pipeline.add(videorate)
        pipeline.add(videoscale)
    pipeline.add(capsfilter)
    if not is_raw_nv12:
        pipeline.add(encoder)
        pipeline.add(parser)
        pipeline.add(output_caps)
    pipeline.add(queue)
    pipeline.add(sink)

    # Link elements
    if use_camera or is_raw_nv12:
        src.link(videoconvert)
        videoconvert.link(videorate)
        videorate.link(videoscale)
        videoscale.link(capsfilter)
    else:
        src.link(capsfilter)

    if is_raw_nv12:
        capsfilter.link(queue)
        queue.link(sink)
    else:
        capsfilter.link(encoder)
        encoder.link(parser)
        parser.link(output_caps)
        output_caps.link(queue)
        queue.link(sink)

    sei_injector = None
    if not is_raw_nv12:
        # Setup SEI injection on encoder output
        sei_injector = SeiInjector(stream_format)
        src_pad = encoder.get_static_pad("src")
        if src_pad:
            sei_injector.probe_id = src_pad.add_probe(
                Gst.PadProbeType.BUFFER, sei_injector.probe_callback
            )
            logging.info(f"SEI injection enabled (format: {stream_format})")
        else:
            logging.warning("Could not setup SEI injection")

    return pipeline, sei_injector


def main():
    parser = argparse.ArgumentParser(
        description="Generate H.264 streams with SEI timestamp/frame ID metadata"
    )
    parser.add_argument(
        "--output",
        choices=["file", "tcp", "raw-nv12"],
        default="file",
        help="Output mode: 'file', 'tcp', or 'raw-nv12' (default: file)",
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
        "--width",
        type=int,
        default=1280,
        help="Frame width (default: 1280)",
    )
    parser.add_argument(
        "--height",
        type=int,
        default=720,
        help="Frame height (default: 720)",
    )
    parser.add_argument(
        "--duration",
        type=int,
        default=5,
        help="Duration in seconds (file mode only, default: 5)",
    )
    parser.add_argument(
        "--stream-format",
        choices=["byte-stream", "avc"],
        default="byte-stream",
        help="H.264 stream format: 'byte-stream' (Annex B) or 'avc' (length-prefixed)",
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
        width=args.width,
        height=args.height,
        duration=args.duration if args.output == "file" else None,
        stream_format=args.stream_format,
    )

    if result is None:
        sys.exit(1)

    pipeline, sei_injector = result
    sei_count = 0

    # Setup bus for messages
    loop = GLib.MainLoop()
    bus = pipeline.get_bus()
    bus.add_signal_watch()

    def on_message(bus, msg):
        if msg.type == Gst.MessageType.EOS:
            if sei_injector:
                sei_count = sei_injector.frame_count
            logging.info(f"\nEOS reached. Injected SEI into {sei_count} frames.")
            loop.quit()
        elif msg.type == Gst.MessageType.ERROR:
            error, debug = msg.parse_error()
            logging.error(f"{error}")
            logging.info(f"Debug: {debug}")
            loop.quit()

    bus.connect("message", on_message)

    # Start pipeline
    logging.info("Starting pipeline...")
    pipeline.set_state(Gst.State.PLAYING)

    logging.info(
        "Started pipeline. Press Ctrl+C to stop."
    )

    try:
        loop.run()
    except KeyboardInterrupt:
        if sei_injector:
            sei_count = sei_injector.frame_count
        logging.info(f"\nInterrupted. Injected SEI into {sei_count} frames.")
    finally:
        pipeline.set_state(Gst.State.NULL)


if __name__ == "__main__":
    main()