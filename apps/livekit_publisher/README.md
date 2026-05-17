# livekit_publisher

Standalone Go CLI for publishing pre-encoded H.264/H.265 TCP streams into LiveKit.

## Usage

```bash
go run ./cmd/livekit_publisher room join \
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
go run ./cmd/livekit_publisher room join \
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
