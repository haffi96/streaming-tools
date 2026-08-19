# livekit-publisher

Build from source:

Clone:

```bash
git clone git@gitlab.com:oxa_ua/pe/ro/ra/livekit-publisher.git
cd livekit-publisher
```

Mac:

```bash
GOOS=darwin GOARCH=arm64 CGO_ENABLED=0 go build -ldflags "-w -s" -o bin/livekit-publisher ./cmd/publisher
```

Linux arm64:

```bash
GOOS=linux GOARCH=arm64 CGO_ENABLED=0 go build -ldflags "-w -s" -o linux-arm-bin/livekit-publisher ./cmd/publisher
```

Linux amd64:

```bash
GOOS=linux GOARCH=amd64 CGO_ENABLED=0 go build -ldflags "-w -s" -o linux-arm-bin/livekit-publisher ./cmd/publisher
```

Standalone Go CLI for publishing pre-encoded H.264/H.265 TCP streams into LiveKit.

## Usage

```bash
go run ./cmd/publisher room join \
  --url wss://your-project.livekit.cloud \
  --api-key <key> \
  --api-secret <secret> \
  --identity lk-cli-publisher \
  --room test-recordings \
  --publish h264://FRONT_CAMERA@127.0.0.1:5004 \
  --publish h264://REAR_CAMERA@127.0.0.1:5005 \
  --reconnect-delay 3s \
  --h26x-streaming-format length-prefixed
```

Simulcast uses repeated `--publish` flags with the same logical name and explicit dimensions:

```bash
go run ./cmd/publisher room join \
  --url wss://your-project.livekit.cloud \
  --api-key <key> \
  --api-secret <secret> \
  --identity lk-cli-publisher \
  --room test-recordings \
  --publish h264://FRONT_CAMERA@127.0.0.1:5005/1920x1080 \
  --publish h264://FRONT_CAMERA@127.0.0.1:5006/1280x720 \
  --publish h264://FRONT_CAMERA@127.0.0.1:5007/640x480
```

## Notes

- Phase one supports H.264/H.265 TCP inputs only.
- Multiple unique names publish as multiple tracks on one participant.
- Repeated names become a simulcast track only when all layers include dimensions.
- Packet-trailer timestamp and frame ID parsing is enabled for H.264/H.265 inputs.
- By default, TCP sources retry forever every 3 seconds when a source is unavailable or stops publishing.
- `--reconnect-attempts 0` disables reconnects and restores the previous one-shot behavior.

## OpenTelemetry metrics

Metrics are disabled unless `OTEL_EXPORTER_OTLP_ENDPOINT` or
`OTEL_EXPORTER_OTLP_METRICS_ENDPOINT` is set. The standard OTEL exporter
variables control the endpoint, protocol, TLS, resource attributes, and export
interval. For the local stack:

```bash
cd observability
docker compose up -d

export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317
export OTEL_EXPORTER_OTLP_PROTOCOL=grpc
export OTEL_EXPORTER_OTLP_INSECURE=true
export OTEL_METRIC_EXPORT_INTERVAL=5000
```

Grafana is available at <http://localhost:3000> (`admin` / `admin`) and
Prometheus at <http://localhost:9090>. The LiveKit Publisher dashboard is
provisioned automatically.

Run the two local test streams without putting credentials in the command or
repository:

```bash
export LIVEKIT_URL='wss://your-project.livekit.cloud'
export LIVEKIT_API_KEY='...'
export LIVEKIT_API_SECRET='...'

go run ./cmd/publisher room join \
  --url "$LIVEKIT_URL" \
  --api-key "$LIVEKIT_API_KEY" \
  --api-secret "$LIVEKIT_API_SECRET" \
  --identity lk-cli-publisher \
  --room testing \
  --publish h264://CAMERA_1@127.0.0.1:5004 \
  --publish h264://CAMERA_2@127.0.0.1:5005 \
  --h26x-streaming-format length-prefixed \
  --attach-frame-metadata
```

The exported instruments are:

- `livekit_publisher.frames.consumed`: complete encoded frames parsed from TCP.
- `livekit_publisher.frame.size`: parsed encoded frame sizes in bytes.
- `livekit_publisher.frames.published`: frames whose RTP packet writes all completed successfully.
- `livekit_publisher.frames.publish_errors`: failed frame writes.
- `livekit_publisher.frame.consume_to_publish`: time from parsing a frame through completed RTP writes.
- `livekit_publisher.frame.publish_duration`: time spent in `WriteSample`.
- `livekit_publisher.backlog.schedule_lag` and `livekit_publisher.backlog.events`: how far the synchronous writer falls behind its frame schedule.
- `livekit_publisher.backlog.current_lag`: latest schedule lag per active track.
- `livekit_publisher.ice.connection`: current publisher ICE path, classified as direct UDP, TURN, or unknown.

“Published” means handed successfully to the WebRTC transport, not received or
decoded by a remote participant. The publisher has no application frame queue:
when a write is slow, TCP reads stop and buffering occurs in the kernel or
upstream sender. Schedule lag measures that portable backpressure signal; it is
not a queued frame or byte count.

The current `go.mod` pins the `observer-metrics` branch of
`github.com/haffi96/server-sdk-go` using an immutable Go pseudo-version. The
fork supplies the sample lifecycle observer while retaining the canonical
`github.com/livekit/server-sdk-go/v2` module path. Remove the `replace` line
after that API is released upstream and update the required SDK version to the
corresponding tag.
