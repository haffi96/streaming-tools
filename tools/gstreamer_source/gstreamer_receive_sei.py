#!/usr/bin/env python3
"""Receive a TCP H.264 stream, display it, and surface SEI timestamp metadata.

Usage:
    # Display stream with publish timestamp overlay
    uv run python gstreamer_receive_sei.py --host localhost --port 5004

    # Headless metadata-only mode
    uv run python gstreamer_receive_sei.py --host localhost --port 5004 --headless
"""

import argparse
import logging
import sys
import time
from datetime import datetime, timezone

import gi

gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst

from parse_h264_sei import parse_packet_trailer, parse_user_data


logging.basicConfig(
    level="INFO",
    format="[%(asctime)s] | %(levelname)-8s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler()],
)


def parse_sei_metadata(payload: bytes) -> list[dict[str, int | str]]:
    """Extract supported user_data_unregistered metadata from an SEI RBSP payload."""
    metadata_items: list[dict[str, int | str]] = []
    i = 0

    while i < len(payload) - 1:
        sei_type = 0
        while i < len(payload) and payload[i] == 0xFF:
            sei_type += 255
            i += 1
        if i >= len(payload):
            break
        sei_type += payload[i]
        i += 1

        sei_size = 0
        while i < len(payload) and payload[i] == 0xFF:
            sei_size += 255
            i += 1
        if i >= len(payload):
            break
        sei_size += payload[i]
        i += 1

        if sei_type == 5 and i + sei_size <= len(payload):
            uuid = payload[i : i + 16]
            user_data = payload[i + 16 : i + sei_size]
            parsed = parse_user_data(uuid, user_data)
            if parsed is not None:
                timestamp_us, metadata_format = parsed
                item: dict[str, int | str] = {
                    "timestamp_us": timestamp_us,
                    "format": metadata_format,
                }
                trailer_metadata = parse_packet_trailer(user_data)
                if trailer_metadata and "frame_id" in trailer_metadata:
                    item["frame_id"] = trailer_metadata["frame_id"]
                metadata_items.append(item)

        i += sei_size

    return metadata_items


def parse_avc_buffer(data: bytes) -> list[dict[str, int | str]]:
    """Parse AVC length-prefixed NAL units and return SEI metadata entries."""
    metadata_items: list[dict[str, int | str]] = []
    offset = 0

    while offset + 4 <= len(data):
        nalu_len = int.from_bytes(data[offset : offset + 4], "big")
        nalu_start = offset + 4
        nalu_end = nalu_start + nalu_len
        if nalu_len <= 0 or nalu_end > len(data):
            break

        nal_data = data[nalu_start:nalu_end]
        if nal_data and nal_data[0] & 0x1F == 6:
            metadata_items.extend(parse_sei_metadata(nal_data[1:]))

        offset = nalu_end

    return metadata_items


def format_timestamp(timestamp_us: int) -> str:
    try:
        return datetime.fromtimestamp(timestamp_us / 1_000_000, tz=timezone.utc).isoformat()
    except (ValueError, OSError):
        return f"<invalid: {timestamp_us} us>"


def format_metadata_line(metadata: dict[str, int | str]) -> str:
    timestamp_us = int(metadata["timestamp_us"])
    frame_id = metadata.get("frame_id", "?")
    latency_ms = (int(time.time() * 1_000_000) - timestamp_us) / 1000
    return (
        f"frame_id={frame_id} timestamp_us={timestamp_us} "
        f"timestamp={format_timestamp(timestamp_us)} latency={latency_ms:.1f}ms"
    )


class SeiReceiver:
    def __init__(self, host: str, port: int, headless: bool):
        self.host = host
        self.port = port
        self.headless = headless
        self.pipeline: Gst.Pipeline | None = None
        self.overlay: Gst.Element | None = None

    def create_pipeline(self) -> Gst.Pipeline | None:
        pipeline = Gst.Pipeline.new("sei-receiver-pipeline")

        src = Gst.ElementFactory.make("tcpclientsrc", "source")
        input_caps = Gst.ElementFactory.make("capsfilter", "input-caps")
        parser = Gst.ElementFactory.make("h264parse", "parser")
        parsed_caps = Gst.ElementFactory.make("capsfilter", "parsed-caps")
        tee = Gst.ElementFactory.make("tee", "tee")
        metadata_queue = Gst.ElementFactory.make("queue", "metadata-queue")
        appsink = Gst.ElementFactory.make("appsink", "metadata-sink")

        elements = [src, input_caps, parser, parsed_caps, tee, metadata_queue, appsink]

        if not self.headless:
            video_queue = Gst.ElementFactory.make("queue", "video-queue")
            decoder = Gst.ElementFactory.make("avdec_h264", "decoder")
            videoconvert = Gst.ElementFactory.make("videoconvert", "convert")
            overlay = Gst.ElementFactory.make("textoverlay", "overlay")
            sink = Gst.ElementFactory.make("autovideosink", "sink")
            elements.extend([video_queue, decoder, videoconvert, overlay, sink])
        else:
            video_queue = decoder = videoconvert = overlay = sink = None

        if not all(elements):
            logging.error("Not all GStreamer elements could be created")
            logging.error("Install GStreamer base/good/bad/ugly/libav plugins if needed")
            return None

        src.set_property("host", self.host)
        src.set_property("port", self.port)
        input_caps.set_property(
            "caps", Gst.Caps.from_string("video/x-h264,stream-format=avc,alignment=au")
        )
        parsed_caps.set_property(
            "caps", Gst.Caps.from_string("video/x-h264,stream-format=avc,alignment=au")
        )
        appsink.set_property("emit-signals", True)
        appsink.set_property("sync", False)
        appsink.set_property("max-buffers", 4)
        appsink.set_property("drop", True)

        metadata_queue.set_property("max-size-buffers", 4)
        metadata_queue.set_property("max-size-bytes", 0)
        metadata_queue.set_property("max-size-time", 0)
        metadata_queue.set_property("leaky", 2)

        if overlay is not None:
            overlay.set_property("text", "Waiting for SEI timestamp...")
            overlay.set_property("halignment", "left")
            overlay.set_property("valignment", "top")
            overlay.set_property("font-desc", "Sans 18")
            overlay.set_property("shaded-background", True)
            self.overlay = overlay

        for element in elements:
            pipeline.add(element)

        if not src.link(input_caps):
            logging.error("Failed to link source to capsfilter")
            return None
        if not input_caps.link(parser):
            logging.error("Failed to link capsfilter to h264parse")
            return None
        if not parser.link(parsed_caps):
            logging.error("Failed to link h264parse to parsed capsfilter")
            return None
        if not parsed_caps.link(tee):
            logging.error("Failed to link parsed capsfilter to tee")
            return None
        if not tee.link(metadata_queue):
            logging.error("Failed to link metadata branch")
            return None
        if not metadata_queue.link(appsink):
            logging.error("Failed to link metadata appsink")
            return None

        if not self.headless:
            if not tee.link(video_queue):
                logging.error("Failed to link video branch")
                return None
            if not video_queue.link(decoder):
                logging.error("Failed to link decoder")
                return None
            if not decoder.link(videoconvert):
                logging.error("Failed to link videoconvert")
                return None
            if not videoconvert.link(overlay):
                logging.error("Failed to link textoverlay")
                return None
            if not overlay.link(sink):
                logging.error("Failed to link video sink")
                return None

        appsink.connect("new-sample", self.on_metadata_sample)
        self.pipeline = pipeline
        return pipeline

    def on_metadata_sample(self, sink: Gst.Element) -> Gst.FlowReturn:
        sample = sink.emit("pull-sample")
        if sample is None:
            return Gst.FlowReturn.OK

        buffer = sample.get_buffer()
        if buffer is None:
            return Gst.FlowReturn.OK

        success, map_info = buffer.map(Gst.MapFlags.READ)
        if not success:
            return Gst.FlowReturn.OK

        try:
            metadata_items = parse_avc_buffer(bytes(map_info.data))
        finally:
            buffer.unmap(map_info)

        for metadata in metadata_items:
            line = format_metadata_line(metadata)
            if self.headless:
                print(line, flush=True)
            elif self.overlay is not None:
                GLib.idle_add(self.overlay.set_property, "text", line)

        return Gst.FlowReturn.OK

    def run(self) -> int:
        pipeline = self.create_pipeline()
        if pipeline is None:
            return 1

        loop = GLib.MainLoop()
        bus = pipeline.get_bus()
        bus.add_signal_watch()

        def on_message(bus, msg):
            if msg.type == Gst.MessageType.EOS:
                logging.info("EOS reached")
                loop.quit()
            elif msg.type == Gst.MessageType.ERROR:
                error, debug = msg.parse_error()
                logging.error(error)
                logging.info(f"Debug: {debug}")
                loop.quit()

        bus.connect("message", on_message)

        logging.info(f"Connecting to {self.host}:{self.port} (AVC H.264)")
        pipeline.set_state(Gst.State.PLAYING)

        try:
            loop.run()
        except KeyboardInterrupt:
            logging.info("Interrupted")
        finally:
            pipeline.set_state(Gst.State.NULL)

        return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Receive a TCP AVC H.264 stream and display SEI publish timestamps"
    )
    parser.add_argument("--host", default="localhost", help="TCP server host")
    parser.add_argument("--port", type=int, default=5004, help="TCP server port")
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Do not open a window; print frame ID and SEI timestamp metadata",
    )
    args = parser.parse_args()

    Gst.init(None)
    return SeiReceiver(args.host, args.port, args.headless).run()


if __name__ == "__main__":
    sys.exit(main())
