# gstreamer-source

Standalone GStreamer source that simulates the camera pipeline on any dev
machine: macOS, Jetson Orin Nano, Jetson Thor and plain Ubuntu. It produces
H.264 or H.265/HEVC (Annex B byte-stream or 4-byte length-prefixed) or raw
NV12 frames over a TCP server socket or into a file, and injects SEI
timestamp / frame-id metadata before every access unit so end-to-end latency
can be measured.

The hybrid-bridge C++ publisher consumes this over TCP as a passthrough
source, so the defaults mirror its real encoder settings: constrained baseline,
one IDR per second, no B-frames, SPS/PPS in front of every IDR. H.264 is the
default; `--codec h265` produces the H.265 stream the vehicle encoders emit in
production (Main profile, one IDR per second, no frame reordering, VPS/SPS/PPS
in front of every IDR).

## Install

```bash
# from the repo root (workspace member, installs the `gstreamer-source`,
# `parse-h264` and `parse-h265` commands)
uv sync

# or, standalone in this directory
uv sync
```

Pre-requisites: GStreamer 1.20+ with the base/good/bad/ugly plugin sets,
Python 3.10+, and the platform's hardware encoder plugin (VideoToolbox is part
of the macOS GStreamer build, `nvv4l2h264enc`/`nvv4l2h265enc` ship with
JetPack, `nvh264enc`/`nvh265enc` come from the `nvcodec` plugin in
gst-plugins-bad on machines with a discrete NVIDIA GPU, `va`/`v4l2` plugins on
Ubuntu). The H.265 software fallback `x265enc` is in gst-plugins-bad and needs
libx265.

The gst-python overrides are optional: the tool runs on the raw PyGObject
binding too (Arch/Omarchy without the `gst-python` package). On Arch install
`gstreamer gst-plugins-base gst-plugins-good gst-plugins-bad gst-plugins-ugly
gst-plugin-va gst-python` for the full feature set.

## Usage

```bash
# What is available on this machine
uv run gstreamer-source --list-cameras --list-encoders

# Default: test pattern, best encoder, byte-stream H.264 + SEI, TCP server on 0.0.0.0:5004
uv run gstreamer-source

# AVC (length-prefixed) framing, reachable from another machine on the LAN
uv run gstreamer-source --stream-format avc --bind 0.0.0.0 --port 5004

# H.265: Annex B byte-stream, or 4-byte length-prefixed (hev1 caps)
uv run gstreamer-source --codec h265 --port 5004
uv run gstreamer-source --codec h265 --stream-format avc --port 5004
uv run gstreamer-source --codec h265 --output file --path test.h265 --duration 5

# Camera: prompt / auto-pick, or select by index, device path, or name
uv run gstreamer-source --camera
uv run gstreamer-source --camera 1
uv run gstreamer-source --camera /dev/video2
uv run gstreamer-source --camera "FaceTime"

# Force an encoder / profile / bitrate
uv run gstreamer-source --encoder x264 --profile main --bitrate 4000
uv run gstreamer-source --codec h265 --encoder vtenc --bitrate 4000

# Raw NV12 frames (fixed-size, width*height*1.5 bytes each)
uv run gstreamer-source --codec nv12 --output tcp --port 5004

# File output for offline checks (stops after --duration seconds)
uv run gstreamer-source --output file --path test.h264 --duration 5

# Disable SEI injection
uv run gstreamer-source --no-sei-metadata

# Burn a running millisecond clock (h:mm:ss.mmm since start) into the frames
uv run gstreamer-source --timestamps
```

Inspect a stream or file with the parser. It logs every frame (size, NAL
types, keyframe, live fps) whether or not the stream carries SEI metadata, and
auto-detects byte-stream vs avc framing and H.264 vs H.265 (from the NAL
headers; `--codec h264|h265` forces it, `parse-h265` is `parse-h264` with
`--codec h265`). Add `--sei-detection` (or
`--sei_detection`) to decode the SEI timestamps: embedded capture time for
files, latency for TCP. That latency is receive time minus the frame's capture
timestamp, so it includes capture delivery, conversion and encoding as well as
transport; the parser prints a note saying so. Transport alone is not measured.

```bash
uv run parse-h264 test.h264
uv run parse-h264 --tcp --host <source-ip> --port 5004
uv run parse-h264 --tcp --host <source-ip> --port 5004 --sei-detection
uv run parse-h264 --tcp --host <source-ip> --port 5004 --nv12 1280x720   # raw NV12 stream
uv run parse-h264 --codec h265 --tcp --host <source-ip> --port 5004 --sei-detection
uv run parse-h265 test.h265
ffplay -i test.h264
ffplay -i test.h265
```

For H.265 the frame lines show HEVC NAL names (`VPS`, `SPS`, `PPS`, `AUD`,
`SEI` = prefix SEI, `suffix-SEI`, `IDR_W_RADL`, `IDR_N_LP`, `CRA`,
`TRAIL_R`, ...); keyframes are IRAP pictures (types 16-21), marked `IDR`,
`CRA` or `BLA`.

### Stream formats

`--stream-format` takes the same two values for both codecs:

| CLI value | Wire format | H.264 caps | H.265 caps |
| --- | --- | --- | --- |
| `byte-stream` (default) | Annex B, 4-byte start codes | `stream-format=byte-stream` | `stream-format=byte-stream` |
| `avc` | 4-byte big-endian length before every NAL | `stream-format=avc` | `stream-format=hev1` |

For H.265, `avc` maps to `hev1` rather than `hvc1` because the VPS/SPS/PPS
are carried in-band in front of every IDR, which `hvc1` forbids; the bytes on
the wire are the same either way (h265parse writes 4-byte lengths for both).
Over TCP there is no out-of-band codec_data, so a receiver of the
length-prefixed form relies on those in-band parameter sets.

## Platform behaviour

| Platform | Detected via | Camera source | Encoder auto order |
| --- | --- | --- | --- |
| macOS | `platform.system()` | `avfvideosrc` (GstDeviceMonitor) | `vtenc` > `x264` / `x265` |
| Jetson (Orin Nano, Thor, ...) | `/etc/nv_tegra_release`, tegra kernel, device-tree model | CSI: `nvarguscamerasrc`, USB: `v4l2src` | `nvv4l2` > `x264` / `x265` |
| Ubuntu / other Linux | fallback | `v4l2src` | `nvenc` > `v4l2` > `va` > `vaapi` > `x264` / `x265` |

### Encoders

`--encoder` names an encoder family; the codec picks the element:

| `--encoder` | H.264 element | H.265 element | Low-latency settings |
| --- | --- | --- | --- |
| `x264` / `x265` | `x264enc` | `x265enc` | `ultrafast` / `zerolatency`, key-int-max = fps, no B-frames (x265: `option-string=bframes=0:open-gop=0`) |
| `vtenc` | `vtenc_h264_hw` > `vtenc_h264` | `vtenc_h265_hw` > `vtenc_h265` | `realtime`, `allow-frame-reordering=false`, `max-keyframe-interval` = fps |
| `nvv4l2` | `nvv4l2h264enc` | `nvv4l2h265enc` | CBR, UltraFast preset, I/IDR interval = fps, `insert-sps-pps`, `maxperf-enable` |
| `nvenc` | `nvh264enc` | `nvh265enc` | preset `p1`, `ultra-low-latency`, CBR, gop-size = fps, no B-frames, `zerolatency` |
| `v4l2` | `v4l2h264enc` | `v4l2h265enc` | `extra-controls`: bitrate, profile, I-frame period / GOP size, repeat sequence header |
| `va` | `vah264enc` | `vah265enc` | CBR, key-int-max = fps, no B-frames |
| `vaapi` | `vaapih264enc` | `vaapih265enc` | CBR, keyframe-period = fps, no B-frames |

Profiles: H.264 `baseline` (constrained, default), `main`, `high`; H.265 `main`
only (the default; an H.264 profile with `--codec h265` is an error).
`--threads` / `--sliced-threads` are x264-only and ignored (with a warning) for
H.265. Only the macOS VideoToolbox H.265 path has been run; the other H.265
encoders mirror their H.264 settings and are untested.

VideoToolbox H.265 (`vtenc_h265_hw`, macOS 15, M-series) with these settings
produces Main profile, level 3.1 at 720p, IDR_N_LP keyframes every `fps`
frames and only TRAIL_R pictures in between. Its SPS has
`sps_max_num_reorder_pics = 0`, `sps_max_latency_increase_plus1 = 0`,
`sps_max_dec_pic_buffering_minus1 = 4`; the VUI carries only the video signal
type (limited range, BT.709) - no timing info and no bitstream_restriction. The
first access unit also carries a VideoToolbox user_data_unregistered SEI of
its own (different UUID), which the parser skips.
### Cameras

On Linux and Jetson every `/dev/video*` node is queried directly with the V4L2
`QUERYCAP` / `ENUM_FMT` ioctls rather than through GstDeviceMonitor: on Jetson
the monitor is PipeWire-backed and hides the CSI sensor node while listing UVC
metadata nodes. Capture nodes owned by the `tegra-video` driver are CSI sensors
and become `nvarguscamerasrc sensor-id=N` (N in node order); metadata-only
nodes are skipped; everything else is a `v4l2src` camera listed with its pixel
formats, e.g.

```
Detected cameras (3):
  [0] imx219 9-0010 (CSI) [csi] nvarguscamerasrc sensor-id=0 formats=RG10 (/dev/video0)
  [1] H264 USB Camera: USB Camera [v4l2] v4l2src device=/dev/video1 formats=MJPG,YUYV
  [2] H264 USB Camera: USB Camera [v4l2] v4l2src device=/dev/video3 formats=H264
```

UVC cameras usually reach 720p/1080p at 30 fps only via MJPEG, so v4l2 cameras
are captured as `image/jpeg ! jpegdec` whenever MJPG is offered
(`--camera-format raw` forces a raw format, `mjpeg` requires MJPEG). Nodes that
only offer H.264 (like `/dev/video3` above) cannot be used by this tool, which
re-encodes raw frames; selecting one is an error.

`--list-encoders` shows every known encoder for both codecs with the reason
it is unusable. On Jetson the `nvv4l2h264enc`/`nvv4l2h265enc` plugins are
always installed, so the tool also checks for an NVENC device node; on an
Orin Nano (no hardware video encoder) `auto` therefore falls through to `x264`
/ `x265`. Requesting an encoder explicitly that is not usable is an error
rather than a silent fallback.

On a desktop Linux box with an NVIDIA GPU the `nvcodec` plugin registers
`nvh264enc` only if it can create a CUDA context and load `libnvidia-encode`
when the plugin registry is scanned. If `--list-encoders` reports `nvenc` as
not installed while `nvidia-smi` works, the cached registry was probably built
while the driver was unavailable; `rm ~/.cache/gstreamer-1.0/registry.*.bin`
forces a rescan.

### x264 threading and slices

x264 is tuned `zerolatency` / `ultrafast`. `--sliced-threads` splits every
frame into one slice per thread, which parallelises encoding without adding
delay but emits several VCL NALs per access unit. Not every consumer copes with
that: the LiveKit Go SDK reader behind `livekit-cli` paces and packetizes each
VCL NAL as a complete frame, so multi-slice video plays slowly and partly green.
It is therefore off by default (one slice per frame, decodes everywhere); turn
it on for the hybrid-bridge passthrough or any other consumer that assembles
access units properly.

Without sliced threads x264 frame-threads instead, and every extra thread adds
a frame of output delay, so `--threads` defaults to 1 in that mode (4 with
`--sliced-threads`). Measured on an Orin Nano with `videotestsrc`, SEI capture
timestamps and `parse-h264 --sei-detection` on the same host:

| Mode | 720p30 | 1080p30 | NALs per frame |
|------|--------|---------|----------------|
| `--sliced-threads` (threads 4) | ~6 ms | - | 4 slices |
| default (threads 1) | ~14 ms | ~21 ms | 1 slice |
| `--no-sliced-threads --threads 4` | ~103 ms | ~106 ms | 1 slice |

Hardware encoders always emit one slice per frame.

Pipeline shape:

```
source ! NV12 caps ! queue(leaky, 1 buffer) ! [textoverlay]   <- --timestamps
       ! [nvvidconv] ! <encoder> ! [profile caps] ! h264parse|h265parse config-interval=-1
       ! video/x-h264,stream-format=<byte-stream|avc>,alignment=au   <- SEI probe
         (video/x-h265,stream-format=<byte-stream|hev1>,alignment=au)
       ! tcpserversink | filesink
```

## SEI metadata

Enabled by default (`--sei-metadata` / `--no-sei-metadata`). Each access unit
is prefixed with one SEI NAL (`user_data_unregistered`, UUID
`3fa85f6457174562b3fc2c963f66afa6`) carrying an `LKTS` packet trailer with a
wall-clock timestamp in microseconds and a 32-bit frame id. The NAL uses the
same framing as the stream (start code or 4-byte length), so it works with
both `byte-stream` and `avc` over TCP and file.

With `--au-terminator` (default on) every access unit also ends with an AUD
NAL, so a byte-stream receiver can complete the frame as soon as its last byte
arrives instead of when the next start code does.

NAL layouts (shown after the start code / length prefix):

```
H.264 SEI:  06 | 05 | size | UUID(16) | LKTS trailer | 80
H.265 SEI:  4E 01 | 05 | size | UUID(16) | LKTS trailer | 80
H.264 AUD:  09 F0
H.265 AUD:  46 01 50
```

- `06`: H.264 nal_ref_idc 0, nal_unit_type 6 (SEI).
- `4E 01`: H.265 prefix SEI, nal_unit_type 39, nuh_layer_id 0,
  nuh_temporal_id_plus1 1.
- `05`: payloadType 5 (user_data_unregistered); `size` is one byte
  (UUID + trailer, 37 bytes with timestamp and frame id); `80` is the RBSP
  stop bit.
- `09 F0`: H.264 AUD, primary_pic_type 7 + stop bit (2 bytes).
- `46 01 50`: H.265 AUD, nal_unit_type 35, layer 0, tid 1, then pic_type 2
  ("I, P, B", `010`) + stop bit (3 bytes).

The SEI payload gets emulation prevention bytes (`00 00 0x` -> `00 00 03 0x`)
when the XOR-ed trailer happens to contain a start-code-like run (rare: a
`FF FF` in the timestamp or frame id); the parser strips them again. AUDs have
a fixed size, so `parse-h264` completes a 2-byte H.264 / 3-byte H.265 AUD
without waiting for the next start code. VideoToolbox, nvenc/va/vaapi H.265
(when terminating AUs) and x265 emit no AUDs of their own; x264 and x265 put
a leading AUD in every AU only when `--no-au-terminator` is given.

The timestamp is the frame's **capture time**: the buffer PTS set by the
source (V4L2 buffer timestamp, Argus sensor timestamp, AVFoundation sample
time, or the scheduled time for `videotestsrc`) converted to wall clock. The
latency `parse-h264 --sei-detection` reports therefore covers capture-to-
encoder queuing, encoding, and transport, not just transport. Sensor exposure
itself is not included unless the driver's timestamp already accounts for it.
Cross-machine measurements need synchronized clocks.

## Timestamp overlay

`--timestamps` burns a running clock (`h:mm:ss.mmm`) into the top-left corner
of every frame so a receiver's display can be compared against the source by
eye or from a screen recording. The text is rendered by `textoverlay`; a pad
probe rewrites it per frame from the buffer's running time, i.e. the frame's
capture time relative to pipeline start, the same instant the SEI timestamp
encodes. No wall-clock or monotonic clock is read on the streaming thread, so
the only cost is the pango render of the changed text. On Jetson CSI sources
the overlay forces a round trip through system memory (`nvvidconv` before and
after). Works with `--codec nv12` too, since it sits before the encoder.

## Layout

```
gstreamer_source/
  cli.py        argparse entry point (`gstreamer-source`)
  platform.py   macOS / Jetson / Linux detection
  encoders.py   H.264/H.265 encoder discovery, preference order, per-encoder settings
  cameras.py    camera enumeration and --camera selection
  pipeline.py   pipeline construction
  sei.py        SEI NAL construction and pad-probe injector
  overlay.py    --timestamps running-clock textoverlay driver
  parse.py      parser / verifier (`parse-h264`, `parse-h265`, file or TCP)
test_sei_metadata.py
```

Tests: `uv run python -m unittest test_sei_metadata` from this directory.
