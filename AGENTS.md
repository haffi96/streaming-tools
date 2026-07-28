# AGENTS.md

## Cursor Cloud specific instructions

### Overview

`streaming-tools` is a mixed Python + Go monorepo for network video/data streaming into LiveKit:

- `tools/gstreamer_source` — Python GStreamer publisher that generates/encodes H.264 and injects SEI timestamp/frame-id metadata (`gstreamer_source_sei.py`), plus a parser (`parse_h264_sei.py`).
- `apps/livekit_tester` — Python LiveKit data-channel / data-track publishers & subscribers (`livekit-api`, `livekit`).
- `apps/livekit_publisher` — standalone **Go** CLI that republishes pre-encoded H.264/H.265 TCP streams into a LiveKit room.

### Python (uv workspace)

Managed with **uv** (`pyproject.toml` declares a workspace: `tools/*` and `apps/livekit_tester/`). Python 3.12.

- `uv sync` alone only installs the root project (just `ruff`). Use **`uv sync --all-packages`** to install the workspace members' deps (`pygobject`, `livekit-api`, etc.). The update script does this.
- `pygobject`/`pycairo` build from source and need system libs (`libgirepository1.0-dev`, `gobject-introspection`, `libcairo2-dev`, `python3-dev`) plus the GStreamer runtime + plugins (`gstreamer1.0-tools`, `-plugins-base/-good/-bad/-ugly`, `-libav`). These are already installed in the Cloud VM.

Run / lint examples:

```bash
uv run ruff check .                         # lint (repo currently has pre-existing style findings)
cd tools/gstreamer_source
uv run python gstreamer_source_sei.py --output file --path /tmp/test.h264 --duration 2
uv run python parse_h264_sei.py samples/generated_sei.h264
```

The `livekit_tester` publishers/subscribers (`data_channels/`, `data_tracks/`) need a reachable LiveKit server + credentials (see below).

### Go (`apps/livekit_publisher`)

Requires **Go 1.25+** (`go.mod`); the VM's Go 1.26 satisfies it.

```bash
cd apps/livekit_publisher
go build ./cmd/livekit_publisher
go test ./...
go run ./cmd/livekit_publisher room join --help
```

### Local end-to-end testing (no cloud account needed)

`livekit-server` and the `lk` CLI are preinstalled in the Cloud VM. Run a local server and publish a GStreamer stream:

```bash
livekit-server --dev   # ws://localhost:7880, key "devkey", secret "secret"

# Terminal A: TCP H.264 source
uv run python tools/gstreamer_source/gstreamer_source_sei.py --output tcp --port 5004

# Terminal B: publish into a room (default --h26x-streaming-format annex-b matches the source)
apps/livekit_publisher/... go run ./cmd/livekit_publisher room join \
  --url ws://localhost:7880 --api-key devkey --api-secret secret \
  --identity gst-publisher --room test-room \
  --publish h264://FRONT_CAMERA@127.0.0.1:5004 --fps 30
```

Start the GStreamer/TCP source before the publisher (the publisher connects as a TCP client). The sibling `livekit-dashboard` repo can then view the room's live video and WebRTC stats.
