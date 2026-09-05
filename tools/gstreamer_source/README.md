# gstreamer-source

Standalone GStreamer source that simulates the camera pipeline on any dev
machine: macOS, Jetson Orin Nano, Jetson Thor and plain Ubuntu. It produces
H.264 (Annex B byte-stream or AVC length-prefixed) or raw NV12 frames over a
TCP server socket or into a file, and injects SEI timestamp / frame-id
metadata before every access unit so end-to-end latency can be measured.

The hybrid-bridge C++ publisher consumes this over TCP as a passthrough
source, so the defaults mirror its real encoder settings: constrained baseline,
one IDR per second, no B-frames, SPS/PPS in front of every IDR.

## Install

```bash
# from the repo root (workspace member, installs the `gstreamer-source` and `parse-h264` commands)
uv sync

# or, standalone in this directory
uv sync
```

Pre-requisites: GStreamer 1.20+ with the base/good/bad/ugly plugin sets,
Python 3.10+, and the platform's hardware encoder plugin (VideoToolbox is part
of the macOS GStreamer build, `nvv4l2h264enc` ships with JetPack, `va`/`v4l2`
plugins on Ubuntu).

## Usage

```bash
# What is available on this machine
uv run gstreamer-source --list-cameras --list-encoders

# Default: test pattern, best encoder, byte-stream H.264 + SEI, TCP server on 0.0.0.0:5004
uv run gstreamer-source

# AVC (length-prefixed) framing, reachable from another machine on the LAN
uv run gstreamer-source --stream-format avc --bind 0.0.0.0 --port 5004

# Camera: prompt / auto-pick, or select by index, device path, or name
uv run gstreamer-source --camera
uv run gstreamer-source --camera 1
uv run gstreamer-source --camera /dev/video2
uv run gstreamer-source --camera "FaceTime"

# Force an encoder / profile / bitrate
uv run gstreamer-source --encoder x264 --profile main --bitrate 4000

# Raw NV12 frames (fixed-size, width*height*1.5 bytes each)
uv run gstreamer-source --codec nv12 --output tcp --port 5004

# File output for offline checks (stops after --duration seconds)
uv run gstreamer-source --output file --path test.h264 --duration 5

# Disable SEI injection
uv run gstreamer-source --no-sei-metadata
```

Inspect a stream or file with the parser. It logs every frame (size, NAL
types, keyframe, live fps) whether or not the stream carries SEI metadata, and
auto-detects byte-stream vs avc framing. Add `--sei-detection` (or
`--sei_detection`) to decode the SEI timestamps: embedded capture time for
files, latency for TCP. That latency is receive time minus the frame's capture
timestamp, so it includes capture delivery, conversion and encoding as well as
transport; the parser prints a note saying so. Transport alone is not measured.

```bash
uv run parse-h264 test.h264
uv run parse-h264 --tcp --host <source-ip> --port 5004
uv run parse-h264 --tcp --host <source-ip> --port 5004 --sei-detection
uv run parse-h264 --tcp --host <source-ip> --port 5004 --nv12 1280x720   # raw NV12 stream
ffplay -i test.h264
```

## Platform behaviour

| Platform | Detected via | Camera source | Encoder auto order |
| --- | --- | --- | --- |
| macOS | `platform.system()` | `avfvideosrc` (GstDeviceMonitor) | `vtenc` > `x264` |
| Jetson (Orin Nano, Thor, ...) | `/etc/nv_tegra_release`, tegra kernel, device-tree model | CSI: `nvarguscamerasrc`, USB: `v4l2src` | `nvv4l2` > `x264` |
| Ubuntu / other Linux | fallback | `v4l2src` | `v4l2` > `va` > `vaapi` > `x264` |

`--list-encoders` shows every known encoder with the reason it is unusable.
On Jetson the `nvv4l2h264enc` plugin is always installed, so the tool also
checks for an NVENC device node; on an Orin Nano (no H.264 hardware encoder)
`auto` therefore falls through to `x264`. Requesting an encoder explicitly
that is not usable is an error rather than a silent fallback.

Pipeline shape:

```
source ! NV12 caps ! queue(leaky, 1 buffer)
       ! [nvvidconv] ! <encoder> ! [profile caps] ! h264parse config-interval=-1
       ! video/x-h264,stream-format=<byte-stream|avc>,alignment=au   <- SEI probe
       ! tcpserversink | filesink
```

## SEI metadata

Enabled by default (`--sei-metadata` / `--no-sei-metadata`). Each access unit
is prefixed with one SEI NAL (`user_data_unregistered`, UUID
`3fa85f6457174562b3fc2c963f66afa6`) carrying an `LKTS` packet trailer with a
wall-clock timestamp in microseconds and a 32-bit frame id. The NAL uses the
same framing as the stream (start code or 4-byte length), so it works with
both `byte-stream` and `avc` over TCP and file.

The timestamp is the frame's **capture time**: the buffer PTS set by the
source (V4L2 buffer timestamp, Argus sensor timestamp, AVFoundation sample
time, or the scheduled time for `videotestsrc`) converted to wall clock. The
latency `parse-h264 --sei-detection` reports therefore covers capture-to-
encoder queuing, encoding, and transport, not just transport. Sensor exposure
itself is not included unless the driver's timestamp already accounts for it.
Cross-machine measurements need synchronized clocks.

## Layout

```
gstreamer_source/
  cli.py        argparse entry point (`gstreamer-source`)
  platform.py   macOS / Jetson / Linux detection
  encoders.py   encoder discovery, preference order, per-encoder settings
  cameras.py    camera enumeration and --camera selection
  pipeline.py   pipeline construction
  sei.py        SEI NAL construction and pad-probe injector
  parse.py      parser / verifier (`parse-h264`, file or TCP)
test_sei_metadata.py
```

Tests: `uv run python -m unittest test_sei_metadata` from this directory.
